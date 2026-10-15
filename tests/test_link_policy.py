"""Exclusions survive every link hop; legacy copies retain path identity."""
import importlib
import json
import os

import pytest

from .conftest import git


def test_lexical_policy_before_dot_collapse(plugin, service, repo, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(repo))
    (repo / "vendor").mkdir()
    victim = repo / "tracked.txt"
    victim.write_text("SYNTHETIC-EXCLUDED")
    plugin.make_hook(service)("write_file", {"path": "vendor/../tracked.txt"})
    assert not service.refs(str(repo)), "Lexical exclusions must run before dot-segment collapse"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize("middle_name", [".aws/credentials", "vendor/dep", "node_modules/item"])
@pytest.mark.parametrize("shape", ["file-hop", "directory-hop", "copy-parent"])
def test_intermediate_exclusions(plugin, service, repo, tmp_path, middle_name, shape):
    victim = tmp_path / "ordinary"
    if shape == "file-hop":
        victim.write_text("SYNTHETIC-EXCLUDED")
        middle = tmp_path / "chain" / middle_name
        middle.parent.mkdir(parents=True)
        middle.symlink_to(victim)
        link = repo / "alias"
        link.symlink_to(middle)
        plugin.make_hook(service)("write_file", {"path": str(link)})
    else:
        victim.mkdir()
        target = victim / "plain"
        target.write_text("SYNTHETIC-EXCLUDED")
        # The directory's name is policy-sensitive even though the final physical path is ordinary.
        middle = tmp_path / "chain" / middle_name.rsplit("/", 1)[0]
        middle.parent.mkdir(parents=True, exist_ok=True)
        middle.symlink_to(victim, target_is_directory=True)
        alias = repo / "alias"
        alias.symlink_to(middle, target_is_directory=True)
        if shape == "copy-parent":
            assert service.backup_file(str(alias / "plain")) is None
        else:
            plugin.make_hook(service)("write_file", {"path": str(alias / "plain")})
        victim = target
    assert service.file_versions(str(victim)) == []
    if shape != "file-hop":
        assert not any(r.get("changed") for r in service.read_ledger() if r.get("kind") == "repo")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink ancestors")
def test_scoped_target_checks_lexical_hops(plugin, service, repo, tmp_path):
    real = repo / "ordinary"
    real.mkdir()
    victim = real / "plain"
    victim.write_text("SYNTHETIC-EXCLUDED")
    middle = tmp_path / ".aws"
    middle.symlink_to(real, target_is_directory=True)
    alias = repo / "alias"
    alias.symlink_to(middle, target_is_directory=True)
    service.settings["big_repo_entries"] = 1
    git(repo, "add", "ordinary/plain", "alias")
    git(repo, "commit", "-m", "alias fixture")
    result = service.snapshot_repo(str(repo), [str(alias / "plain")])
    assert not result.get("error"), result
    cli = importlib.import_module(plugin.__name__ + ".cli")
    assert cli.find(service, str(victim))["versions"] == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink loop")
def test_exclusion_traversal_bounded(plugin, service, repo):
    a, b = repo / "a", repo / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    assert plugin.make_hook(service)("write_file", {"path": str(a)}) is None
    assert not service.file_versions(str(a))


@pytest.mark.skipif(os.name == "nt", reason="POSIX cyclic link entry")
def test_patch_delete_cyclic_entry(plugin, service, repo):
    link = repo / "cyclic"
    link.symlink_to(link.name)
    patch = f"*** Begin Patch\n*** Delete File: {link}\n*** End Patch"
    plugin.make_hook(service)("patch", {"mode": "patch", "patch": patch})
    cli = importlib.import_module(plugin.__name__ + ".cli")
    assert cli.find(service, str(link))["versions"][0]["mode"] == "120000"
    link.unlink()
    cli.restore(service, str(link))
    assert link.is_symlink() and os.readlink(link) == link.name


@pytest.mark.parametrize("layout", ["legacy", "v1"])
@pytest.mark.parametrize("identity_source", ["metadata", "ledger"])
def test_legacy_conflicting_identity(plugin, service, tmp_path, layout, identity_source):
    wanted, different = tmp_path / "v1-foo", tmp_path / "foo"
    parent = service.files / "20261015" / service.copy_relative(str(wanted)).parent
    parent.mkdir(parents=True)
    name = "v1-foo.123456-00000000" if layout == "legacy" else "v1-v1-foo.123456-00000000"
    copy = parent / name
    copy.write_bytes(b"OTHER-PATH-CONTENT")
    record = {"kind": "file", "copy": str(copy), "path": str(different), "mode": 0o644}
    if identity_source == "metadata":
        copy.with_name(copy.name + ".json").write_text(json.dumps(record))
    else:
        service.log(record)
    cli = importlib.import_module(plugin.__name__ + ".cli")
    assert cli.find(service, str(wanted))["versions"] == []
