"""Bounded storage components, including metadata filenames."""
import importlib
from pathlib import Path

import pytest


@pytest.mark.parametrize("basename", ["f" * 220, "f" * 255, "界" * 80])
def test_long_copy_name(plugin, service, tmp_path, basename):
    p = tmp_path / basename
    try:
        p.write_bytes(b"PREEDIT")
    except OSError as exc:
        pytest.skip(f"Filesystem does not support fixture basename: {exc}")
    saved = service.backup_file(str(p))
    assert saved, service.read_ledger()
    assert len(Path(saved).name.encode()) <= 255
    p.unlink()
    importlib.import_module(plugin.__name__ + ".cli").restore(service, str(p))
    assert p.read_bytes() == b"PREEDIT"
