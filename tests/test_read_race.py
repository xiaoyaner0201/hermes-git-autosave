"""Regular->FIFO/link replacement must never be read or marked covered."""
import os

import pytest

from .conftest import git


@pytest.mark.parametrize("backend", ["git", "copy"])
@pytest.mark.parametrize("replacement", ["fifo", "symlink"])
def test_open_revalidates_entry(plugin, service, repo, tmp_path, monkeypatch, backend, replacement):
    target = repo / "tracked.txt" if backend == "git" else tmp_path / "standalone"
    target.write_bytes(b"pre-edit")
    secret = tmp_path / ".env"
    secret.write_bytes(b"SYNTHETIC-DO-NOT-COPY")
    original = plugin.core.os.open
    swapped = False
    def race(path, flags, *args, **kwargs):
        nonlocal swapped
        if os.fspath(path) == str(target) and not swapped:
            swapped = True
            target.unlink()
            if replacement == "fifo":
                os.mkfifo(target)
            else:
                target.symlink_to(secret)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(plugin.core.os, "open", race)
    if backend == "git":
        result = service.snapshot_repo(str(repo), [str(target)])
        assert str(target) not in result.get("covered_paths", [])
        assert "SYNTHETIC-DO-NOT-COPY" not in git(repo, "show", "refs/hermes-autosave/main:tracked.txt")
    else:
        assert service.backup_file(str(target)) is None
        assert not service.file_versions(str(target))
    assert swapped
