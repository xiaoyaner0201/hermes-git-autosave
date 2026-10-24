import builtins
import json
import os
from pathlib import Path
import time

import pytest

from .conftest import git, inject_worker, load_plugin, shadow, tree


def test_index_status_head_unchanged(service, repo):
    (repo / "tracked.txt").write_text("dirty working tree\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    git(repo, "add", "staged.txt")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    index = (repo / ".git" / "index").read_bytes()
    status = git(repo, "status", "--porcelain")
    head = git(repo, "rev-parse", "HEAD")
    branches = git(repo, "show-ref", "--heads")
    result = service.snapshot_repo(str(repo), [str(repo / "tracked.txt")])
    assert not result.get("error"), result
    assert result["changed"]
    assert (repo / ".git" / "index").read_bytes() == index
    assert git(repo, "status", "--porcelain") == status
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "show-ref", "--heads") == branches
    assert git(repo, "show", shadow(repo) + ":tracked.txt") == "dirty working tree"
    assert git(repo, "show", shadow(repo) + ":staged.txt") == "staged"
    assert git(repo, "show", shadow(repo) + ":untracked.txt") == "untracked"
    assert git(repo, "reflog", "show", "refs/hermes-autosave/main")


def test_same_turn_and_no_change(plugin, service, repo):
    hook = plugin.make_hook(service)
    p = repo / "tracked.txt"
    p.write_text("first\n", encoding="utf-8")
    assert hook("patch", {"path": str(p)}, task_id="s", turn_id="t") is None
    before = shadow(repo)
    p.write_text("second\n", encoding="utf-8")
    hook("patch", {"path": str(p)}, task_id="s", turn_id="t")
    assert shadow(repo) == before
    hook("patch", {"path": str(p)}, task_id="s", turn_id="next")
    after = shadow(repo)
    assert before != after
    result = service.snapshot_repo(str(repo), [str(p)])
    assert not result["changed"] and not result.get("error")
    assert shadow(repo) == after


