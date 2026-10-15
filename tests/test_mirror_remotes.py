"""Use Git's own boolean parser and refuse invalid mirror config."""
import pytest

from .conftest import git


@pytest.mark.parametrize("value", [None, "2", "-1", "yes", "on", "invalid"])
def test_mirror_boolean(service, repo, value):
    if value is None:
        with (repo / ".git/config").open("a") as out:
            out.write('\n[remote "origin"]\n\tmirror\n')
    else:
        git(repo, "config", "remote.origin.mirror", value)
    (repo / "tracked.txt").write_text("PRIVATE-PREEDIT")
    result = service.snapshot_repo(str(repo))
    assert not service.refs(str(repo)), result
    assert result.get("skipped") or result.get("error"), result
    assert service.read_ledger()[-1].get("skipped") or service.read_ledger()[-1].get("error")


@pytest.mark.parametrize("value", ["false", "no", "off", "0", ""])
def test_mirror_false_allowed(service, repo, value):
    git(repo, "config", "remote.origin.mirror", value)
    (repo / "tracked.txt").write_text("PREEDIT")
    assert service.snapshot_repo(str(repo))["changed"]
