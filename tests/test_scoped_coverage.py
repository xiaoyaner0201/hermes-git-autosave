"""Only successful path coverage suppresses later targets."""
import importlib

import pytest

from .conftest import git


@pytest.mark.parametrize("kind", ["big", "untracked-limit", "ignored", "sparse"])
def test_scoped_same_turn(plugin, service, repo, kind):
    a, b = repo / "tracked.txt", repo / "b"
    b.write_text("BASE-B")
    if kind in {"big", "sparse"}:
        git(repo, "add", "b")
        git(repo, "commit", "-m", "Second file")
    if kind == "big":
        service.settings["big_repo_entries"] = 1
    elif kind == "untracked-limit":
        service.settings["max_untracked"] = 1
        (repo / "c").write_text("extra")
    elif kind == "ignored":
        (repo / ".gitignore").write_text("b\n")
    else:
        git(repo, "update-index", "--skip-worktree", "b")
    a.write_text("PREEDIT-A")
    b.write_text("PREEDIT-B")
    hook = plugin.make_hook(service)
    for path in (a, b):
        hook("write_file", {"path": str(path)}, session_id="s", turn_id="t")
        path.write_text("OVERWRITTEN")
    importlib.import_module(plugin.__name__ + ".cli").restore(service, str(b))
    assert b.read_text() == "PREEDIT-B"


@pytest.mark.parametrize("failure", ["lock", "mirror", "oversize"])
def test_failed_snapshot_not_coverage(plugin, service, repo, failure):
    p = repo / "tracked.txt"
    p.write_text("PREEDIT")
    hook = plugin.make_hook(service)
    if failure == "lock":
        with plugin.core.locked(service.repo_lock(str(repo), plugin.core.Budget())):
            hook("write_file", {"path": str(p)}, turn_id="t")
    elif failure == "mirror":
        git(repo, "config", "remote.test.mirror", "true")
        hook("write_file", {"path": str(p)}, turn_id="t")
        git(repo, "config", "--unset", "remote.test.mirror")
    else:
        service.settings["max_file_mb"] = 0.000001
        hook("write_file", {"path": str(p)}, turn_id="t")
        service.settings["max_file_mb"] = 10
    hook("write_file", {"path": str(p)}, turn_id="t")
    p.write_text("OVERWRITTEN")
    importlib.import_module(plugin.__name__ + ".cli").restore(service, str(p))
    assert p.read_text() == "PREEDIT"
