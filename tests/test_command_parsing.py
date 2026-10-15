"""Command parsing, copy ordering and credential exclusion regressions; synthetic local fixtures only."""
import importlib
import os
from pathlib import Path
import subprocess
import sys
import time

from .conftest import ROOT, git

import pytest


def module(plugin, name):
    return importlib.import_module(plugin.__name__ + "." + name)


@pytest.mark.parametrize("command", ["true;\nrm a", "true &&\nrm a", "cd /x &&\nrm a",
                                     "true ||\nrm a", "true |\nrm a", "printf x # note\nrm a"])
def test_newline_boundaries(plugin, tmp_path, command):
    hooks = module(plugin, "hooks")
    assert hooks.destructive(command)
    files, _ = hooks.targets("terminal", {"command": command, "workdir": str(tmp_path)})
    assert files == [plugin.core.resolve_path("a", "/x" if command.startswith("cd ") else str(tmp_path))]


def test_quoted_comments(plugin):
    hooks = module(plugin, "hooks")
    assert not hooks.destructive('printf "# rm a"')
    assert hooks.segments('printf "# literal" # comment\nrm a') == [["printf", "# literal"], ["rm", "a"]]
    assert hooks.destructive("printf x &>> a")
    assert hooks.destructive("printf x >| a")


def test_large_command_bounded(plugin):
    # Bound the unfixed shlex run out of process, rather than hang the whole suite.
    code = f'''import importlib.util,sys,time
spec=importlib.util.spec_from_file_location("candidate", {str(ROOT / '__init__.py')!r}, submodule_search_locations=[{str(ROOT)!r}])
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
from candidate.hooks import destructive,targets
command="printf "+"x"*2000000
start=time.monotonic()
assert destructive(command)
assert targets("terminal", {{"command":command,"workdir":"/initial"}}) == ([], ["/initial"])
assert time.monotonic()-start < 0.5
'''
    subprocess.run([sys.executable, "-B", "-c", code], check=True, timeout=3)


def test_large_command_logs_once(plugin, service, repo):
    command = "printf " + "x" * 2000000
    (repo / "tracked.txt").write_text("pre-edit")
    hook = plugin.make_hook(service)
    for _ in range(2):
        assert hook("terminal", {"command": command, "workdir": str(repo)}) is None
    assert service.refs(str(repo))
    assert len([r for r in service.read_ledger() if r.get("kind") == "parser"]) == 1


def test_large_command_real_host(plugin, tmp_path):
    hermes = pytest.importorskip("hermes_cli.plugins")
    from hermes_cli.plugin_host import PluginHost
    manager = hermes.PluginManager()
    manifest = hermes.PluginManifest(name="git-autosave", version="0.3.0", requires_hermes=">=0.22.0",
                                    provides_hooks=["pre_tool_call"], path=str(ROOT), source="user")
    host = PluginHost(manager)
    try:
        host.load(manifest, hermes.PluginContext(manifest, manager),
                  module_name="hermes_plugins.git_autosave", entrypoint=False)
        command = "printf " + "x" * 2000000
        start = time.monotonic()
        result = manager.invoke_hook("pre_tool_call", tool_name="terminal",
                                     args={"command": command, "workdir": str(tmp_path)},
                                     session_id="large", turn_id="one", tool_call_id="call")
        assert time.monotonic() - start < 0.5
        assert not any(isinstance(r, dict) and r.get("action") == "block" for r in result), result
    finally:
        host.shutdown()


def test_same_second_copy_order(plugin, service, tmp_path, monkeypatch):
    cli = module(plugin, "cli")
    p = tmp_path / "plain"
    stamp = time.time_ns()
    monkeypatch.setattr(plugin.core.time, "time_ns", lambda: stamp)
    monkeypatch.setattr(plugin.core.time, "strftime", lambda fmt, *args: "20261015" if fmt == "%Y%m%d" else ".123456-")
    for content in (b"first\n", b"second\n"):
        p.write_bytes(content)
        assert service.backup_file(str(p))
    versions = cli.find(service, str(p))["versions"]
    assert [Path(v["copy"]).read_bytes() for v in versions] == [b"second\n", b"first\n"]
    cli.restore(service, str(p))
    assert p.read_bytes() == b"second\n"


def test_git_copy_mix(plugin, service, repo):
    cli = module(plugin, "cli")
    p = repo / "tracked.txt"
    service.backup_file(str(p))
    p.write_bytes(b"shadow")
    assert service.snapshot_repo(str(repo))["changed"]
    p.write_bytes(b"newest copy")
    service.backup_file(str(p))
    assert [v["kind"] for v in cli.find(service, str(p))["versions"]] == ["file", "git", "file"]
    cli.restore(service, str(p))
    assert p.read_bytes() == b"newest copy"


