"""Content tools protect symlink referents as well as entries."""
import importlib
import os
from pathlib import Path

import pytest

from .conftest import git


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize("tool", ["write_file", "patch"])
@pytest.mark.parametrize("location", ["outside", "same-repo", "other-repo", "denied"])
def test_symlink_write(plugin, service, repo, tmp_path, tool, location):
    cli = importlib.import_module(plugin.__name__ + ".cli")
    parent = repo if location == "same-repo" else tmp_path / location
    parent.mkdir(exist_ok=True)
    if location in {"other-repo", "denied"}:
        git(parent, "init", "-b", "main")
    if location == "denied":
        service.settings["deny_toplevels"] = [str(parent)]
    victim = parent / "victim"
    victim.write_bytes(b"PREEDIT\n")
    link = repo / "link"
    link.symlink_to(victim)
    git(repo, "add", "link")
    git(repo, "commit", "-m", "Link fixture")
    hook = plugin.make_hook(service)
    assert hook(tool, {"path": str(link)}, turn_id="one") is None
    link.write_bytes(b"OVERWRITTEN\n")
    found = cli.find(service, str(link))
    cli.restore(service, str(link))
    assert victim.read_bytes() == b"OVERWRITTEN\n"
    assert link.is_symlink()
    assert found["versions"][0]["path"] == str(link)
    cli.restore(service, str(victim))
    assert victim.read_bytes() == b"PREEDIT\n"
    entry = next(v for v in cli.find(service, str(link))["versions"] if v.get("mode") == "120000")
    link.unlink()
    cli.restore(service, str(link), entry["version"])
    assert link.is_symlink() and Path(os.readlink(link)) == victim


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize("name", [".env", "vendor/hidden", ".aws/credentials"])
def test_symlink_referent_exclusions(plugin, service, repo, tmp_path, name):
    victim = tmp_path / "outside" / name
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"SYNTHETIC_EXCLUDED")
    link = repo / "link"
    link.symlink_to(victim)
    plugin.make_hook(service)("write_file", {"path": str(link)})
    assert not service.file_versions(str(victim))


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_unlink_keeps_entry_semantics(plugin, service, repo, tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b"PREEDIT")
    link = repo / "link"
    link.symlink_to(victim)
    plugin.make_hook(service)("terminal", {"command": "unlink link", "workdir": str(repo)})
    assert not service.file_versions(str(victim))
    cli = importlib.import_module(plugin.__name__ + ".cli")
    assert cli.find(service, str(link))["versions"][0]["mode"] == "120000"
