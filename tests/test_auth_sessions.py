"""Persistent management auth with real SQLite and isolated credential files."""

import hashlib
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tuber_studio.auth_sessions import (
    COOKIE_REFRESH_SECONDS,
    AuthSessions,
    EnvironmentManagedToken,
    InvalidAdminCredential,
)
from tuber_studio.store import Store


def make_sessions(settings, name="auth.sqlite3"):
    store = Store(
        settings.runtime / name,
        [
            {
                "id": "fixture",
                "name": "fixture",
                "core": {},
                "style": {},
                "voice_profile": "fixture",
            }
        ],
    )
    return AuthSessions(settings, store), store


def test_only_hashes_are_stored_and_session_survives_reopening_and_years(settings, monkeypatch):
    auth, store = make_sessions(settings)
    cookie = auth.issue(settings.admin_token)
    assert len(cookie) == 64 and cookie != settings.admin_token
    row = store.one("SELECT * FROM admin_sessions")
    assert row["session_hash"] == hashlib.sha256(cookie.encode()).hexdigest()
    dump = "\n".join(store.db.iterdump())
    assert cookie not in dump and settings.admin_token not in dump
    created = row["created_at"]
    store.close()
    auth, store = make_sessions(settings)
    try:
        monkeypatch.setattr("tuber_studio.auth_sessions.time.time", lambda: created + 20 * 365 * 86400)
        assert auth.valid(cookie)
        assert auth.needs_cookie_refresh(cookie)
        assert not auth.needs_cookie_refresh(cookie)
    finally:
        store.close()


def test_shared_workers_rotate_key_and_revoke_every_old_cookie_and_bearer(settings):
    first, first_store = make_sessions(settings)
    second, second_store = make_sessions(settings)
    try:
        old = settings.admin_token
        one, two = first.issue(old), second.issue(old)
        assert second.valid(one) and first.valid(two)
        new = first.reset_token(cookie=one)
        assert new != old and (settings.runtime / "admin-token").read_text().strip() == new
        assert not first.valid(one) and not second.valid(two)
        assert not first.authenticate(old) and not second.authenticate(old)
        assert second.authenticate(new)
        with pytest.raises(InvalidAdminCredential):
            second.reset_token(cookie=two)
        fresh = second.issue(new)
        assert first.valid(fresh)
        # Restoring a former key must not resurrect its previously revoked rows.
        (settings.runtime / "admin-token").write_text(old)
        assert not first.valid(fresh) and not first.valid(one)
    finally:
        first_store.close()
        second_store.close()


def test_external_file_change_and_missing_file_fail_closed_without_cached_key_fallback(settings):
    auth, store = make_sessions(settings)
    try:
        old = settings.admin_token
        cookie = auth.issue(old)
        path = settings.runtime / "admin-token"
        path.write_text("new-external-secret")
        assert not auth.authenticate(old) and not auth.valid(cookie)
        assert auth.authenticate("new-external-secret")
        new_cookie = auth.issue("new-external-secret")
        path.unlink()
        assert not auth.valid(new_cookie)
        path.write_text("new-external-secret")
        assert not auth.valid(new_cookie)
        assert auth.authenticate("new-external-secret")
    finally:
        store.close()


def test_environment_precedence_is_preserved_and_reset_is_not_worker_local(settings, monkeypatch):
    file_token = settings.admin_token
    monkeypatch.setenv("TUBER_ADMIN_TOKEN", "environment-token")
    auth, store = make_sessions(settings)
    try:
        cookie = auth.issue("environment-token")
        assert not auth.authenticate(file_token)
        (settings.runtime / "admin-token").write_text("changed-file-token")
        assert auth.valid(cookie) and auth.authenticate("environment-token")
        with pytest.raises(EnvironmentManagedToken, match="环境变量"):
            auth.reset_token(cookie=cookie)
        assert auth.valid(cookie)
        monkeypatch.setenv("TUBER_ADMIN_TOKEN", "changed-environment-token")
        assert not auth.valid(cookie) and not auth.authenticate("environment-token")
        assert auth.authenticate("changed-environment-token")
        monkeypatch.delenv("TUBER_ADMIN_TOKEN")
        assert auth.authenticate("changed-file-token")
    finally:
        store.close()


