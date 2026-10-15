"""First clean edits on a new branch have an observed baseline."""
import importlib

from .conftest import git


def test_branch_clean_baseline(plugin, service, repo):
    p = repo / "tracked.txt"
    p.write_text("OLD-SHADOW")
    service.snapshot_repo(str(repo), [str(p)])
    git(repo, "restore", "tracked.txt")
    git(repo, "switch", "-c", "second")
    p.write_text("NEW-COMMITTED")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "New branch baseline")
    result = service.snapshot_repo(str(repo), [str(p)])
    assert not result.get("error"), result
    p.write_text("OVERWRITTEN")
    cli = importlib.import_module(plugin.__name__ + ".cli")
    cli.restore(service, str(p))
    assert p.read_text() == "NEW-COMMITTED"
    assert cli.find(service, str(p))["versions"][0]["ref"].endswith("/second")


def test_version_model_without_ledger(plugin, service, repo, monkeypatch):
    cli = importlib.import_module(plugin.__name__ + ".cli")
    p = repo / "tracked.txt"
    stamp = plugin.core.time.time_ns()
    monkeypatch.setattr(plugin.core.time, "time_ns", lambda: stamp)
    with plugin.core.locked(service.state / "ledger.lock"):
        for kind, content in (("git", "G1"), ("file", "F1"), ("git", "G2"), ("file", "F2")):
            p.write_text(content)
            if kind == "git":
                assert service.snapshot_repo(str(repo), [str(p)])["changed"]
            else:
                assert service.backup_file(str(p))
    versions = cli.find(service, str(p))["versions"]
    assert [v["kind"] for v in versions] == ["file", "git", "file", "git"]
    assert len({v["at_ns"] for v in versions}) == 4
    assert all(v["content_id"] and v["mode"] for v in versions)
    cli.restore(service, str(p))
    assert p.read_text() == "F2"
