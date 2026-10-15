"""Turn coverage is raw-byte and HEAD/branch specific."""
import importlib
import shlex
import sys

import pytest

from .conftest import git


def cli_for(plugin):
    return importlib.import_module(plugin.__name__ + ".cli")


@pytest.mark.parametrize("representation", ["crlf", "filter"])
def test_stat_baseline_not_raw_coverage(plugin, service, repo, representation):
    a, b = repo / "tracked.txt", repo / "b"
    if representation == "crlf":
        git(repo, "config", "core.autocrlf", "true")
        expected = b"line1\r\nline2\r\n"
    else:
        code = "import sys; sys.stdout.write(sys.stdin.read().split('|',1)[0]+'\\n')"
        git(repo, "config", "filter.example.clean", shlex.quote(sys.executable) + " -c " + shlex.quote(code))
        (repo / ".gitattributes").write_text("b filter=example\n")
        expected = b"NORMALIZED|IRREPLACEABLE-RAW\n"
    b.write_bytes(expected)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "normalized index")
    hook = plugin.make_hook(service)
    hook("write_file", {"path": str(a)}, session_id="s", turn_id="t")
    hook("write_file", {"path": str(b)}, session_id="s", turn_id="t")
    b.write_bytes(b"LOST")
    cli_for(plugin).restore(service, str(b))
    assert b.read_bytes() == expected


def test_branch_switch_invalidates_turn(plugin, service, repo):
    p = repo / "tracked.txt"
    git(repo, "switch", "-c", "second")
    p.write_text("SECOND")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "second")
    git(repo, "switch", "main")
    hook = plugin.make_hook(service)
    hook("terminal", {"command": "git switch second", "workdir": str(repo)}, session_id="s", turn_id="t")
    git(repo, "switch", "second")
    hook("write_file", {"path": str(p)}, session_id="s", turn_id="t")
    p.write_text("LOST")
    cli_for(plugin).restore(service, str(p))
    assert p.read_text() == "SECOND"


@pytest.mark.parametrize("change", ["head", "branch"])
def test_identity_checked_without_terminal_hook(plugin, service, repo, change):
    p = repo / "tracked.txt"
    hook = plugin.make_hook(service)
    hook("write_file", {"path": str(p)}, session_id="s", turn_id="t")
    p.write_text("SECOND")
    git(repo, "add", ".")
    if change == "head":
        git(repo, "commit", "-m", "second")
    else:
        git(repo, "branch", "-m", "renamed")
    hook("write_file", {"path": str(p)}, session_id="s", turn_id="t")
    p.write_text("LOST")
    cli_for(plugin).restore(service, str(p))
    assert p.read_text() == "SECOND"


@pytest.mark.parametrize("command", ["git restore -- tracked.txt", "git add tracked.txt", "git update-index --refresh"])
def test_git_state_command_discards_coverage(plugin, service, repo, command):
    p = repo / "tracked.txt"
    hook = plugin.make_hook(service)
    hook("write_file", {"path": str(p)}, session_id="s", turn_id="t")
    hook("terminal", {"command": command, "workdir": str(repo)}, session_id="s", turn_id="t")
    git(repo, *shlex.split(command)[1:])
    p.write_text("SECOND")
    hook("write_file", {"path": str(p)}, session_id="s", turn_id="t")
    p.write_text("LOST")
    cli_for(plugin).restore(service, str(p))
    assert p.read_text() == "SECOND"