def test_logout_only_revokes_selected_client_and_login_rotates_previous_cookie(settings):
    auth, store = make_sessions(settings)
    try:
        one, two = auth.issue(settings.admin_token), auth.issue(settings.admin_token)
        replacement = auth.issue(settings.admin_token, previous_cookie=one)
        assert not auth.valid(one) and auth.valid(replacement) and auth.valid(two)
        auth.revoke(replacement)
        assert not auth.valid(replacement) and auth.valid(two)
        assert not auth.valid(None) and not auth.valid("not-a-session")
        assert not auth.authenticate("\ud800")
        assert not auth.authenticate({"token": settings.admin_token})
    finally:
        store.close()


def test_more_than_thirty_two_sessions_does_not_evict_existing_browser(settings):
    auth, store = make_sessions(settings)
    try:
        cookies = [auth.issue(settings.admin_token) for _ in range(40)]
        assert all(auth.valid(cookie) for cookie in cookies)
    finally:
        store.close()


def test_concurrent_login_and_reset_leave_no_valid_old_authority(settings):
    first, one_store = make_sessions(settings)
    second, two_store = make_sessions(settings)
    old, barrier = settings.admin_token, Barrier(2)

    def login():
        barrier.wait(timeout=5)
        try:
            return first.issue(old)
        except InvalidAdminCredential:
            return None

    def reset():
        barrier.wait(timeout=5)
        return second.reset_token(bearer=old)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending_login, pending_reset = pool.submit(login), pool.submit(reset)
            cookie, token = pending_login.result(timeout=10), pending_reset.result(timeout=10)
        assert first.authenticate(token) and not first.authenticate(old)
        assert not cookie or not second.valid(cookie)
    finally:
        one_store.close()
        two_store.close()


def test_cookie_renewal_is_atomic_across_workers_and_has_no_auth_expiration(settings):
    first, one_store = make_sessions(settings)
    second, two_store = make_sessions(settings)
    try:
        cookie = first.issue(settings.admin_token)
        assert not first.needs_cookie_refresh(cookie)
        one_store.execute(
            "UPDATE admin_sessions SET refreshed_at=refreshed_at-?", (COOKIE_REFRESH_SECONDS + 1,)
        )
        assert first.needs_cookie_refresh(cookie)
        assert not second.needs_cookie_refresh(cookie)
        assert second.valid(cookie)
    finally:
        one_store.close()
        two_store.close()


@pytest.mark.parametrize("filename", ["admin-token", "studio.sqlite3", "studio.sqlite3-wal"])
def test_runtime_preparation_rejects_symlinks_without_changing_target(settings, tmp_path, filename):
    path = settings.runtime / filename
    victim = tmp_path / "outside-secret"
    victim.write_bytes(b"unchanged")
    path.unlink(missing_ok=True)
    try:
        path.symlink_to(victim)
    except OSError:
        pytest.skip("Symbolic links unavailable")
    with pytest.raises(ValueError, match="符号链接"):
        AuthSessions.prepare_runtime(settings)
    assert victim.read_bytes() == b"unchanged"


@pytest.mark.skipif(os.name != "posix", reason="POSIX private mode assertion")
def test_credentials_database_and_reset_file_keep_private_modes(settings):
    AuthSessions.prepare_runtime(settings)
    auth, store = make_sessions(settings, "studio.sqlite3")
    try:
        key = auth.reset_token(bearer=settings.admin_token)
        assert auth.authenticate(key)
        for filename in ("admin-token", "studio.sqlite3", "studio.sqlite3-wal", "studio.sqlite3-shm"):
            path = settings.runtime / filename
            if path.exists():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        store.close()
