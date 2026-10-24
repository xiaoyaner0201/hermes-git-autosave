"""Opaque terminal commands degrade visibly without executing their shell payload."""
import importlib
import os
import shlex
import subprocess

import pytest

from .conftest import git, shadow, tree

PAYLOAD = "SYNTHETIC_SHELL_PAYLOAD_MUST_NOT_BE_LOGGED"
VALID_OPAQUE = [
    "cat <<'EOF'\ndon't " + PAYLOAD + "\nEOF\n",
    'cat <<EOF\n"' + PAYLOAD + "\nEOF\n",
    "cat <<'EOF'\n'\" " + PAYLOAD + "\nEOF\n",
    "cat <<-'EOF'\n\tdon't " + PAYLOAD + "\n\tEOF\n",
]
MALFORMED = ["printf '" + PAYLOAD, 'printf "' + PAYLOAD]
OPAQUE = VALID_OPAQUE + MALFORMED
WARNING = "Command tokenization failed; attempting initial cwd repository protection only"


def hooks_for(plugin):
    return importlib.import_module(plugin.__name__ + ".hooks")


def state(repo):
    """Observe raw real-index/HEAD bytes and every non-.git working entry."""
    entries = {}
    for path in repo.rglob("*"):
        if ".git" in path.relative_to(repo).parts:
            continue
        entries[str(path.relative_to(repo))] = (
            path.lstat().st_mode,
            os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None,
        )
    return git(repo, "rev-parse", "HEAD"), (repo / ".git/HEAD").read_bytes(), (repo / ".git/index").read_bytes(), entries


def assert_diagnostic(service, count=1):
    records = service.read_ledger()
    diagnostics = [r for r in records if r.get("kind") == "parser"]
    assert len(diagnostics) == count, records
    for record in diagnostics:
        assert {k: v for k, v in record.items() if k != "at"} == {
            "kind": "parser", "code": "command_parse_fallback", "warning": WARNING,
        }
    assert not any(r.get("kind") == "hook" or r.get("error") for r in records), records
    assert PAYLOAD not in service.ledger.read_text()
    assert "No closing quotation" not in service.ledger.read_text()


@pytest.mark.parametrize("command", OPAQUE)
def test_opaque_helpers_use_initial_cwd(plugin, repo, tmp_path, command):
    hooks = hooks_for(plugin)
    command = "cd elsewhere\n" + command
    result = subprocess.run(["bash", "-n"], input=command, text=True, capture_output=True, timeout=3)
    assert (result.returncode == 0) == (command.split("\n", 1)[1] in VALID_OPAQUE), result.stderr
    with pytest.raises(ValueError, match="No closing quotation"):
        hooks.segments(command)
    args = {"command": command, "workdir": str(repo)}
    assert hooks.destructive(command)
    assert hooks.terminal_cwd(args) == str(repo)
    assert hooks.targets("terminal", args) == ([], [str(repo)])
    assert hooks.targets("terminal", args, content_only=True) == ([], [str(repo)])
    assert hooks.targets("terminal", args, all_terminal=True) == ([], [str(repo)])
    assert hooks.git_state_dirs(args) == [str(repo)]
    # Relative workdirs still use the caller's base; omitted workdirs use the public cwd fallback.
    assert hooks.targets("terminal", {"command": command, "workdir": "repo"}, str(tmp_path)) == ([], [str(repo)])
    assert hooks.targets("terminal", {"command": command}, str(repo)) == ([], [str(repo)])


@pytest.mark.parametrize("command", OPAQUE)
def test_opaque_hook_snapshots_initial_repo_without_payload(plugin, service, repo, tmp_path, command):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "untouched").write_text("outside original")
    (repo / "tracked.txt").write_text("staged")
    git(repo, "add", "tracked.txt")
    (repo / "tracked.txt").write_text("raw pre-edit bytes")
    before = state(repo)
    args = {"command": "cd " + shlex.quote(str(outside)) + "\n" + command, "workdir": str(repo)}
    assert plugin.make_hook(service)("terminal", args, session_id="s", turn_id="t") is None
    assert git(repo, "show", shadow(repo) + ":tracked.txt") == "raw pre-edit bytes"
    assert state(repo) == before
    assert (outside / "untouched").read_text() == "outside original"
    assert not list(service.files.rglob("*"))
    assert_diagnostic(service)
    assert args["command"] not in service.ledger.read_text()


@pytest.mark.parametrize("tool", ["write_file", "patch"])
def test_fallback_invalidates_but_does_not_repopulate_turn_cache(plugin, service, repo, tool):
    p = repo / "tracked.txt"
    hook = plugin.make_hook(service)
    hook("write_file", {"path": str(p)}, session_id="s", turn_id="t")
    p.write_text("before opaque command")
    before = state(repo)
    assert hook("terminal", {"command": VALID_OPAQUE[0], "workdir": str(repo)}, session_id="s", turn_id="t") is None
    assert state(repo) == before
    assert git(repo, "show", shadow(repo) + ":tracked.txt") == "before opaque command"
    # Model the actual tool's subsequent mutation; a later explicit edit must retry in the same turn.
    p.write_text("after opaque command")
    before = state(repo)
    assert hook(tool, {"path": str(p)}, session_id="s", turn_id="t") is None
    assert state(repo) == before
    assert git(repo, "show", shadow(repo) + ":tracked.txt") == "after opaque command"
    assert_diagnostic(service)


