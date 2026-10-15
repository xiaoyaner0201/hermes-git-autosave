"""Git safety, storage and restore regressions. All data, filters and remotes are synthetic/local."""
import argparse
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

from .conftest import ROOT, git, shadow, tree


def module(plugin, name):
    return importlib.import_module(plugin.__name__ + "." + name)


def blob(repo, rev, path):
    return subprocess.check_output(["git", "-C", str(repo), "show", rev + ":" + path])


@pytest.mark.skipif(os.name == "nt", reason="POSIX filter executable")
@pytest.mark.parametrize("explicit", [True, False])
def test_racy_filter(service, repo, tmp_path, explicit):
    marker, victim = tmp_path / "ran", repo / "victim"
    script = tmp_path / "filter"
    script.write_text(f'#!/bin/sh\ncat >/dev/null\nprintf pointer\nprintf ran >> "{marker}"\nprintf DESTROYED > "{victim}"\n')
    script.chmod(0o755)
    git(repo, "config", "filter.demo.clean", str(script))
    git(repo, "config", "core.checkstat", "minimal")
    git(repo, "config", "core.trustctime", "false")
    (repo / ".gitattributes").write_text("payload filter=demo\n")
    p = repo / "payload"
    p.write_bytes(b"raw-original-bytes\n")
    stamp = p.stat().st_mtime_ns
    git(repo, "add", ".")
    git(repo, "commit", "-m", "filtered")
    marker.unlink(missing_ok=True)
    victim.write_bytes(b"user-data\n")
    p.write_bytes(b"new-original-bytes\n")
    os.utime(p, ns=(stamp, stamp))
    os.utime(repo / ".git/index", ns=(stamp, stamp))
    index = (repo / ".git/index").read_bytes()
    result = service.snapshot_repo(str(repo), [str(p)] if explicit else [])
    assert not result.get("error"), result
    assert not marker.exists(), "Snapshot ran a clean filter"
    assert victim.read_bytes() == b"user-data\n"
    assert (repo / ".git/index").read_bytes() == index
    assert blob(repo, shadow(repo), "payload") == b"new-original-bytes\n"


@pytest.mark.parametrize("operation", ["snapshot", "prune"])
def test_symbolic_shadow_ref(service, repo, operation):
    env = dict(os.environ, GIT_AUTHOR_DATE="2001-01-01T00:00:00+00:00", GIT_COMMITTER_DATE="2001-01-01T00:00:00+00:00")
    old = git(repo, "commit-tree", "HEAD^{tree}", "-m", "old", env=env)
    git(repo, "update-ref", "refs/heads/main", old)
    git(repo, "symbolic-ref", "refs/hermes-autosave/main", "refs/heads/main")
    (repo / "tracked.txt").write_text("dirty pre-edit")
    before = git(repo, "rev-parse", "HEAD")
    service.log({"repo": str(repo)})
    result = service.snapshot_repo(str(repo)) if operation == "snapshot" else service.prune()
    assert git(repo, "rev-parse", "--verify", "refs/heads/main") == before
    assert git(repo, "symbolic-ref", "refs/hermes-autosave/main") == "refs/heads/main"
    if operation == "prune" and (not hasattr(os, "O_NOFOLLOW") or not shutil.rmtree.avoids_symlink_attacks):
        assert result["errors"]  # Safe pruning is unavailable, so the repository was not visited.
    else:
        assert "symbolic" in json.dumps(result).lower()


def test_split_index_dependencies(service, repo):
    git(repo, "config", "core.splitIndex", "true")
    git(repo, "config", "splitIndex.sharedIndexExpire", "now")
    git(repo, "update-index", "--split-index")
    def dependencies():
        return {p.name: p.read_bytes() for p in (repo / ".git").iterdir()
                if p.name == "index" or p.name.startswith("sharedindex.")}
    before = dependencies()
    (repo / "tracked.txt").write_text("dirty")
    (repo / "new").write_text("untracked")
    result = service.snapshot_repo(str(repo))
    assert result["changed"] and not result.get("error"), result
    assert dependencies() == before
    assert "tracked.txt" in git(repo, "ls-files")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks and directory descriptors")