def test_legacy_copy_order(plugin, service, tmp_path):
    cli = module(plugin, "cli")
    p = tmp_path / "legacy"
    parent = service.files / "20261015" / service.copy_relative(str(p)).parent
    parent.mkdir(parents=True)
    old = parent / "legacy.123456-ffffffff"
    new = parent / "legacy.123456-00000000"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    import os
    os.utime(old, ns=(1000000000, 1000000000))
    os.utime(new, ns=(2000000000, 2000000000))
    cli.restore(service, str(p))
    assert p.read_bytes() == b"new"


VENDOR_CREDENTIALS = [".claude/.credentials.json", ".config/gcloud/application_default_credentials.json",
                      ".config/gcloud/credentials.db", ".config/gcloud/access_tokens.db",
                      ".azure/accessTokens.json", ".azure/msal_token_cache.bin", ".azure/msal_token_cache.json",
                      ".aws/config", ".kube/config", ".config/gh/hosts.yml"]


@pytest.mark.parametrize("name", VENDOR_CREDENTIALS)
def test_vendor_credentials(plugin, service, repo, name):
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"SYNTHETIC_UNTRACKED")
    result = service.snapshot_repo(str(repo), [str(p)])
    assert not result.get("error"), result
    assert name not in git(repo, "ls-tree", "-r", "--name-only", result.get("commit", "HEAD")).splitlines()
    assert service.backup_file(str(p)) is None
    git(repo, "add", name)
    git(repo, "commit", "-m", "Synthetic credential baseline")
    p.write_bytes(b"SYNTHETIC_STAGED")
    git(repo, "add", name)
    p.write_bytes(b"SYNTHETIC_WORKING")
    (repo / "tracked.txt").write_text("dirty")
    result = service.snapshot_repo(str(repo), [str(p)])
    assert not result.get("error"), result
    assert git(repo, "show", result.get("commit", "HEAD") + ":" + name) == "SYNTHETIC_UNTRACKED"


@pytest.mark.parametrize("directory,name", [(".aws", "credentials"), (".ssh", "config"),
                                            (".config/gcloud", "credentials.db"), (".azure", "accessTokens.json"),
                                            (".kube", "config"), (".config/gh", "hosts.yml"),
                                            (".claude", ".credentials.json")])
def test_credential_ancestors(plugin, service, repo, tmp_path, directory, name):
    parent = tmp_path / "home" / directory
    parent.mkdir(parents=True, exist_ok=True)
    moved = parent / "nested"
    repo.rename(moved)
    p = moved / name
    p.write_text("SYNTHETIC_COMMITTED")
    git(moved, "add", name)
    git(moved, "commit", "-m", "Synthetic credential baseline")
    p.write_text("SYNTHETIC_STAGED")
    git(moved, "add", name)
    p.write_text("SYNTHETIC_WORKING")
    (moved / "tracked.txt").write_text("dirty")
    result = service.snapshot_repo(str(moved), [str(p)])
    assert not result.get("error"), result
    assert git(moved, "show", result.get("commit", "HEAD") + ":" + name) == "SYNTHETIC_COMMITTED"
    assert service.backup_file(str(p)) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor prune")
