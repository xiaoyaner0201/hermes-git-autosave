"""Only raw pre-edit observations are recoverable versions."""
import importlib

import pytest

from .conftest import git


def cli_for(plugin):
    return importlib.import_module(plugin.__name__ + ".cli")


def test_branch_repeat_observation(plugin, service, repo):
    p = repo / "tracked.txt"
    assert service.snapshot_repo(str(repo), [str(p)])["changed"]
    git(repo, "switch", "-c", "second")
    p.write_text("B")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "B")
    assert service.snapshot_repo(str(repo), [str(p)])["changed"]
    git(repo, "switch", "main")
    assert service.snapshot_repo(str(repo), [str(p)])["changed"], "Recurring A is a new observation"
    service.ledger.unlink(missing_ok=True)
    p.write_text("LOST")
    cli = cli_for(plugin)
    versions = cli.find(service, str(p))["versions"]
    assert versions[0]["ref"].endswith("/main")
    cli.restore(service, str(p))
    assert p.read_text() == "baseline\n"


@pytest.mark.parametrize("last_backend", ["file", "git"])
def test_backend_repeat_observation(plugin, service, repo, last_backend):
    p = repo / "tracked.txt"
    cli = cli_for(plugin)
    for backend, content in ((last_backend, "A"), ("git" if last_backend == "file" else "file", "B"),
                             (last_backend, "A")):
        p.write_text(content)
        if backend == "file":
            assert service.backup_file(str(p)), "Recurring copy is a new observation"
        else:
            assert service.snapshot_repo(str(repo), [str(p)])["changed"]
    service.ledger.unlink(missing_ok=True)
    p.write_text("LOST")
    versions = cli.find(service, str(p))["versions"]
    assert versions[0]["kind"] == last_backend
    assert len(versions) == 3
    assert all(v.get("content_id") and v.get("mode") and v.get("at_ns") for v in versions)
    cli.restore(service, str(p))
    assert p.read_text() == "A"


def test_scoped_placeholder_not_observation(plugin, service, repo):
    a, b = repo / "tracked.txt", repo / "b"
    b.write_text("BASE-B")
    git(repo, "add", "b")
    git(repo, "commit", "-m", "b")
    service.settings["big_repo_entries"] = 1
    a.write_text("PREEDIT-A")
    b.write_text("PREEDIT-B")
    cli = cli_for(plugin)
    assert service.snapshot_repo(str(repo), [str(a)])["changed"]
    assert cli.find(service, str(b))["versions"] == [], "Index placeholders are not observations"
    a.write_text("OVERWRITTEN-A")
    assert service.snapshot_repo(str(repo), [str(b)])["changed"]
    b.write_text("OVERWRITTEN-B")
    service.ledger.unlink(missing_ok=True)
    # Also discard process-local state; provenance must travel with snapshots.
    fresh = plugin.core.Autosave({"storage_dir": str(service.storage)})
    versions = cli.find(fresh, str(a))["versions"]
    assert len(versions) == 1
    cli.restore(fresh, str(a))
    cli.restore(fresh, str(b))
    assert a.read_text() == "PREEDIT-A"
    assert b.read_text() == "PREEDIT-B"


def test_scoped_does_not_enumerate_unrelated_untracked(plugin, service, repo, monkeypatch):
    (repo / "b").write_text("B")
    git(repo, "add", "b")
    git(repo, "commit", "-m", "second file")
    service.settings["big_repo_entries"] = 1
    calls = []
    original = service.git

    def record(repo, args, *rest, **kwargs):
        if args[:2] == ["ls-files", "-o"]:
            calls.append(args)
        return original(repo, args, *rest, **kwargs)

    monkeypatch.setattr(service, "git", record)
    result = service.snapshot_repo(str(repo), [str(repo / "tracked.txt")])
    assert not result.get("error"), result
    assert calls == [], "Paths-only snapshots must not scan unrelated untracked files"


def test_never_read_path_not_listed(plugin, service, repo):
    p = repo / "large"
    p.write_bytes(b"TOO LARGE")
    git(repo, "add", "large")
    git(repo, "commit", "-m", "large")
    service.settings["max_file_mb"] = 0.000001
    result = service.snapshot_repo(str(repo), [str(p)])
    assert not result.get("error"), result
    assert cli_for(plugin).find(service, str(p))["versions"] == []
