"""The shell, not its child, opens redirection targets."""
import importlib

import pytest


@pytest.mark.parametrize("command", ["env -C {other} printf NEW > tracked.txt",
                                     "git -C {other} status > tracked.txt",
                                     "git --work-tree={other} status >> tracked.txt",
                                     "env -C {other} rm victim > tracked.txt"])
def test_redirection_shell_cwd(plugin, tmp_path, repo, command):
    hooks = importlib.import_module(plugin.__name__ + ".hooks")
    other = tmp_path / "other"
    other.mkdir()
    files, _ = hooks.targets("terminal", {"command": "cd " + str(repo) + " && " + command.format(other=other),
                                          "workdir": str(tmp_path)})
    assert str(repo / "tracked.txt") in files
    assert str(other / "tracked.txt") not in files
    if "rm victim" in command:
        assert str(other / "victim") in files
