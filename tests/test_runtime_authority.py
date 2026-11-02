import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tuber_studio.runtime_authority import (
    AUTHORITY_FILE,
    BusinessAuthorityError,
    acquire_business_writer,
    assert_business_writer,
    read_business_authority,
    set_business_authority,
)


def test_existing_installation_and_explicit_cutover_preserve_data(tmp_path):
    (tmp_path / "original.txt").write_text("keep")
    assert_business_writer(tmp_path)
    set_business_authority(tmp_path, "retired", migration_id="move-1", destination="other-host")
    with pytest.raises(BusinessAuthorityError, match="staged or retired"):
        assert_business_writer(tmp_path)
    assert (tmp_path / "original.txt").read_text() == "keep"
    set_business_authority(tmp_path, "active", migration_id="rollback-1")
    assert_business_writer(tmp_path)
    assert read_business_authority(tmp_path)["writer_host"] == socket.gethostname()


def test_running_writer_excludes_second_writer_and_retirement(tmp_path):
    with acquire_business_writer(tmp_path):
        with pytest.raises(BusinessAuthorityError, match="running writer"):
            with acquire_business_writer(tmp_path):
                pytest.fail("a second writer entered")
        with pytest.raises(BusinessAuthorityError, match="running writer"):
            set_business_authority(tmp_path, "retired", migration_id="bad-transition")
    with acquire_business_writer(tmp_path):
        pass


def test_host_binding_rejects_a_copied_active_directory(tmp_path):
    set_business_authority(tmp_path, "active", migration_id="move", writer_host="different-host")
    with pytest.raises(BusinessAuthorityError, match="different writer host"):
        assert_business_writer(tmp_path)


def test_staged_restores_cannot_start_until_activated(tmp_path):
    set_business_authority(tmp_path, "staged", migration_id="move")
    with pytest.raises(BusinessAuthorityError):
        with acquire_business_writer(tmp_path):
            pytest.fail("staged data started")
    set_business_authority(tmp_path, "active", migration_id="move")
    with acquire_business_writer(tmp_path):
        pass


def test_corrupt_authority_does_not_silently_return_to_legacy(tmp_path):
    (tmp_path / AUTHORITY_FILE).write_text(json.dumps({"state": "active"}))
    with pytest.raises(BusinessAuthorityError, match="Invalid"):
        assert_business_writer(tmp_path)


def test_os_lease_excludes_an_independent_process_and_is_released_on_exit(tmp_path):
    source = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys; from pathlib import Path; "
        "from tuber_studio.runtime_authority import acquire_business_writer; "
        "lease=acquire_business_writer(Path(sys.argv[1])); lease.__enter__()"
    )
    options = {"env": {**os.environ, "PYTHONPATH": str(source)}, "capture_output": True, "timeout": 15}
    with acquire_business_writer(tmp_path):
        refused = subprocess.run([sys.executable, "-c", code, str(tmp_path)], **options)
        assert refused.returncode != 0
        assert b"running writer" in refused.stderr
    accepted = subprocess.run([sys.executable, "-c", code, str(tmp_path)], **options)
    assert accepted.returncode == 0, accepted.stderr
    with acquire_business_writer(tmp_path):
        pass