@pytest.mark.parametrize("component", ["root", "files", "state", "day"])
def test_prune_symlink_roots(plugin, tmp_path, component):
    outside = tmp_path / "outside"
    (outside / "20010101").mkdir(parents=True)
    sentinel = outside / "20010101/user-data"
    sentinel.write_text("external data")
    root = tmp_path / "storage"
    if component == "root":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        if component in ("files", "state"):
            (root / component).symlink_to(outside, target_is_directory=True)
        else:
            (root / "files").mkdir()
            (root / "files/20010101").symlink_to(outside / "20010101", target_is_directory=True)
    service = plugin.core.Autosave({"storage_dir": str(root)})
    result = service.prune()
    assert sentinel.read_text() == "external data"
    assert result["errors"], "Unsafe traversal must be reported"


def test_linear_parser(plugin):
    # Run out of process so the unfixed exponential regex cannot hold pytest's GIL.
    code = f'''import sys,time
sys.path.insert(0, {str(ROOT.parent)!r})
import importlib.util
s=importlib.util.spec_from_file_location("candidate", {str(ROOT / '__init__.py')!r}, submodule_search_locations=[{str(ROOT)!r}])
m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m)
from candidate.hooks import destructive
start=time.monotonic()
for _ in range(20):
    assert not destructive("git " + "--no-pager " * 38 + "status")
assert time.monotonic()-start < 1
'''
    subprocess.run([sys.executable, "-B", "-c", code], check=True, timeout=3)


def test_real_hermes_dispatcher(plugin, tmp_path):
    hermes = pytest.importorskip("hermes_cli.plugins")
    from hermes_cli.plugin_host import PluginHost
    manager = hermes.PluginManager()
    manifest = hermes.PluginManifest(name="git-autosave", version="0.3.0", requires_hermes=">=0.22.0",
                                    provides_hooks=["pre_tool_call"], path=str(ROOT), source="user")
    ctx = hermes.PluginContext(manifest, manager)
    host = PluginHost(manager)
    try:
        host.load(manifest, ctx, module_name="hermes_plugins.git_autosave", entrypoint=False)
        start = time.monotonic()
        result = manager.invoke_hook("pre_tool_call", tool_name="terminal",
                                     args={"command": "git " + "--no-pager " * 38 + "status", "workdir": str(tmp_path)},
                                     session_id="adversarial", turn_id="one", tool_call_id="call")
        assert time.monotonic() - start < 2
        assert not any(isinstance(r, dict) and r.get("action") == "block" for r in result), result
    finally:
        host.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode/symlink")
@pytest.mark.parametrize("kind", ["exec", "link", "missing-parent", "copy"])
def test_mode_aware_restore(plugin, service, repo, tmp_path, kind):
    cli = module(plugin, "cli")
    target = repo / "folder/saved" if kind == "missing-parent" else repo / "saved"
    if kind == "copy":
        target = tmp_path / "outside"
    target.parent.mkdir(parents=True, exist_ok=True)
    victim = repo / "victim"
    victim.write_bytes(b"must survive")
    if kind == "link":
        target.symlink_to("victim")
    else:
        target.write_bytes(b"pre-edit\x00\xff")
        target.chmod(0o755)
    if kind == "copy":
        service.backup_file(str(target))
    else:
        assert service.snapshot_repo(str(repo))["changed"]
    result = cli.find(service, str(target))
    assert result["versions"][0]["restore"].startswith("hermes autosave restore ")
    target.unlink()
    if kind == "missing-parent":
        target.parent.rmdir()
    elif kind in ("link", "exec", "copy"):
        target.symlink_to(victim)
    parser = argparse.ArgumentParser()
    cli.setup_cli(parser)
    args = parser.parse_args(["restore", str(target), "--version", "1", "--json"])
    assert cli.cli_handler(args, service) == 0
    assert victim.read_bytes() == b"must survive"
    if kind == "link":
        assert target.is_symlink() and os.readlink(target) == "victim"
    else:
        assert not target.is_symlink() and target.read_bytes() == b"pre-edit\x00\xff"
        assert target.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX synthetic upload-pack executable")
