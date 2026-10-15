"""Restoring a link never writes its referent."""
import importlib
import json
import os

import pytest

from .conftest import ROOT, git


def cli_for(plugin):
    return importlib.import_module(plugin.__name__ + ".cli")


@pytest.mark.parametrize("operation", ["Update\tFile", "Add  File", "Delete\tFile", "Move\tFile", "Move File"])
@pytest.mark.parametrize("arrow", ["->", "  ->\t"])
def test_v4a_header_whitespace(plugin, tmp_path, operation, arrow):
    hooks = importlib.import_module(plugin.__name__ + ".hooks")
    source, destination = tmp_path / "source", tmp_path / "destination"
    moving = operation.startswith("Move")
    operand = str(source) + arrow + str(destination) if moving else str(source)
    args = {"mode": "patch", "patch": f"*** Begin Patch\n*** {operation}: {operand}\n*** End Patch"}
    paths, _ = hooks.targets("patch", args)
    expected = [str(source), str(destination)] if moving else [str(source)]
    assert paths == expected
    content, _ = hooks.targets("patch", args, content_only=True)
    assert content == ([str(source)] if operation.split()[0] in {"Update", "Add"} else [])


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize("operation", ["write_file", "patch", "redirect", "tee", "sed"])
def test_independent_referent(plugin, service, repo, tmp_path, operation):
    victim = tmp_path / "victim"
    victim.write_text("PREEDIT\n")
    link = repo / "link"
    link.symlink_to(victim)
    git(repo, "add", "link")
    git(repo, "commit", "-m", "link")
    if operation in {"write_file", "patch"}:
        tool, args = operation, {"path": str(link)}
    else:
        command = {"redirect": "printf x > link", "tee": "printf x | tee link",
                   "sed": "sed -i '' s/a/b/ link"}[operation]
        tool, args = "terminal", {"command": command, "workdir": str(repo)}
    plugin.make_hook(service)(tool, args, turn_id="one")
    victim.write_text("IMPORTANT-NOW\n")
    cli = cli_for(plugin)
    found = cli.find(service, str(link))
    assert all(v["path"] == str(link) for v in found["versions"])
    assert str(victim) in " ".join(found.get("notes", []))
    cli.restore(service, str(link))
    assert victim.read_text() == "IMPORTANT-NOW\n"
    cli.restore(service, str(victim))
    assert victim.read_text() == "PREEDIT\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_stale_link_is_not_restore_alias(plugin, service, repo, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("OLD")
    link = repo / "link"
    link.symlink_to(victim)
    hook = plugin.make_hook(service)
    hook("write_file", {"path": str(link)}, turn_id="one")
    link.unlink()
    link.write_text("REGULAR-PREEDIT")
    victim.write_text("UNRELATED")
    hook("write_file", {"path": str(link)}, turn_id="two")
    link.write_text("OVERWRITTEN")
    cli_for(plugin).restore(service, str(link))
    assert victim.read_text() == "UNRELATED"
    assert link.read_text() == "REGULAR-PREEDIT"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize("denied", [False, True])
def test_outside_link_has_no_entry_version(plugin, service, repo, tmp_path, denied):
    victim = tmp_path / "victim"
    victim.write_text("PREEDIT")
    link = (repo if denied else tmp_path) / "link"
    link.symlink_to(victim)
    if denied:
        service.settings["deny_toplevels"] = [str(repo)]
    plugin.make_hook(service)("write_file", {"path": str(link)})
    cli = cli_for(plugin)
    assert cli.find(service, str(link))["versions"] == []
    assert cli.find(service, str(victim))["versions"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize("operation", ["Delete", "Move"])
def test_real_host_patch_entry(plugin, repo, tmp_path, monkeypatch, operation):
    hermes = pytest.importorskip("hermes_cli.plugins")
    from hermes_cli.plugin_host import PluginHost
    from tools.file_tools import patch_tool

    monkeypatch.setenv("TERMINAL_CWD", str(repo))
    victim = tmp_path / "victim"
    victim.write_text("PREEDIT\n")
    link, moved = repo / "link", repo / "moved"
    link.symlink_to(victim)
    git(repo, "add", "link")
    git(repo, "commit", "-m", "link")
    manager = hermes.PluginManager()
    manifest = hermes.PluginManifest(name="git-autosave", version="0.3.0", requires_hermes=">=0.22.0",
                                    provides_hooks=["pre_tool_call"], path=str(ROOT), source="user")
    host = PluginHost(manager)
    try:
        host.load(manifest, hermes.PluginContext(manifest, manager),
                  module_name="hermes_plugins.git_autosave", entrypoint=False)
        header = f"*** Delete File: {link}" if operation == "Delete" else f"*** Move\tFile: {link}->{moved}"
        args = {"mode": "patch", "patch": f"*** Begin Patch\n{header}\n*** End Patch"}
        assert manager.invoke_hook("pre_tool_call", tool_name="patch", args=args,
                                   session_id="r7", turn_id=operation, tool_call_id="patch") == []
        result = json.loads(patch_tool(**args, task_id="r7-" + operation))
        assert result.get("success"), result
        assert not os.path.lexists(link)
        if operation == "Move":
            assert moved.is_symlink()
        service = plugin.core.Autosave()
        assert not service.file_versions(str(victim)), "Delete/Move must not observe referent content"
        victim.write_text("UNRELATED\n")
        cli_for(plugin).restore(service, str(link))
        assert link.is_symlink()
        assert victim.read_text() == "UNRELATED\n"
    finally:
        host.shutdown()