@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_prune_root_swap(plugin, service, tmp_path, monkeypatch, replacement):
    day = service.files / "20010101"
    day.mkdir(parents=True)
    (day / "saved").write_text("owned")
    outside = tmp_path / "outside"
    (outside / "state").mkdir(parents=True)
    (outside / "ledger.jsonl").write_text("EXTERNAL_DATA_MUST_SURVIVE\n")
    original = plugin.core.shutil.rmtree
    attacked = False

    def race(path, *args, **kwargs):
        nonlocal attacked
        if not attacked:
            attacked = True
            service.storage.rename(tmp_path / "pinned-root")
            if replacement == "symlink":
                service.storage.symlink_to(outside, target_is_directory=True)
            else:
                outside.rename(service.storage)
        return original(path, *args, **kwargs)

    race.avoids_symlink_attacks = original.avoids_symlink_attacks
    monkeypatch.setattr(plugin.core.shutil, "rmtree", race)
    result = service.prune()
    external = outside if replacement == "symlink" else service.storage
    assert attacked
    assert (external / "ledger.jsonl").read_text() == "EXTERNAL_DATA_MUST_SURVIVE\n"
    assert not (external / "state/ledger.lock").exists()
    assert result["errors"], result


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor prune")
@pytest.mark.parametrize("name", ["ledger.jsonl", "state/ledger.lock"])
def test_prune_ledger_symlinks(plugin, service, tmp_path, name):
    service.state.mkdir(parents=True)
    outside = tmp_path / "external"
    outside.write_text("EXTERNAL_DATA_MUST_SURVIVE\n")
    (service.storage / name).symlink_to(outside)
    result = service.prune()
    assert outside.read_text() == "EXTERNAL_DATA_MUST_SURVIVE\n"
    assert (service.storage / name).is_symlink()
    assert result["errors"], result


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("target", ["directory", "absent-file"])
def test_sparse_checkout(plugin, service, repo, scoped, target):
    for name in ("keep/a", "gone/b"):
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("sparse baseline")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Sparse fixture")
    git(repo, "sparse-checkout", "init", "--cone")
    git(repo, "sparse-checkout", "set", "keep")
    service.settings["big_repo_entries"] = 1 if scoped else 20000
    before = (repo / ".git/index").read_bytes()
    (repo / "keep/a").write_text("dirty")
    selected = repo if target == "directory" else repo / "gone/b"
    result = service.snapshot_repo(str(repo), [str(selected)])
    assert not result.get("error"), result
    assert git(repo, "show", result.get("commit", "HEAD") + ":gone/b") == "sparse baseline"
    assert (repo / ".git/index").read_bytes() == before


@pytest.mark.parametrize("scoped", [False, True])
def test_skip_worktree_present(plugin, service, repo, scoped):
    p = repo / "tracked.txt"
    git(repo, "update-index", "--skip-worktree", "tracked.txt")
    p.write_text("unsaved real bytes")
    (repo / "other").write_text("force shadow creation")
    service.settings["big_repo_entries"] = 1 if scoped else 20000
    implicit = service.snapshot_repo(str(repo), [str(repo)])
    assert not implicit.get("error"), implicit
    assert git(repo, "show", implicit.get("commit", "HEAD") + ":tracked.txt") == "baseline"
    explicit = service.snapshot_repo(str(repo), [str(p)])
    assert not explicit.get("error"), explicit
    assert git(repo, "show", explicit.get("commit", "HEAD") + ":tracked.txt") == "unsaved real bytes"


@pytest.mark.parametrize("option", ["-C {repo}", "--chdir {repo}", "--chdir={repo}", "-C{repo}"])
def test_env_cwd(plugin, service, repo, tmp_path, option):
    hooks = module(plugin, "hooks")
    command = "env " + option.format(repo=repo) + " rm tracked.txt; rm untouched"
    files, _ = hooks.targets("terminal", {"command": command, "workdir": str(tmp_path)})
    assert files == [str(repo / "tracked.txt"), str(tmp_path / "untouched")]
    assert hooks.terminal_cwd({"command": command, "workdir": str(tmp_path)}) == str(repo)
    (repo / "tracked.txt").write_text("pre-edit")
    assert plugin.make_hook(service)("terminal", {"command": command, "workdir": str(tmp_path)}) is None
    assert service.refs(str(repo))


@pytest.mark.parametrize("option", ["-t {repo}", "--target-directory {repo}", "--target-directory={repo}", "-t{repo}"])
def test_install_target(plugin, service, repo, tmp_path, option):
    command = "install " + option.format(repo=repo) + " src"
    files, _ = module(plugin, "hooks").targets("terminal", {"command": command, "workdir": str(tmp_path)})
    assert str(repo) in files
    (repo / "tracked.txt").write_text("pre-edit")
    assert plugin.make_hook(service)("terminal", {"command": command, "workdir": str(tmp_path)}) is None
    assert service.refs(str(repo))


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable modes")
def test_copy_mode_dedupe(plugin, service, tmp_path):
    p = tmp_path / "mode-only"
    p.write_bytes(b"same bytes")
    p.chmod(0o644)
    assert service.backup_file(str(p))
    p.chmod(0o755)
    assert service.backup_file(str(p)), "Mode-only change must have its own version"
    assert service.backup_file(str(p)) is None
    p.unlink()
    module(plugin, "cli").restore(service, str(p))
    assert p.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent symlink")
def test_restore_to_parent_symlink(plugin, service, tmp_path):
    p = tmp_path / "source"
    p.write_bytes(b"saved")
    service.backup_file(str(p))
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    module(plugin, "cli").restore(service, str(p), destination=str(alias / "destination"))
    assert (outside / "destination").read_bytes() == b"saved"
