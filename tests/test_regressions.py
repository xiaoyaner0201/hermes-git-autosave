"""Regressions: real Git, bounded concurrency and operation-count performance checks."""
import inspect
import os
from pathlib import Path
import threading
import time

import pytest

from .conftest import git, shadow, tree


def count_hashes(service, monkeypatch):
    calls = []
    original = service.git

    def record(repo, args, *rest, **kwargs):
        if args[0] == "hash-object":
            calls.append(args)
        return original(repo, args, *rest, **kwargs)

    monkeypatch.setattr(service, "git", record)
    return calls


def test_many_untracked_one_hash_process(plugin, service, repo, monkeypatch):
    for i in range(299):
        (repo / f"tracked-{i}").write_text(f"tracked {i}\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "300 tracked files")
    for i in range(1500):
        (repo / f"note-{i}").write_text(f"untracked {i}\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    calls = count_hashes(service, monkeypatch)
    assert plugin.make_hook(service)("write_file", {"path": str(repo / "tracked.txt")}, turn_id="first") is None
    result = [r for r in service.read_ledger() if r.get("kind") == "repo"][-1]
    assert result["changed"] and not result.get("error"), result
    assert result["secs"] < service.settings["hook_budget_seconds"]
    assert len(tree(repo)) == 1800
    assert calls == [["hash-object", "-w", "--no-filters", "--stdin-paths"]]


@pytest.mark.skipif(os.name == "nt", reason="POSIX filenames with quotes, backslashes and line breaks")
def test_batch_unusual_names_and_symlinks(service, repo, monkeypatch):
    names = ['"quoted', "back\\slash", "with space", "with\ttab", "unicodé", "-dash", "line\nbreak", "carriage\rreturn"]
    for name in names:
        (repo / name).write_bytes(os.fsencode(name))
    (repo / "link").symlink_to("with space")
    calls = count_hashes(service, monkeypatch)
    result = service.snapshot_repo(str(repo))
    assert result["changed"] and not result.get("error"), result
    for name in names:
        assert git(repo, "show", shadow(repo) + ":" + name) == name
    assert git(repo, "show", shadow(repo) + ":link") == "with space"
    assert git(repo, "ls-tree", shadow(repo), "link").startswith("120000")
    assert sum("--stdin-paths" in args for args in calls) == 1
    assert sum("--stdin" in args for args in calls) == 3
    assert all("--no-filters" in args for args in calls)


def test_batch_size_limit_and_tracked_deletion(service, repo, monkeypatch):
    (repo / "tracked.txt").unlink()
    (repo / "small").write_bytes(b"ok")
    (repo / "large").write_bytes(b"x" * 100)
    service.settings["max_file_mb"] = 0.00001
    calls = count_hashes(service, monkeypatch)
    result = service.snapshot_repo(str(repo))
    assert result["changed"] and not result.get("error"), result
    assert tree(repo) == ["small"]
    assert len(calls) == 1 and "--stdin-paths" in calls[0]


def test_25k_entries_resolve_count(plugin, service, repo, monkeypatch):
    # Repeated blob IDs keep fixture construction cheap while exercising a real 25k-entry index.
    sha = git(repo, "rev-parse", "HEAD:tracked.txt")
    entries = "".join(f"100644 {sha}\tsrc/f{i:05d}\0" for i in range(24999))
    service.git(str(repo), ["update-index", "-z", "--index-info"], data=os.fsencode(entries))
    git(repo, "commit", "-m", "25k entries")
    git(repo, "checkout-index", "-a")
    git(repo, "update-index", "--refresh")
    original_resolve = Path.resolve
    original_skip = service.skip
    counts = {"resolve": 0, "skip": 0}

    def resolve(path, *args, **kwargs):
        counts["resolve"] += 1
        return original_resolve(path, *args, **kwargs)

    def skip(*args, **kwargs):
        counts["skip"] += 1
        return original_skip(*args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(service, "skip", skip)
    assert plugin.make_hook(service)("write_file", {"path": str(repo / "tracked.txt")}, turn_id="first") is None
    result = [r for r in service.read_ledger() if r.get("kind") == "repo"][-1]
    assert not result.get("error"), result
    assert result["mode"] == "paths-only"
    assert counts["resolve"] < 100, counts
    assert counts["skip"] == 1, counts  # Single user-path classification, never index entries.


@pytest.mark.parametrize("placement", ["inside", "outside", "whole-repo"])
def test_repo_storage_prefix(service, repo, tmp_path, placement):
    service.root = {"inside": repo / "autosave", "outside": tmp_path / "other", "whole-repo": repo}[placement]
    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")
    if placement == "inside":
        service.root.mkdir()
        (service.root / "tracked-state").write_text("never save", encoding="utf-8")
        git(repo, "add", "autosave/tracked-state")
        (service.root / "untracked-state").write_text("never save", encoding="utf-8")
        sibling = repo / "autosave-sibling"
        sibling.mkdir()
        (sibling / "keep").write_text("keep", encoding="utf-8")
    result = service.snapshot_repo(str(repo))
    assert not result.get("error"), result
    if placement == "whole-repo":
        assert tree(repo) == []
    else:
        assert "tracked.txt" in tree(repo)
        assert not any(p.startswith("autosave/") for p in tree(repo))
        if placement == "inside":
            assert "autosave-sibling/keep" in tree(repo)


def test_repo_skipper_ancestor_and_component_rules(service, tmp_path):
    skip = service.repo_skipper(tmp_path / "vendor" / "nested")
    assert skip("file", tracked=True)
    skip = service.repo_skipper(tmp_path / "build" / "nested")
    assert skip("file") and not skip("file", tracked=True)
    skip = service.repo_skipper(tmp_path / "project")
    assert skip("src/node_modules/file", tracked=True)
    assert not skip("src/node_modules-like/file", tracked=True)
    assert skip("dist/file") and not skip("dist/file", tracked=True)
    service.settings["extra_skip_dirs"] = ["generated"]
    assert service.repo_skipper(tmp_path / "project")("src/generated/file", tracked=True)


def test_mutex_contention_still_snapshots(plugin, service, repo, monkeypatch):
    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")
    hook = plugin.make_hook(service)
    mutex = inspect.getclosurevars(hook).nonlocals["mutex"]
    held, release, classified, finished = (threading.Event() for _ in range(4))
    original = service.classify

    def classify(*args, **kwargs):
        result = original(*args, **kwargs)
        classified.set()
        return result

    monkeypatch.setattr(service, "classify", classify)

    def holder():
        with mutex:
            held.set()
            release.wait(5)

    def call():
        try:
            assert hook("write_file", {"path": str(repo / "tracked.txt")}, turn_id="contended") is None
        finally:
            finished.set()

    holding = threading.Thread(target=holder)
    calling = threading.Thread(target=call)
    holding.start()
    try:
        assert held.wait(5)
        calling.start()
        assert classified.wait(5)
        assert not finished.wait(0.1), "Contended hook silently returned instead of waiting"
    finally:
        release.set()
        holding.join(5)
        if calling.ident is not None:
            calling.join(5)
    assert finished.is_set() and not holding.is_alive() and not calling.is_alive()
    assert shadow(repo)
    result = [r for r in service.read_ledger() if r.get("kind") == "repo"][-1]
    assert result["changed"] and not result.get("error"), result


def test_real_hermes_concurrent_hook(plugin, repo, monkeypatch):
    hermes = pytest.importorskip("hermes_cli.plugins")
    manager = hermes.PluginManager()
    manifest = hermes.PluginManifest(name="git-autosave", provides_hooks=["pre_tool_call"])
    ctx = hermes.PluginContext(manifest, manager)
    active, release = threading.Event(), threading.Event()
    original = plugin.core.Autosave._snapshot
    durations, results, errors = [], {}, []

    def measurable_snapshot(service, *args, **kwargs):
        start = time.monotonic()
        active.set()
        assert release.wait(5), "Concurrent read never completed"
        original(service, *args, **kwargs)
        durations.append(time.monotonic() - start)

    monkeypatch.setattr(plugin.core.Autosave, "_snapshot", measurable_snapshot)
    plugin.register(ctx)  # Real register(ctx), real PluginContext and bounded hook dispatcher.
    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")

    def invoke(name, tool):
        try:
            results[name] = manager.invoke_hook(
                "pre_tool_call", tool_name=tool, args={"path": str(repo / "tracked.txt")},
                task_id=name, session_id="session-" + name, turn_id="turn-" + name, tool_call_id="call-" + name,
            )
        except Exception as exc:
            errors.append(exc)

    writing = threading.Thread(target=invoke, args=("write", "write_file"))
    reading = threading.Thread(target=invoke, args=("read", "read_file"))
    writing.start()
    try:
        assert active.wait(5), "Real plugin snapshot did not start"
        reading.start()
        reading.join(5)
        assert not reading.is_alive()
        # Keep the first real callback active for measurable time, not a scheduling accident.
        assert not release.wait(0.2)
    finally:
        release.set()
        writing.join(5)
        if reading.ident is not None:
            reading.join(5)
    assert not writing.is_alive() and not errors, errors
    assert set(results) == {"write", "read"}
    for result in results.values():
        # Hermes's fail-closed directive is {"action": "block", ...}; match the value, not a key.
        assert not any(isinstance(item, dict) and item.get("action") == "block" for item in result), results
    assert durations and durations[0] >= 0.2
    assert shadow(repo)
    assert git(repo, "show", shadow(repo) + ":tracked.txt") == "dirty"
