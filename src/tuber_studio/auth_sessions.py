"""Persistent opaque management sessions, bound to the current local admin key.

Only session/key fingerprints enter SQLite. The private admin-token bootstrap
file remains the operator's recovery source; the environment takes precedence.
Server sessions have no fixed lifetime. Browser persistence is advisory and its
own retention limits still apply; active cookies renew at most once per day.
"""

import hashlib
import os
import re
import secrets
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .config import atomic_bytes

COOKIE_NAME = "tuber_admin"
COOKIE_MAX_AGE = 10 * 365 * 24 * 3600
COOKIE_REFRESH_SECONDS = 24 * 3600
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{64}$")


class InvalidAdminCredential(ValueError):
    pass


class EnvironmentManagedToken(ValueError):
    pass


def _fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _private_regular(path, *, create=False):
    path = Path(path)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("管理凭证或会话数据库不能使用符号链接")
    if create:
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("管理凭证与会话数据库必须是普通文件")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        actual = os.fstat(descriptor)
        if not stat.S_ISREG(actual.st_mode):
            raise ValueError("管理凭证与会话数据库必须是普通文件")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            # Windows follows runtime-directory ACLs; refuse a changed leaf before chmod.
            if (metadata.st_dev, metadata.st_ino) != (actual.st_dev, actual.st_ino) or path.is_symlink():
                raise ValueError("管理凭证路径已变化")
            path.chmod(0o600)
    finally:
        os.close(descriptor)


class AuthSessions:
    @staticmethod
    def prepare_runtime(settings):
        """Validate DB paths before Store/SQLite can follow a link or create a public DB."""
        for suffix in ("", "-wal", "-shm"):
            _private_regular(settings.runtime / ("studio.sqlite3" + suffix), create=not suffix)
        if not os.getenv("TUBER_ADMIN_TOKEN"):
            _private_regular(settings.runtime / "admin-token")

    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.lock = threading.RLock()
        path = store.db.execute("PRAGMA database_list").fetchone()[2]
        if not path or not Path(path).resolve().is_relative_to(settings.runtime.resolve()):
            raise ValueError("管理会话必须保存在当前runtime数据库")
        for suffix in ("", "-wal", "-shm"):
            _private_regular(Path(path + suffix))
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS admin_sessions(
                session_hash TEXT PRIMARY KEY,token_fingerprint TEXT NOT NULL,
                created_at REAL NOT NULL,refreshed_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS admin_session_authority(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),token_fingerprint TEXT NOT NULL);
        """)
        with self._transaction():
            token, _ = self._authority()
        if token is None:
            raise ValueError("管理凭证不可用，请在本机检查配置")

    @contextmanager
    def _transaction(self):
        with self.lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.store.db.commit()
            except BaseException:
                self.store.db.rollback()
                raise

    def _read_token(self):
        environment = os.getenv("TUBER_ADMIN_TOKEN")
        if environment:
            token = environment
        else:
            path = self.settings.runtime / "admin-token"
            _private_regular(path)
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            )
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("管理凭证必须是普通文件")
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError("管理凭证文件无效")
            token = raw.decode("utf-8").strip()
        if not token:
            raise ValueError("管理凭证为空")
        self.settings.admin_token = token
        return token

    def _authority(self):
        try:
            token = self._read_token()
            fingerprint = _fingerprint("tuber-admin-key-v1\0" + token)
        except (OSError, ValueError, UnicodeError):
            token, fingerprint = None, "unavailable"
        previous = self.store.db.execute(
            "SELECT token_fingerprint FROM admin_session_authority WHERE singleton=1"
        ).fetchone()
        if previous is None or previous[0] != fingerprint:
            self.store.db.execute("DELETE FROM admin_sessions")
            self.store.db.execute(
                "INSERT OR REPLACE INTO admin_session_authority VALUES(1,?)", (fingerprint,)
            )
        return token, fingerprint

    @staticmethod
    def _matches(value, actual):
        if not isinstance(value, str) or actual is None:
            return False
        try:
            return secrets.compare_digest(value.encode("utf-8"), actual.encode("utf-8"))
        except UnicodeError:
            return False

    @staticmethod
    def _session_hash(cookie):
        return _fingerprint(cookie) if isinstance(cookie, str) and SESSION_PATTERN.fullmatch(cookie) else None

    def _valid(self, cookie, fingerprint):
        digest = self._session_hash(cookie)
        return bool(
            digest
            and self.store.db.execute(
                "SELECT 1 FROM admin_sessions WHERE session_hash=? AND token_fingerprint=?",
                (digest, fingerprint),
            ).fetchone()
        )

    def authenticate(self, token):
        with self._transaction():
            actual, _ = self._authority()
            return self._matches(token, actual)

    def valid(self, cookie):
        with self._transaction():
            token, fingerprint = self._authority()
            return token is not None and self._valid(cookie, fingerprint)

    def issue(self, token, previous_cookie=None):
        session = None
        with self._transaction():
            actual, fingerprint = self._authority()
            if self._matches(token, actual):
                session, now = secrets.token_urlsafe(48), time.time()
                digest = self._session_hash(previous_cookie)
                if digest:
                    self.store.db.execute("DELETE FROM admin_sessions WHERE session_hash=?", (digest,))
                self.store.db.execute(
                    "INSERT INTO admin_sessions VALUES(?,?,?,?)",
                    (_fingerprint(session), fingerprint, now, now),
                )
        if session is None:
            raise InvalidAdminCredential("管理令牌无效")
        return session

    def revoke(self, cookie):
        digest = self._session_hash(cookie)
        if digest:
            with self._transaction():
                self.store.db.execute("DELETE FROM admin_sessions WHERE session_hash=?", (digest,))

    def needs_cookie_refresh(self, cookie):
        with self._transaction():
            token, fingerprint = self._authority()
            if token is None or not self._valid(cookie, fingerprint):
                return False
            now = time.time()
            cursor = self.store.db.execute(
                "UPDATE admin_sessions SET refreshed_at=? WHERE session_hash=? AND refreshed_at<=?",
                (now, self._session_hash(cookie), now - COOKIE_REFRESH_SECONDS),
            )
            return cursor.rowcount == 1

    def reset_token(self, *, cookie=None, bearer=None):
        result, failure = None, None
        with self._transaction():
            actual, fingerprint = self._authority()
            if actual is None or not (self._matches(bearer, actual) or self._valid(cookie, fingerprint)):
                failure = InvalidAdminCredential("请重新登录后重置管理令牌")
            elif os.getenv("TUBER_ADMIN_TOKEN"):
                failure = EnvironmentManagedToken("管理令牌由TUBER_ADMIN_TOKEN配置，请更新环境变量后重启服务")
            else:
                result = secrets.token_urlsafe(48)
                path = self.settings.runtime / "admin-token"
                _private_regular(path)
                atomic_bytes(path, (result + "\n").encode("utf-8"), private=True)
                self.settings.admin_token = result
                self.store.db.execute("DELETE FROM admin_sessions")
                self.store.db.execute(
                    "UPDATE admin_session_authority SET token_fingerprint=? WHERE singleton=1",
                    (_fingerprint("tuber-admin-key-v1\0" + result),),
                )
        if failure is not None:
            raise failure
        return result

    def status(self):
        environment = bool(os.getenv("TUBER_ADMIN_TOKEN"))
        return {
            "persistent": True,
            "server_session_expiry": None,
            "cookie_max_age_seconds": COOKIE_MAX_AGE,
            "cookie_refresh_seconds": COOKIE_REFRESH_SECONDS,
            "token_source": "environment" if environment else "file",
            "reset_allowed": not environment,
        }
