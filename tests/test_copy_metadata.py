"""Restoration metadata belongs to the copy, not the ledger."""
import importlib
import os

import pytest


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode and flock")
def test_copy_metadata_without_ledger(plugin, service, tmp_path):
    p = tmp_path / "executable"
    p.write_bytes(b"PREEDIT")
    p.chmod(0o755)
    with plugin.core.locked(service.state / "ledger.lock"):
        assert service.backup_file(str(p))
    assert not service.read_ledger()
    p.unlink()
    cli = importlib.import_module(plugin.__name__ + ".cli")
    cli.restore(service, str(p))
    assert p.read_bytes() == b"PREEDIT"
    assert p.stat().st_mode & 0o777 == 0o755
    version = cli.find(service, str(p))["versions"][0]
    assert version["content_id"]
    assert version["at_ns"] > 0


def test_incomplete_copy_not_published(plugin, service, tmp_path, monkeypatch):
    p = tmp_path / "original"
    p.write_bytes(b"PREEDIT")
    def fail(*args, **kwargs):
        raise OSError("Metadata unavailable")
    monkeypatch.setattr(plugin.core, "atomic_json", fail)
    assert service.backup_file(str(p)) is None
    assert not service.file_versions(str(p))
    assert "Metadata unavailable" in service.read_ledger()[-1]["error"]
