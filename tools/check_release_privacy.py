"""Audit unpublished commit snapshots and local release metadata without echoing values."""

import argparse
import ipaddress
import re
import subprocess
import tarfile
from pathlib import Path

TAILNET = ipaddress.ip_network("100.64.0.0/10")
# Reserved in this repository for synthetic connection tests.
TAILNET_FIXTURE = TAILNET.network_address + 42
PRIVATE_ADDRESS = re.compile(
    r"(?<![\w.])(?:100|10|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))"
    r"(?:\.\d{1,3}){2,3}(?![\w.])"
)
TAILNET_NAME = re.compile(r"\b[a-z0-9][a-z0-9._-]+\.ts\.net\b", re.IGNORECASE)
TAILNET_V6 = re.compile(r"\bfd7a:115c:a1e0:[0-9a-f:]+", re.IGNORECASE)
HOME_PATH = re.compile(
    r"\b[a-z]:[/\\]+(?:Users|Code)[/\\]+[^/\\\s\"']+", re.IGNORECASE
)
POSIX_HOME = re.compile(r"(?<![\w./-])/(?:home|Users)/([\w.-]+)", re.IGNORECASE)
BUILD_USERS = {"app", "build", "ci", "runner", "tuber", "user"}
RECORDED_UID = re.compile(
    r'"(?:account_uid|account_id|recipient_uid|platform_user_id|self_uid|own_uid|author_uid|owner_uid|'
    r'parent_author_uid)"\s*:\s*"?'
    r"([1-9]\d{5,})(?!\d)"
)
ACCOUNT_PROFILE = re.compile(
    r'"(?:account_display_name|account_bio|account_name|self_name)"\s*:\s*"([^"]+)"'
)
SCRUBBED_PROFILE = "账号资料（已脱敏）"
GREP = (
    r"100\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+"
    r"|172\.[0-9]+\.[0-9]+\.[0-9]+|\.ts\.net|fd7a:115c:a1e0|[A-Z]:[\\/](Users|Code)"
    r'|/(home|Users)/|"(account_uid|account_id|recipient_uid|platform_user_id|self_uid|own_uid|author_uid|'
    r'owner_uid|parent_author_uid|account_display_name|account_bio|account_name|self_name)"'
)
METADATA_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".toml", ".yaml", ".yml", ".svg", ".csv"}


def categories(line, *, evidence=False, fixture=False):
    for match in PRIVATE_ADDRESS.finditer(line):
        try:
            address = ipaddress.ip_address(match[0])
        except ValueError:
            continue
        if address == TAILNET.network_address and line[match.end():].startswith("/10"):
            continue
        if fixture and (address == TAILNET_FIXTURE or address not in TAILNET):
            continue
        if address in TAILNET or address.is_private:
            yield "private-address"
            break
    if TAILNET_NAME.search(line) or TAILNET_V6.search(line):
        yield "tailnet-identifier"
    if HOME_PATH.search(line) or any(m[1].casefold() not in BUILD_USERS for m in POSIX_HOME.finditer(line)):
        yield "personal-path"
    if evidence and any(
        not 900_000_000_000_000 <= int(match[1]) < 920_000_000_000_000
        for match in RECORDED_UID.finditer(line)
    ):
        yield "account-identifier"
    if evidence and any(match[1] != SCRUBBED_PROFILE for match in ACCOUNT_PROFILE.finditer(line)):
        yield "account-profile"


def git(*args, allow_empty=False):
    result = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode and not (allow_empty and result.returncode == 1):
        raise ValueError("Git privacy scan could not read the requested snapshots")
    return result.stdout


def check_identity(ref):
    addresses = git("show", "-s", "--format=%ae%n%ce", ref).splitlines()
    if any(not (address.endswith(".example") or address.endswith("@users.noreply.github.com")) for address in addresses):
        yield f"{ref[:12]} commit metadata [personal-email]"


def check_ref(ref):
    prefix = ref + ":"
    for entry in git("grep", "-n", "-I", "-i", "-E", GREP, ref, allow_empty=True).splitlines():
        path, number, line = entry.removeprefix(prefix).split(":", 2)
        for category in categories(
            line, evidence=path.startswith("evaluations/results/"), fixture=path.startswith("tests/")
        ):
            yield f"{ref[:12]} {path}:{number} [{category}]"


def check_metadata(name, data):
    for number, line in enumerate(data.splitlines(), 1):
        for category in categories(line, evidence=True):
            yield f"{name}:{number} [{category}]"


def check_artifact(path):
    files = [path] if path.is_file() else sorted(path.rglob("*"))
    for item in files:
        if not item.is_file():
            continue
        if item.suffix.lower() in METADATA_SUFFIXES:
            yield from check_metadata(item.name, item.read_text(encoding="utf-8", errors="replace"))
        elif item.name.endswith((".tar", ".tar.gz", ".tgz")):
            with tarfile.open(item) as archive:
                for member in archive:
                    if member.isfile() and Path(member.name).suffix.lower() in METADATA_SUFFIXES:
                        data = archive.extractfile(member).read().decode("utf-8", errors="replace")
                        yield from check_metadata(f"{item.name}/{Path(member.name).name}", data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--ref", default="HEAD")
    selection.add_argument("--range", dest="revision_range")
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    for path in args.artifact:
        if not path.exists():
            parser.error("An artifact path does not exist")
    try:
        if args.revision_range:
            refs = git("rev-list", args.revision_range).splitlines()
        else:
            refs = [git("rev-parse", "--verify", args.ref).strip()]
        if not refs:
            parser.error("The revision range is empty")
        findings = [finding for ref in refs for finding in check_ref(ref)]
        if args.revision_range:
            findings.extend(finding for ref in refs for finding in check_identity(ref))
        findings.extend(finding for path in args.artifact for finding in check_artifact(path))
    except (ValueError, tarfile.TarError) as error:
        parser.error(str(error))
    for finding in findings:
        print(finding)
    print(f"Privacy check: {len(refs)} snapshot(s), {len(findings)} finding(s)")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