def test_no_lazy_fetch(service, repo, tmp_path):
    head = git(repo, "rev-parse", "HEAD")
    (repo / ".git/objects" / head[:2] / head[2:]).unlink()
    marker = tmp_path / "upload-ran"
    upload = tmp_path / "upload-pack"
    upload.write_text(f'#!/bin/sh\nprintf ran >> "{marker}"\nexit 1\n')
    upload.chmod(0o755)
    git(repo, "config", "remote.origin.url", str(repo))
    git(repo, "config", "remote.origin.promisor", "true")
    git(repo, "config", "remote.origin.partialclonefilter", "blob:none")
    git(repo, "config", "remote.origin.uploadpack", str(upload))
    (repo / "tracked.txt").write_text("dirty")
    result = service.snapshot_repo(str(repo))
    assert not marker.exists(), "Implicit fetch invoked a remote helper"
    assert result.get("error") and not result["changed"]


CREDENTIALS = [".env", ".env.local", ".netrc", ".git-credentials", ".npmrc", ".pypirc", ".ssh/config",
               ".aws/credentials", ".docker/config.json", "private.pem", "private.key", "id_rsa.pub",
               "id_ed25519", "id_ecdsa.pub", ".ENV", ".SSH/config", ".AWS/credentials", ".DOCKER/config.json",
               "PRIVATE.KEY", "AUTH.JSON"]