@pytest.mark.parametrize("location", ["plain", "denied", "dependency", "credential"])
def test_opaque_command_never_copies_unknown_non_git_targets(plugin, service, repo, tmp_path, location):
    cwd = tmp_path / "plain"
    cwd.mkdir()
    if location == "denied":
        cwd = repo
        service.settings["deny_toplevels"] = [str(repo)]
    elif location in {"dependency", "credential"}:
        cwd = repo / {"dependency": "node_modules", "credential": ".aws/nested"}[location]
        cwd.mkdir(parents=True)
    p = cwd / "unknown-target"
    p.write_text("original")
    hook = plugin.make_hook(service)
    for _ in range(2):
        assert hook("terminal", {"command": VALID_OPAQUE[0], "workdir": str(cwd)}) is None
    assert not service.refs(str(repo))
    assert not list(service.files.rglob("*"))
    assert p.read_text() == "original"
    assert_diagnostic(service, count=2)
    # A parse failure cannot poison subsequent path-aware non-Git edits.
    explicit = tmp_path / "explicit"
    explicit.write_text("known pre-edit")
    for count, tool in enumerate(("write_file", "patch"), 1):
        before = explicit.read_bytes()
        assert hook(tool, {"path": str(explicit)}) is None
        versions = service.file_versions(str(explicit))
        assert len(versions) == count
        assert before in [version.read_bytes() for version in versions]
        assert explicit.read_bytes() == before
        explicit.write_text("next pre-edit")


@pytest.mark.parametrize("scoped", [False, True])
def test_fallback_preserves_exclusions_and_paths_only_limits(plugin, service, repo, scoped):
    (repo / "second.txt").write_text("second baseline")
    git(repo, "add", "second.txt")
    git(repo, "commit", "-m", "Second tracked fixture")
    for name in (".env", "node_modules/file", "generated/file"):
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("EXCLUDED_SYNTHETIC_CONTENT")
    (repo / "untracked").write_text("eligible untracked")
    (repo / "tracked.txt").write_text("dirty")
    service.settings["extra_skip_dirs"] = ["generated"]
    service.settings["big_repo_entries"] = 1 if scoped else 20000
    before = state(repo)
    assert plugin.make_hook(service)("terminal", {"command": VALID_OPAQUE[0], "workdir": str(repo)}) is None
    assert state(repo) == before
    assert git(repo, "show", shadow(repo) + ":tracked.txt") == "dirty"
    assert set(tree(repo)) == ({"tracked.txt", "second.txt"} if scoped else {"tracked.txt", "second.txt", "untracked"})
    assert_diagnostic(service)


def test_parseable_multiline_comments_quotes_and_git_cwd_are_unchanged(plugin, service, repo, tmp_path):
    hooks = hooks_for(plugin)
    command = 'printf "%s" "ordinary quoted text" # unmatched comment quotes: \'"\n'
    command += "# don't parse this: git reset --hard\ncd repo\ngit restore -- tracked.txt\n"
    args = {"command": command, "workdir": str(tmp_path)}
    assert subprocess.run(["bash", "-n"], input=command, text=True, capture_output=True, timeout=3).returncode == 0
    assert hooks.destructive(command)
    assert hooks.git_state_dirs(args) == [str(repo)]
    assert hooks.targets("terminal", args) == ([str(repo / "tracked.txt")], [])
    (repo / "tracked.txt").write_text("pre-restore")
    before = state(repo)
    assert plugin.make_hook(service)("terminal", args) is None
    assert state(repo) == before
    assert git(repo, "show", shadow(repo) + ":tracked.txt") == "pre-restore"
    assert not any(r.get("kind") in {"parser", "hook"} for r in service.read_ledger())


def test_oversized_command_never_enters_tokenizer_or_parse_fallback(plugin, service, repo, monkeypatch):
    hooks = hooks_for(plugin)

    def forbidden(*args, **kwargs):
        pytest.fail("oversized command entered shlex")

    monkeypatch.setattr(hooks.shlex, "shlex", forbidden)
    command = "cd elsewhere\nprintf '" + PAYLOAD + "x" * hooks.MAX_COMMAND_CHARS
    (repo / "tracked.txt").write_text("large command pre-edit")
    before = state(repo)
    hook = plugin.make_hook(service)
    for _ in range(2):
        assert hook("terminal", {"command": command, "workdir": str(repo)}) is None
    assert state(repo) == before
    assert git(repo, "show", shadow(repo) + ":tracked.txt") == "large command pre-edit"
    diagnostics = [r for r in service.read_ledger() if r.get("kind") == "parser"]
    assert len(diagnostics) == 1
    assert diagnostics[0].get("code") != "command_parse_fallback"
    assert PAYLOAD not in service.ledger.read_text()
