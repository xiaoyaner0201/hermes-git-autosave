import argparse
import importlib
import json
import sqlite3
import time

import pytest

from .conftest import git, shadow


def modules(plugin):
    return (importlib.import_module(plugin.__name__ + ".cli"),
            importlib.import_module(plugin.__name__ + ".sweep"))


def database(plugin, repo, missing=False):
    path = plugin.core.hermes_home() / "state.db"
    with sqlite3.connect(path) as db:
        if missing:
            db.execute("CREATE TABLE sessions (id TEXT)")
            db.execute("CREATE TABLE messages (session_id TEXT)")
        else:
            db.execute("CREATE TABLE sessions (id TEXT, cwd TEXT, started_at REAL, last_activity_at REAL)")
            db.execute("CREATE TABLE messages (session_id TEXT, timestamp REAL, role TEXT, tool_calls TEXT)")
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", ("s", str(repo), time.time(), None))
            calls = [dict(function=dict(name="patch", arguments=json.dumps({"path": "tracked.txt"}))),
                     dict(function=dict(name="terminal", arguments={"command": "cd . && rm file"})),
                     dict(function=dict(name="write_file", arguments={"path": str(repo / "untracked")}))]
            db.execute("INSERT INTO messages VALUES (?, ?, ?, ?)", ("s", time.time(), "assistant", json.dumps(calls)))
    return path


def test_sweep_readonly_session_db(plugin, service, repo):
    _, module = modules(plugin)
    path = database(plugin, repo)
    before = path.read_bytes()
    (repo / "tracked.txt").write_text("missed by hook", encoding="utf-8")
    result = module.sweep(service, 60, 8)
    assert not result["errors"] and not result["warnings"], result
    assert len(result["repos_checked"]) == 1
    assert git(repo, "show", shadow(repo) + ":tracked.txt") == "missed by hook"
    assert path.read_bytes() == before


def test_sweep_missing_columns_warns(plugin, service, repo):
    _, module = modules(plugin)
    database(plugin, repo, missing=True)
    (repo / "tracked.txt").write_text("ledger discovery", encoding="utf-8")
    service.log(dict(kind="repo", repo=str(repo), paths=[str(repo / "tracked.txt")]))
    result = module.sweep(service)
    assert "missing required columns" in result["warnings"][0]
    assert not result["errors"] and len(result["repos_checked"]) == 1


def test_sweep_optional_columns_and_bad_json(plugin, service, repo):
    _, module = modules(plugin)
    path = plugin.core.hermes_home() / "state.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
        db.execute("CREATE TABLE messages (session_id TEXT, tool_calls TEXT)")
        db.execute("INSERT INTO sessions VALUES ('s', ?)", (time.time(),))
        db.execute("INSERT INTO messages VALUES ('s', 'not json')")
    result = module.sweep(service)
    assert "missing optional columns" in result["warnings"][0]
    assert not result["errors"]


@pytest.mark.parametrize("command", ["status", "find", "list", "prune", "sweep"])
@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_all_commands(plugin, service, repo, capsys, command, json_mode):
    cli, _ = modules(plugin)
    database(plugin, repo)
    (repo / "tracked.txt").write_text("snapshot", encoding="utf-8")
    assert service.snapshot_repo(str(repo))["changed"]
    parser = argparse.ArgumentParser()
    cli.setup_cli(parser)
    argv = [command]
    if command == "find":
        argv.append(str(repo / "tracked.txt"))
    elif command == "list":
        argv.append(str(repo))
    elif command == "prune":
        argv.append("--dry-run")
    if json_mode:
        argv.append("--json")
    assert cli.cli_handler(parser.parse_args(argv), service) == 0
    output = capsys.readouterr().out
    if json_mode:
        assert isinstance(json.loads(output), dict)
    elif command == "sweep":
        assert output == ""
    else:
        assert output and all(ord(x) < 128 for x in output)


def test_find_all_versions_and_restore(plugin, service, repo, tmp_path):
    cli, _ = modules(plugin)
    p = repo / "tracked.txt"
    for content in ("version one", "version two"):
        p.write_text(content, encoding="utf-8")
        assert service.snapshot_repo(str(repo))["changed"]
    result = cli.find(service, str(p))
    assert len(result["versions"]) == 2
    for number, version in enumerate(result["versions"], 1):
        assert version["restore"].startswith("hermes autosave restore ")
        restored = cli.restore(service, str(p), number)
        assert restored["restored_to"] == str(p)
        assert p.read_text() in ("version one", "version two")
    outside = tmp_path / "outside with spaces"
    outside.write_text("backup", encoding="utf-8")
    service.backup_file(str(outside))
    result = cli.find(service, str(outside))
    assert len(result["versions"]) == 1
    outside.write_text("mutated", encoding="utf-8")
    assert cli.restore(service, str(outside))["restored_to"] == str(outside)
    assert outside.read_text() == "backup"


def test_sweep_error_stdout_exit_zero(plugin, service, repo, capsys):
    cli, _ = modules(plugin)
    database(plugin, repo, missing=True)
    parser = argparse.ArgumentParser()
    cli.setup_cli(parser)
    assert cli.cli_handler(parser.parse_args(["sweep"]), service) == 0
    assert "Autosave sweep:" in capsys.readouterr().out


def test_sweep_budget_failure(plugin, service, repo):
    _, module = modules(plugin)
    database(plugin, repo)
    result = module.sweep(service, 60, 0.001)
    assert result["errors"] or result["warnings"]


def test_sweep_session_filter_and_ledger_age(plugin, service, repo):
    _, module = modules(plugin)
    path = database(plugin, repo)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE sessions SET started_at=1, last_activity_at=NULL")
    service.log(dict(repo=str(repo), at=1))
    result = module.sweep(service, 1, 8)
    assert result["repos_checked"] == []


def test_cli_bad_numbers(plugin):
    cli, _ = modules(plugin)
    parser = argparse.ArgumentParser()
    cli.setup_cli(parser)
    for value in ("nan", "inf", "-1", "0"):
        with pytest.raises(SystemExit):
            parser.parse_args(["sweep", "--budget-seconds", value])