@pytest.mark.parametrize("name", CREDENTIALS)
def test_credentials_excluded(service, repo, tmp_path, name):
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("SYNTHETIC_SECRET_ONLY")
    assert service.backup_file(str(p)) is None
    result = service.snapshot_repo(str(repo))
    assert not result.get("error"), result
    assert name not in (tree(repo) if result["changed"] else git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
    assert service.file_versions(str(p)) == []


@pytest.mark.parametrize("name", [".env", "auth.json"])
def test_hermes_credentials(plugin, service, name):
    p = plugin.core.hermes_home() / name
    p.write_text("SYNTHETIC_SECRET_ONLY")
    plugin.make_hook(service)("write_file", {"path": str(p)})
    assert service.file_versions(str(p)) == []


@pytest.mark.parametrize("case", ["crlf", "assume-unchanged", "clean-first-edit", "filtered"])
def test_explicit_preedit(plugin, service, repo, tmp_path, case):
    p = repo / "tracked.txt"
    if case == "crlf":
        git(repo, "config", "core.autocrlf", "true")
        p.write_bytes(b"line one\r\nline two\r\n")
        git(repo, "add", "tracked.txt")
        git(repo, "commit", "-m", "normalized")
        (repo / "other").write_text("unrelated dirty file")
    elif case == "assume-unchanged":
        git(repo, "update-index", "--assume-unchanged", "tracked.txt")
        p.write_bytes(b"dirty but assumed unchanged\n")
    elif case == "filtered":
        if os.name == "nt":
            pytest.skip("POSIX synthetic filter")
        script = tmp_path / "clean"
        script.write_text("#!/bin/sh\ncat >/dev/null\nprintf pointer\\n\n")
        script.chmod(0o755)
        git(repo, "config", "filter.demo.clean", str(script))
        (repo / ".gitattributes").write_text("tracked.txt filter=demo\n")
        p.write_bytes(b"pre-edit-payload\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "filtered")
    original = p.read_bytes()
    plugin.make_hook(service)("patch", {"path": str(p)})
    found = module(plugin, "cli").find(service, str(p))
    assert found["versions"], "First clean edit must have a recovery version"
    assert blob(repo, found["versions"][0]["commit"], "tracked.txt") == original


LOCATION_COMMANDS = ["git -C {repo} checkout -- tracked.txt", "cd {repo} && rm tracked.txt",
                     "rm {file}", "mv {file} elsewhere", "cp {file} elsewhere", "truncate -s 0 {file}",
                     "dd if=input of={file}", "sed -i s/a/b/ {file}", "perl -pi -e s/a/b/ {file}",
                     "tee {file}", "printf x > {file}", "unzip -d {repo} archive.zip", "tar -xf archive.tar -C {repo}"]


@pytest.mark.parametrize("command", LOCATION_COMMANDS)
def test_terminal_location(plugin, service, repo, tmp_path, command):
    p = repo / "tracked.txt"
    p.write_bytes(b"pre-edit bytes")
    cmd = command.format(repo=str(repo), file=str(p))
    assert plugin.make_hook(service)("terminal", {"command": cmd, "workdir": str(tmp_path)}) is None
    assert service.refs(str(repo)), service.read_ledger()
    assert blob(repo, shadow(repo), "tracked.txt") == b"pre-edit bytes"


DIRECT_MUTATIONS = ["rm a\nrm b", "printf x\nrm a", "sudo -n rm a", "sudo -u root rm a", "env -i rm a",
                    "env -u X rm a", "env -- rm a", "command -- rm a", "command -p rm a", "nice rm a",
                    "nohup rm a", "time rm a", "xargs -0 rm", "git --work-tree=. checkout .",
                    "git --git-dir=.git reset --hard", "git -C . checkout .", "git -c core.autocrlf=false checkout .",
                    "git checkout-index -f -a", "git read-tree -u --reset HEAD", "git update-index --cacheinfo 100644,abc,a",
                    "git update-index --force-remove a", "git update-index --cacheinfo=100644,abc,a",
                    "git update-index --chmod +x a", "git read-tree -um HEAD", "git stash pop", "git stash apply", "git worktree remove ../wt",
                    "sed --in-place s/a/b/ a", "perl --in-place -pe s/a/b/ a", "cat <<EOF >| a\nx\nEOF",
                    "printf x >| a", "printf x &>> a", 'find . -exec sh -c "rm a" \\;', "chmod -x a", "touch a", "git checkout ."]


@pytest.mark.parametrize("command", DIRECT_MUTATIONS)
def test_direct_mutations(plugin, command):
    assert module(plugin, "hooks").destructive(command), command


def test_find_symlinked_ancestors(plugin, service, repo, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX symlink")
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    (repo / "tracked.txt").write_text("dirty")
    service.snapshot_repo(str(repo))
    found = module(plugin, "cli").find(service, str(alias / "tracked.txt"))
    assert found["versions"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX trailing pathname whitespace")
@pytest.mark.parametrize("suffix", [" ", "\n"])
def test_root_whitespace(plugin, service, tmp_path, suffix):
    repo = tmp_path / ("root" + suffix)
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "a").write_text("pre-edit")
    assert service.toplevel(str(repo / "a")) == str(repo.resolve())
    plugin.make_hook(service)("patch", {"path": str(repo / "a")})
    assert service.refs(str(repo))


@pytest.mark.parametrize("source", ["ledger", "calls"])
def test_sweep_budget(plugin, service, tmp_path, source):
    if source == "ledger":
        service.storage.mkdir()
        service.ledger.write_text((json.dumps({"at": time.time(), "kind": "file", "path": "nothing"}) + "\n") * 200000)
    else:
        dbpath = plugin.core.hermes_home() / "state.db"
        with sqlite3.connect(dbpath) as db:
            db.execute("CREATE TABLE sessions(id TEXT, started_at REAL, cwd TEXT)")
            db.execute("CREATE TABLE messages(session_id TEXT, tool_calls TEXT)")
            db.execute("INSERT INTO sessions VALUES ('s', ?, ?)", (time.time(), str(tmp_path)))
            calls = [{"function": {"name": "terminal", "arguments": {"command": "pwd"}}}] * 200000
            db.execute("INSERT INTO messages VALUES ('s', ?)", (json.dumps(calls),))
    start = time.monotonic()
    result = module(plugin, "sweep").sweep(service, 360, 0.05)
    elapsed = time.monotonic() - start
    assert result["errors"] or any("budget" in w.lower() or "limit" in w.lower() for w in result["warnings"]), result
    assert elapsed < 0.25, elapsed


def test_hyphenated_checkout(tmp_path):
    destination = tmp_path / "hermes-git-autosave"
    shutil.copytree(ROOT, destination, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".ruff_cache"))
    result = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short",
                             "tests/test_hooks.py", "--maxfail=1"], cwd=destination, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()


def test_restore_to_json_and_invalid_version(plugin, service, repo, tmp_path, capsys):
    cli = module(plugin, "cli")
    p = repo / "tracked.txt"
    p.write_bytes(b"pre-edit\x00\xff")
    service.snapshot_repo(str(repo))
    p.write_bytes(b"edited")
    destination = tmp_path / "missing/parents/restored"
    parser = argparse.ArgumentParser()
    cli.setup_cli(parser)
    cli.cli_handler(parser.parse_args(["restore", str(p), "--to", str(destination), "--json"]), service)
    result = json.loads(capsys.readouterr().out)
    assert result["restored_to"] == str(destination)
    assert destination.read_bytes() == b"pre-edit\x00\xff" and p.read_bytes() == b"edited"
    cli.cli_handler(parser.parse_args(["restore", str(p), "--version", "0", "--json"]), service)
    assert json.loads(capsys.readouterr().out)["errors"]


def test_committed_credentials_unchanged(service, repo):
    p = repo / ".env"
    p.write_bytes(b"SYNTHETIC_COMMITTED")
    git(repo, "add", ".env")
    git(repo, "commit", "-m", "synthetic committed entry")
    p.write_bytes(b"SYNTHETIC_STAGED")
    git(repo, "add", ".env")
    p.write_bytes(b"SYNTHETIC_WORKING")
    (repo / "tracked.txt").write_bytes(b"dirty")
    result = service.snapshot_repo(str(repo), [str(p)])
    assert not result.get("error"), result
    assert blob(repo, shadow(repo), ".env") == b"SYNTHETIC_COMMITTED"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink")
def test_find_keeps_final_symlink(plugin, service, repo, tmp_path):
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    link = repo / "link"
    link.symlink_to(outside, target_is_directory=True)
    service.snapshot_repo(str(repo))
    found = module(plugin, "cli").find(service, str(link))
    assert found["versions"][0]["mode"] == "120000"
    assert found["path"] == str(link)


def test_unsupported_pruning_is_refused(plugin, service, tmp_path, monkeypatch):
    old = service.files / "20010101"
    old.mkdir(parents=True)
    (old / "sentinel").write_text("saved")
    monkeypatch.setattr(plugin.core.shutil.rmtree, "avoids_symlink_attacks", False)
    result = service.prune()
    assert result["errors"] and (old / "sentinel").read_text() == "saved"


def test_real_hermes_restore(plugin, service, repo):
    pytest.importorskip("hermes_cli.plugins")
    from pathlib import Path
    executable = Path(sys.executable).parent / ("hermes.exe" if os.name == "nt" else "hermes")
    if not executable.is_file():
        pytest.skip("No Hermes console executable beside the test interpreter")
    hh = plugin.core.hermes_home()
    (hh / "plugins").mkdir()
    shutil.copytree(ROOT, hh / "plugins/git-autosave", ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    (hh / "config.yaml").write_text("updates:\n  check: false\nplugins:\n  enabled: [git-autosave]\n  entries:\n    git-autosave:\n      settings:\n        storage_dir: " + json.dumps(str(service.storage)) + "\n")
    p = repo / "tracked.txt"
    p.write_bytes(b"real CLI pre-edit\x00\xff")
    service.snapshot_repo(str(repo))
    p.unlink()
    result = subprocess.run([str(executable), "autosave", "restore", str(p), "--version", "1", "--json"],
                            capture_output=True, timeout=30, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["restored_to"] == str(p)
    assert p.read_bytes() == b"real CLI pre-edit\x00\xff"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal")
def test_files_parent_swap(service, tmp_path, monkeypatch):
    day = service.files / "20010101"
    day.mkdir(parents=True)
    (day / "plugin-owned").write_text("saved")
    outside = tmp_path / "outside"
    (outside / "20010101").mkdir(parents=True)
    sentinel = outside / "20010101/user-data"
    sentinel.write_text("external")
    original = shutil.rmtree
    swapped = False
    def attack(path, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            service.files.rename(service.storage / "pinned-files")
            service.files.symlink_to(outside, target_is_directory=True)
        return original(path, *args, **kwargs)
    attack.avoids_symlink_attacks = original.avoids_symlink_attacks
    monkeypatch.setattr(shutil, "rmtree", attack)
    result = service.prune()
    assert swapped and not result["errors"], result
    assert sentinel.read_text() == "external"
    assert not (service.storage / "pinned-files/20010101").exists()


@pytest.mark.skipif(os.name == "nt", reason="Descriptor-capable prune and POSIX Git refs")
@pytest.mark.parametrize("operation", ["snapshot", "prune"])
def test_ref_write_race(service, repo, monkeypatch, operation):
    env = dict(os.environ, GIT_AUTHOR_DATE="2001-01-01T00:00:00+00:00", GIT_COMMITTER_DATE="2001-01-01T00:00:00+00:00")
    old = git(repo, "commit-tree", "HEAD^{tree}", "-m", "old", env=env)
    git(repo, "update-ref", "refs/heads/main", old)
    git(repo, "update-ref", "--create-reflog", "refs/hermes-autosave/main", old)
    service.log({"repo": str(repo)})
    (repo / "tracked.txt").write_text("pre-edit")
    original = service.git
    attacked = False
    def race(root, args, *rest, **kwargs):
        nonlocal attacked
        if args[0] == "update-ref" and not attacked:
            attacked = True
            git(repo, "symbolic-ref", "refs/hermes-autosave/main", "refs/heads/main")
        return original(root, args, *rest, **kwargs)
    monkeypatch.setattr(service, "git", race)
    service.snapshot_repo(str(repo)) if operation == "snapshot" else service.prune()
    assert attacked
    assert git(repo, "rev-parse", "--verify", "refs/heads/main") == old


@pytest.mark.parametrize("directory,name", [(".ssh", "config"), (".aws", "credentials"), (".docker", "config.json")])
def test_credential_repo_roots(service, repo, tmp_path, directory, name):
    moved = tmp_path / directory
    repo.rename(moved)
    p = moved / name
    p.write_bytes(b"SYNTHETIC_COMMITTED")
    git(moved, "add", name)
    git(moved, "commit", "-m", "synthetic committed credential")
    p.write_bytes(b"SYNTHETIC_STAGED")
    git(moved, "add", name)
    p.write_bytes(b"SYNTHETIC_WORKING")
    (moved / "tracked.txt").write_text("dirty")
    result = service.snapshot_repo(str(moved), [str(p)])
    assert not result.get("error"), result
    assert blob(moved, result.get("commit", "HEAD"), name) == b"SYNTHETIC_COMMITTED"
    assert service.backup_file(str(p)) is None
