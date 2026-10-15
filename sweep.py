"""Best-effort discovery from a private, version-dependent, read-only session schema."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time

from . import core
from .hooks import targets


def timestamp(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0


def touched_paths(db_path, since, budget):
    files, dirs, warnings = set(), set(), []
    if not Path(db_path).exists():
        return files, dirs, ["Session database is unavailable; using the plugin ledger only"]
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: int(time.monotonic() >= budget.deadline), 1000)
            columns = {table: {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                       for table in ("sessions", "messages")}
            sc, mc = columns["sessions"], columns["messages"]
            if not {"id", "started_at"} <= sc or not {"session_id", "tool_calls"} <= mc:
                return files, dirs, ["Session database schema is missing required columns; using the plugin ledger only"]
            optional = ((sc, "last_activity_at"), (sc, "cwd"), (mc, "timestamp"), (mc, "role"))
            missing = [name for cols, name in optional if name not in cols]
            if missing:
                warnings.append("Session database schema missing optional columns: " + ", ".join(missing))
            activity = "COALESCE(last_activity_at, started_at)" if "last_activity_at" in sc else "started_at"
            cwd = "cwd" if "cwd" in sc else "NULL"
            # Read numeric and ISO timestamps alike. Keep schema assumptions localized here.
            for sid, base, last in db.execute(f"SELECT id, {cwd}, {activity} FROM sessions"):
                budget.left()
                if timestamp(last) < since:
                    continue
                query = "SELECT substr(CAST(tool_calls AS BLOB), 1, 1048577), length(CAST(tool_calls AS BLOB))" + (
                    ", timestamp" if "timestamp" in mc else "") + " FROM messages WHERE session_id=? AND tool_calls IS NOT NULL"
                if "role" in mc:
                    query += " AND role='assistant'"
                for row in db.execute(query, (sid,)):
                    budget.left()
                    if len(row) > 2 and timestamp(row[2]) < since:
                        continue
                    if row[1] > 1048576:
                        warnings.append("Session message exceeds the 1 MiB decoding limit; skipped")
                        continue
                    try:
                        calls = json.loads(row[0])
                    except (TypeError, ValueError):
                        continue
                    for call in calls if isinstance(calls, list) else []:
                        budget.left()
                        if not isinstance(call, dict):
                            continue
                        fn = call.get("function") or {}
                        if not isinstance(fn, dict):
                            continue
                        args = fn.get("arguments", {})
                        try:
                            args = json.loads(args) if isinstance(args, str) else args
                            if not isinstance(args, dict):
                                continue
                            f, d = targets(fn.get("name"), args, base=base or core.agent_cwd(), all_terminal=True)
                            files.update(f)
                            dirs.update(d)
                        except (ValueError, TypeError, OSError):
                            continue
    except Exception as exc:
        warnings.append(f"Session database discovery failed: {type(exc).__name__}: {exc}")
    budget.left()
    return files, dirs, warnings


def sweep(service, since_minutes=360, budget_seconds=150):
    started = time.time()
    budget = core.Budget(budget_seconds)
    result = dict(touched=0, repos_checked=[], files_copied=[], errors=[], warnings=[])
    try:
        files, dirs, warnings = touched_paths(core.hermes_home() / "state.db", started - since_minutes * 60, budget)
        result["warnings"].extend(warnings)
        result["touched"] = len(files | dirs)
        repos = {}
        for record in service.read_ledger(since_minutes / 1440, budget):
            budget.left()
            if record.get("repo"):
                repos.setdefault(record["repo"], set()).update(record.get("paths") or [])
        for path in sorted(files | dirs):
            budget.left()
            kind, repo = service.classify(path, budget)
            if kind == "git":
                repos.setdefault(repo, set()).add(path)
            elif kind == "file" and path in files:
                copy = service.backup_file(path, "Sweep", budget)
                if copy:
                    result["files_copied"].append(copy)
        for repo, paths in sorted(repos.items()):
            budget.left()
            if not Path(repo).is_dir() or service.denied(repo) or service.skip(repo, tracked=True):
                continue
            record = service.snapshot_repo(repo, sorted(paths), "Sweep", budget)
            result["repos_checked"].append(record)
            if record.get("error"):
                result["errors"].append(f"{repo}: {record['error']}")
        # File-copy failures are fail-open, but sweep must still disclose them.
        result["errors"].extend(r["error"] for r in service.read_ledger(1, budget)
                                if r.get("kind") == "file" and r.get("at", 0) >= started and r.get("error"))
        budget.left()
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    result["seconds"] = round(time.time() - started, 3)
    service.log(dict(kind="sweep", **result))
    return result
