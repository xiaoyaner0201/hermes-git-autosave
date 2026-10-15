"""Recurrences are new observations, not historical duplicates."""
import importlib
import os

import pytest


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
@pytest.mark.parametrize("variant", ["content", "mode"])
def test_copy_cycle(plugin, service, tmp_path, variant):
    p = tmp_path / "cycle"
    for content, mode in ((b"A", 0o644), (b"B" if variant == "content" else b"A", 0o755), (b"A", 0o644)):
        p.write_bytes(content)
        p.chmod(mode)
        assert service.backup_file(str(p)), "Each transition is a new observation"
    assert service.backup_file(str(p)) is None
    cli = importlib.import_module(plugin.__name__ + ".cli")
    versions = cli.find(service, str(p))["versions"]
    assert len(versions) == 3
    assert len({v["at_ns"] for v in versions}) == 3
    p.unlink()
    cli.restore(service, str(p))
    assert p.read_bytes() == b"A"
    assert p.stat().st_mode & 0o777 == 0o644
