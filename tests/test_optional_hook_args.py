"""Public isolated hooks: nullable arguments and original settings diagnostics."""
import inspect
import subprocess

import pytest

from .conftest import git, shadow


@pytest.fixture
def registered(plugin, service, monkeypatch):
    hermes = pytest.importorskip("hermes_cli.plugins")
    settings = dict(service.settings)
    monkeypatch.setattr(hermes, "load_config_readonly", lambda: {
        "plugins": {"entries": {"git-autosave": {"settings": settings}}}})
    manager = hermes.PluginManager()
    context = hermes.PluginContext(
        hermes.PluginManifest(name="git-autosave", provides_hooks=["pre_tool_call"]), manager)
    plugin.register(context)
    return manager, settings


@pytest.mark.parametrize("unrelated", [False, True])
@pytest.mark.parametrize("workdir", ["omitted", "null", "explicit", "empty"])
@pytest.mark.parametrize("cwd_source", ["terminal", "session", "invalid-session"])
def test_terminal_optional_workdir(registered, repo, tmp_path, monkeypatch, workdir, cwd_source, unrelated):
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd

    manager, _ = registered
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "tracked.txt").write_bytes(b"wrong cwd")
    session = str(repo) if cwd_source == "session" else (
        str(tmp_path / "missing") if cwd_source == "invalid-session" else "")
    monkeypatch.setenv("TERMINAL_CWD", str(decoy if cwd_source == "session" else repo))
    token = set_session_cwd(session)
    args = {"command": "rm tracked.txt"}
    if workdir != "omitted":
        args["workdir"] = {"null": None, "explicit": str(repo), "empty": ""}[workdir]
    # Unrelated fields, including non-JSON values, must never poison terminal IPC.
    if unrelated:
        args.update(path=object(), mode=None, patch=object())
    before = b"pre-edit\x00exact\n"
    (repo / "tracked.txt").write_bytes(before)
    head, index = git(repo, "rev-parse", "HEAD"), (repo / ".git/index").read_bytes()
    try:
        assert manager.invoke_hook("pre_tool_call", tool_name="terminal", args=args) == []
    finally:
        reset_session_cwd(token)
    saved = subprocess.run(["git", "-C", str(repo), "show", shadow(repo) + ":tracked.txt"],
                           check=True, capture_output=True).stdout
    assert saved == before
    assert (repo / "tracked.txt").read_bytes() == before  # rm was observed, not executed.
    assert (decoy / "tracked.txt").read_bytes() == b"wrong cwd"
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / ".git/index").read_bytes() == index


def test_explicit_workdir_overrides_session(registered, repo, tmp_path, monkeypatch, service):
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd

    manager, _ = registered
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    target = explicit / "tracked.txt"
    target.write_bytes(b"explicit pre-edit")
    monkeypatch.setenv("TERMINAL_CWD", str(repo))
    token = set_session_cwd(str(repo))
    try:
        assert manager.invoke_hook("pre_tool_call", tool_name="terminal",
                                   args={"command": "rm tracked.txt", "workdir": str(explicit)}) == []
    finally:
        reset_session_cwd(token)
    assert service.file_versions(str(target))
    assert target.read_bytes() == b"explicit pre-edit"
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify",
                           "refs/hermes-autosave/main"], capture_output=True).returncode != 0


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "tracked.txt", "content": "new", "mode": object(), "patch": object()}),
    ("patch", {"path": "tracked.txt", "old_string": "old", "new_string": "new"}),
    ("patch", {"path": "tracked.txt", "mode": None, "patch": None}),
    ("patch", {"mode": "patch", "path": None, "patch": "*** Update File: tracked.txt\n@@\n-old\n+new\n"}),
])
def test_file_optional_fields(registered, repo, monkeypatch, tool, args):
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd

    manager, _ = registered
    monkeypatch.setenv("TERMINAL_CWD", str(repo.parent))
    token = set_session_cwd(str(repo))
    (repo / "tracked.txt").write_text("old")
    try:
        assert manager.invoke_hook("pre_tool_call", tool_name=tool,
                                   args={**args, "command": object(), "workdir": object()}) == []
    finally:
        reset_session_cwd(token)
    assert git(repo, "show", shadow(repo) + ":tracked.txt") == "old"
    assert (repo / "tracked.txt").read_text() == "old"


@pytest.mark.parametrize("tool,args", [
    ("terminal", {"command": "rm tracked.txt", "workdir": 123}),
    ("write_file", {"path": 123}),
    ("patch", {"path": "tracked.txt", "mode": []}),
])
def test_invalid_relevant_fields_fail_without_protocol_expansion(plugin, service, repo, tool, args):
    hook = plugin.make_hook(service)
    assert hook(tool, args) is None
    assert hook.health == {"failures": 1, "last_error": "ValueError"}
    assert not service.read_ledger()
    assert (repo / "tracked.txt").read_text() == "baseline\n"


def test_settings_warning_once_across_workers(plugin, registered, service, repo):
    manager, settings = registered
    # Re-register through the real Context after changing its synthetic config.
    settings.update(max_file_mb=-1, extra_skip_dirs=[None])
    import hermes_cli.plugins as hermes
    manager = hermes.PluginManager()
    ctx = hermes.PluginContext(hermes.PluginManifest(name="git-autosave", provides_hooks=["pre_tool_call"]), manager)
    plugin.register(ctx)
    for i in range(2):
        (repo / "tracked.txt").write_text(f"before {i}")
        assert manager.invoke_hook("pre_tool_call", tool_name="write_file",
                                   args={"path": str(repo / "tracked.txt")}) == []
        assert git(repo, "show", shadow(repo) + ":tracked.txt") == f"before {i}"
    warnings = [r["error"] for r in service.read_ledger() if r.get("kind") == "settings"]
    assert warnings == ["Invalid setting max_file_mb; using default",
                        "Invalid setting extra_skip_dirs; using default"]


def test_settings_warning_respects_already_warned(plugin, service, repo):
    service._invalid = ["max_file_mb"]
    service.warn_settings()
    hook = plugin.make_hook(service)
    assert inspect.getclosurevars(hook).nonlocals["service"] is service
    assert hook("write_file", {"path": str(repo / "tracked.txt")}) is None
    assert len([r for r in service.read_ledger() if r.get("kind") == "settings"]) == 1