@pytest.mark.parametrize("name", sorted(load_plugin("directory_constants").core.DEP_DIRS))
def test_dependencies_excluded(service, repo, name):
    folder = repo / name
    folder.mkdir()
    (folder / "tracked.dat").write_text("dependency", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Tracked dependency")
    (folder / "untracked.dat").write_text("dependency", encoding="utf-8")
    (repo / "tracked.txt").write_text("changed", encoding="utf-8")
    result = service.snapshot_repo(str(repo), [str(folder / "tracked.dat")])
    assert not result.get("error"), result
    assert not any(p.startswith(name + "/") for p in tree(repo))
    assert service.backup_file(str(folder / "tracked.dat")) is None


@pytest.mark.parametrize("name", sorted(load_plugin("build_constants").core.BUILD_DIRS))
def test_build_tracked_kept_untracked_skipped(service, repo, name):
    folder = repo / name
    folder.mkdir()
    (folder / "tracked.dat").write_text("initial", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Tracked build artifact")
    (folder / "tracked.dat").write_text("updated", encoding="utf-8")
    (folder / "untracked.dat").write_text("skip", encoding="utf-8")
    result = service.snapshot_repo(str(repo), [str(folder / "tracked.dat")])
    assert not result.get("error"), result
    assert name + "/tracked.dat" in tree(repo)
    assert name + "/untracked.dat" not in tree(repo)
    assert git(repo, "show", shadow(repo) + ":" + name + "/tracked.dat") == "updated"
    assert service.backup_file(str(folder / "tracked.dat")) is None


@pytest.mark.parametrize("kind", ["hermes", "profile", "home", "ancestor", "custom"])
def test_denied_top_level_file_only(plugin, service, tmp_path, kind):
    hh = plugin.core.hermes_home()
    p = {"hermes": hh, "profile": hh / "profiles" / "test-profile", "home": Path.home(),
         "ancestor": tmp_path, "custom": tmp_path / "custom"}[kind]
    p.mkdir(parents=True, exist_ok=True)
    git(p, "init", "-b", "main")
    if kind == "custom":
        service.settings["deny_toplevels"] = [str(p)]
    target = p / "note.txt"
    target.write_text("before", encoding="utf-8")
    hook = plugin.make_hook(service)
    assert hook("write_file", {"path": str(target)}, turn_id="turn") is None
    assert git(p, "for-each-ref", "refs/hermes-autosave/") == ""
    assert len(service.file_versions(str(target))) == 1
    assert service.snapshot_repo(str(p))["skipped"] == "Denied top-level repository"


def test_mount_and_drive_denial(service):
    for name in ("Volumes", "mnt", "media"):
        assert service.denied(str(Path(os.sep) / name / "disk"))
    assert service.denied(Path.cwd().anchor)


def test_paths_only(service, repo):
    service.settings["big_repo_entries"] = 1
    (repo / "other.txt").write_text("other baseline", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Second entry")
    (repo / "tracked.txt").write_text("touched", encoding="utf-8")
    (repo / "other.txt").write_text("untouched mutation", encoding="utf-8")
    (repo / "untracked.txt").write_text("not touched", encoding="utf-8")
    result = service.snapshot_repo(str(repo), [str(repo / "tracked.txt")])
    assert result["mode"] == "paths-only" and not result.get("error"), result
    assert git(repo, "show", shadow(repo) + ":other.txt") == "other baseline"
    assert "untracked.txt" not in tree(repo)
    # Directory targets only add tracked files.
    result = service.snapshot_repo(str(repo), [str(repo)])
    assert not result.get("error"), result
    assert git(repo, "show", shadow(repo) + ":other.txt") == "untouched mutation"
    assert "untracked.txt" not in tree(repo)


def test_slow_paths_only(service, repo, plugin):
    plugin.core.atomic_json(service.repo_state(str(repo)), {"repo": str(repo), "slow_at": time.time()})
    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")
    (repo / "untracked.txt").write_text("skip", encoding="utf-8")
    result = service.snapshot_repo(str(repo), [str(repo)])
    assert result["mode"] == "paths-only"
    assert "untracked.txt" not in tree(repo)


@pytest.mark.parametrize("key,value", [("mirror", "true"), ("push", "+refs/*:refs/*")])
def test_mirror_skipped(service, repo, key, value):
    git(repo, "config", "remote.test." + key, value)
    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")
    assert "Mirror" in service.snapshot_repo(str(repo))["skipped"]
    assert git(repo, "for-each-ref", "refs/hermes-autosave/") == ""


def test_file_dedupe_module_instances(service, tmp_path):
    target = tmp_path / "standalone"
    target.write_text("one content", encoding="utf-8")
    copy = service.backup_file(str(target))
    second = load_plugin("autosave_second_process").core.Autosave({"storage_dir": str(service.storage)})
    assert second.backup_file(str(target)) is None
    assert len(second.file_versions(str(target))) == 1
    assert Path(copy).read_bytes() == target.read_bytes()
    if os.name != "nt":
        assert Path(copy).stat().st_mode & 0o077 == 0


def test_hook_never_raises(plugin, service, monkeypatch, repo):
    code = '''
def fail(*args, **kwargs):
    raise RuntimeError("Injected failure")
plugin.core.Autosave.classify = fail
'''
    inject_worker(plugin, monkeypatch, code)
    hook = plugin.make_hook(service)
    assert hook("patch", {"path": str(repo / "tracked.txt")}, new_field=True) is None
    assert "Injected failure" in service.read_ledger()[-1]["error"]
    inject_worker(plugin, monkeypatch, "plugin.core.Autosave.log = fail")
    assert hook("patch", {"path": str(repo / "tracked.txt")}) is None
    assert hook("patch", None) is None


@pytest.mark.skipif(os.name == "nt", reason="Executable POSIX fake git")
def test_hung_git_deadline(plugin, service, tmp_path, monkeypatch, repo):
    fake = tmp_path / "bin"
    fake.mkdir()
    script = fake / "git"
    # The child retains stdout: killing only the shell would still hang pipe cleanup.
    script.write_text("#!/bin/sh\nsleep 20\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake) + os.pathsep + os.environ["PATH"])
    service.settings["hook_budget_seconds"] = 0.5
    start = time.monotonic()
    assert plugin.make_hook(service)("patch", {"path": str(repo / "tracked.txt")}) is None
    # Documented contract: the budget plus a bounded cleanup/diagnostic allowance below 0.2 seconds.
    assert time.monotonic() - start < 0.5 + 0.2
    assert "TimeoutExpired" in service.read_ledger()[-1]["error"]


def test_fallback_public_imports(plugin, tmp_path, monkeypatch):
    original = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name in ("plugins.plugin_storage", "agent.runtime_cwd", "hermes_constants"):
            raise ImportError("Deliberately absent")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", blocked)
    assert plugin.core.default_storage() == Path(os.environ["HERMES_HOME"]) / "plugin-data" / "git-autosave"
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    assert plugin.core.resolve_path("relative") == str(tmp_path / "relative")
    absolute = str(tmp_path / "absolute")
    assert plugin.core.resolve_path(absolute) == absolute
    monkeypatch.delenv("TERMINAL_CWD")
    assert plugin.core.agent_cwd() == os.getcwd()


def test_optional_helpers_live_or_fallback(plugin):
    assert plugin.core.hermes_home() == Path(os.environ["HERMES_HOME"])
    assert plugin.core.default_storage().is_relative_to(Path(os.environ["HERMES_HOME"]))
    assert Path(plugin.core.agent_cwd()).is_absolute()


@pytest.mark.parametrize("key,value", [("keep_days", "false"), ("keep_days", True), ("max_file_mb", False),
    ("hook_budget_seconds", "8"), ("max_untracked", []), ("big_repo_entries", 1.5), ("keep_days", float("nan")),
    ("extra_skip_dirs", "vendor"), ("deny_toplevels", [123]), ("storage_dir", False)])
def test_invalid_settings(plugin, tmp_path, key, value):
    settings = {"storage_dir": str(tmp_path / "store"), key: value}
    service = plugin.core.Autosave(settings)
    assert service.settings[key] == plugin.core.DEFAULTS[key]
    service.warn_settings()
    service.warn_settings()
    assert len(service.read_ledger()) == 1
    assert "Invalid setting " + key in service.read_ledger()[0]["error"]


def test_budget_cap_and_extra_skip(plugin, tmp_path):
    service = plugin.core.Autosave({"storage_dir": str(tmp_path / "store"), "hook_budget_seconds": 90,
                                  "extra_skip_dirs": ["generated"]})
    assert service.settings["hook_budget_seconds"] == 12
    assert service.skip(tmp_path / "generated" / "x", tracked=True)


def test_prune_refs_files_ledger(plugin, service, repo, tmp_path):
    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")
    result = service.snapshot_repo(str(repo))
    assert not result.get("error"), result
    oldenv = dict(os.environ, GIT_AUTHOR_DATE="2001-01-01T00:00:00+00:00", GIT_COMMITTER_DATE="2001-01-01T00:00:00+00:00")
    sha = git(repo, "commit-tree", git(repo, "rev-parse", shadow(repo) + "^{tree}"), "-m", "Old", env=oldenv)
    git(repo, "update-ref", "--create-reflog", "refs/hermes-autosave/old", sha)
    oldday = service.files / "20010101"
    oldday.mkdir(parents=True)
    (oldday / "file").write_text("old", encoding="utf-8")
    service.log({"at": 978307200, "kind": "old"})
    before = service.ledger.read_bytes()
    dry = service.prune(True)
    if not hasattr(os, "O_NOFOLLOW") or not __import__("shutil").rmtree.avoids_symlink_attacks:
        assert dry["errors"] and oldday.exists() and service.ledger.read_bytes() == before
        assert service.prune()["errors"] and oldday.exists()
        return  # Unsupported platforms refuse unsafe pruning; no best-effort deletion.
    assert dry["refs_deleted"] == [{"repo": str(repo), "ref": "refs/hermes-autosave/old"}]
    assert oldday.exists() and service.ledger.read_bytes() == before
    actual = service.prune()
    assert not actual["errors"], actual
    assert not oldday.exists()
    assert "refs/hermes-autosave/old" not in service.refs(str(repo))
    assert actual["reflogs_expired"] == 1
    assert all(json.loads(line)["at"] > 978307200 for line in service.ledger.read_text().splitlines())


def test_repo_lock_busy(service, repo, plugin):
    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")
    with plugin.core.locked(service.repo_lock(str(repo), plugin.core.Budget())):
        result = service.snapshot_repo(str(repo))
    assert result.get("error") and not result["changed"]
    assert not service.refs(str(repo))


def test_no_filters_or_hooks_executed(service, repo):
    git(repo, "config", "filter.test.clean", "a-nonexistent-filter")
    git(repo, "config", "filter.test.required", "true")
    (repo / ".gitattributes").write_text("tracked.txt filter=test", encoding="utf-8")
    (repo / "tracked.txt").write_text("raw content", encoding="utf-8")
    result = service.snapshot_repo(str(repo))
    assert not result.get("error"), result
    assert git(repo, "show", shadow(repo) + ":tracked.txt") == "raw content"


def test_unborn_repo(service, tmp_path):
    repo = tmp_path / "unborn"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "new").write_text("new content", encoding="utf-8")
    assert service.snapshot_repo(str(repo))["changed"]
    assert tree(repo) == ["new"]
    assert not (repo / ".git" / "index").exists()


def test_ignored_large_and_untracked_limit(service, repo):
    (repo / ".gitignore").write_text("ignored\n", encoding="utf-8")
    for name in ("ignored", "one", "two", "touched"):
        (repo / name).write_text(name, encoding="utf-8")
    service.settings["max_untracked"] = 1
    result = service.snapshot_repo(str(repo), [str(repo / "touched")])
    assert not result.get("error"), result
    assert "touched" in tree(repo) and "one" not in tree(repo) and "ignored" not in tree(repo)
    service.settings["max_file_mb"] = 0.000001
    (repo / "large").write_text("large file", encoding="utf-8")
    assert service.backup_file(str(repo / "large")) is None


def test_index_environment_cannot_redirect(service, repo, tmp_path, monkeypatch):
    foreign = tmp_path / "foreign-index"
    foreign.write_bytes(b"do not touch")
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign))
    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")
    assert service.snapshot_repo(str(repo))["changed"]
    assert foreign.read_bytes() == b"do not touch"


def test_expire_old_reflog_on_live_ref(service, repo):
    p = repo / "tracked.txt"
    p.write_text("old snapshot", encoding="utf-8")
    first = service.snapshot_repo(str(repo))
    assert not first.get("error"), first
    ref = "refs/hermes-autosave/main"
    git(repo, "update-ref", "-d", ref)
    oldenv = dict(os.environ, GIT_COMMITTER_DATE="2001-01-01T00:00:00+00:00")
    git(repo, "update-ref", "--create-reflog", ref, first["commit"], env=oldenv)
    p.write_text("fresh snapshot", encoding="utf-8")
    assert service.snapshot_repo(str(repo))["changed"]
    assert len(git(repo, "reflog", "show", "--format=%H", ref).splitlines()) == 2
    result = service.prune()
    assert not result["errors"], result
    assert len(git(repo, "reflog", "show", "--format=%H", ref).splitlines()) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and executable-bit behavior")
def test_symlink_dependency_and_executable_mode(service, repo):
    folder = repo / "node_modules"
    folder.mkdir()
    (folder / "file").write_text("dependency", encoding="utf-8")
    (repo / "alias").symlink_to(folder, target_is_directory=True)
    assert service.backup_file(str(repo / "alias" / "file")) is None
    tracked = repo / "tracked.txt"
    tracked.chmod(0o755)
    result = service.snapshot_repo(str(repo))
    assert not result.get("error"), result
    assert git(repo, "ls-tree", shadow(repo), "tracked.txt").startswith("100755")


def test_copy_names_do_not_overlap(service, tmp_path):
    p = tmp_path / "file"
    similar = tmp_path / "file.extra"
    p.write_text("one", encoding="utf-8")
    similar.write_text("two", encoding="utf-8")
    service.backup_file(str(p))
    service.backup_file(str(similar))
    assert len(service.file_versions(str(p))) == 1


def test_oversized_integer_setting(plugin):
    service = plugin.core.Autosave({"keep_days": 10 ** 1000})
    assert service.settings["keep_days"] == 3
