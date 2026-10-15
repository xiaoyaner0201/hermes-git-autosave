"""The local `hermes autosave` command tree. Every subcommand supports JSON."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

from . import core
from .sweep import sweep


def positive(value):
    import math
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Must be a finite positive number")
    return number


def setup_cli(parser):
    sub = parser.add_subparsers(dest="autosave_cmd")
    for name, help_text in (("status", "Show storage, sizes, recent snapshots and errors"),
                            ("find", "Find every restorable version of a file"),
                            ("restore", "Restore bytes, mode and symlinks without staging"),
                            ("list", "List shadow snapshots in a repository"),
                            ("prune", "Expire snapshots, copies and ledger records; never run git gc"),
                            ("sweep", "Snapshot recently touched repositories")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true", help="Print machine-readable JSON")
        if name in ("find", "restore"):
            command.add_argument("path")
            if name == "restore":
                command.add_argument("--version", dest="saved_version", type=int, default=1,
                                     help="1-based number printed by find")
                command.add_argument("--to", dest="destination", help="Restore to another path")
        elif name == "list":
            command.add_argument("repo", nargs="?", default=".")
        elif name == "prune":
            command.add_argument("--dry-run", action="store_true")
        elif name == "sweep":
            command.add_argument("--since-minutes", type=positive, default=360)
            command.add_argument("--budget-seconds", type=positive, default=150)


def quote(value):
    return subprocess.list2cmdline([str(value)]) if os.name == "nt" else shlex.quote(str(value))


def size(path):
    total = 0
    for parent, _, names in os.walk(path):
        for name in names:
            try:
                total += (Path(parent) / name).lstat().st_size
            except OSError:
                pass
    return total


def status(service):
    ledger = service.read_ledger()
    repos = sorted({r["repo"] for r in ledger if r.get("kind") == "repo" and r.get("changed")})
    return dict(storage_root=str(service.storage), keep_days=service.settings["keep_days"],
                sizes_bytes=dict(files=size(service.files), state=size(service.state),
                                 ledger=service.ledger.stat().st_size if service.ledger.exists() else 0),
                repos=repos, ledger_records=len(ledger), recent_errors=[r for r in ledger if r.get("error")][-10:])


def find(service, path) -> dict:
    path = str(core.canonical_file(core.resolve_path(path)))
    versions = []
    budget = core.Budget(60)
    repo = service.toplevel(str(Path(path).parent), budget)
    if repo and not service.denied(repo):
        rel = Path(path).relative_to(repo).as_posix()
        saved = service.versions(repo, budget)
        for version in saved:
            observed = service.commit_observations(repo, version["commit"], budget)
            if rel not in observed:
                continue
            mode, content_id = observed[rel]
            if mode in {"100644", "100755", "120000"}:
                versions.append(dict(kind="git", **version, rel=rel, mode=mode,
                                     content_id=content_id, revpath=version["commit"] + ":" + rel))
    ledger = service.read_ledger(365000)
    records = {r.get("copy"): r for r in ledger if r.get("kind") == "file"}
    commits = {r["commit"]: int(r["at"] * 1000000000) for r in ledger if r.get("commit") and r.get("changed")}
    for version in versions:
        version["at_ns"] = version.get("at_ns", commits.get(version["commit"], version.get("at", 0) * 1000000000))
    for copy in reversed(service.file_versions(path, budget)):
        record = service.copy_metadata(copy, records)
        versions.append(dict(kind="file", copy=str(copy), mode=record.get("mode", 0o600),
                             content_id=record.get("content_id", record.get("sha256", copy.name.rsplit("-", 1)[-1])),
                             at_ns=service.copy_order(copy)))
    for saved in versions:
        saved["path"] = path
        saved["role"] = "entry"
    # The ledger supplies a hint only, never versions or restore destinations.
    last_write = next((r for r in reversed(ledger) if r.get("kind") == "link-write" and r.get("path") == path), None)
    notes = ([f"Last content write went through this link to {last_write['referent']}; "
              f"run hermes autosave find {quote(last_write['referent'])}"] if last_write else [])
    versions.sort(key=lambda v: v["at_ns"], reverse=True)
    for number, version in enumerate(versions, 1):
        version["version"] = number
        version["restore"] = f"hermes autosave restore {quote(path)} --version {number}"
    return dict(path=path, versions=versions, notes=notes)


def restore(service, path, version=1, destination=None):
    found = find(service, path)
    if version < 1 or version > len(found["versions"]):
        raise ValueError("Requested version does not exist")
    saved = found["versions"][version - 1]
    target = Path(core.resolve_path(destination)) if destination else Path(saved["path"])
    if saved["kind"] == "git":
        content = os.fsencode(service.git(saved["repo"], ["cat-file", "blob", saved["revpath"]]))
        mode = 0o755 if saved["mode"] == "100755" else 0o644
        symlink = saved["mode"] == "120000"
    else:
        content = Path(saved["copy"]).read_bytes()
        mode, symlink = saved["mode"], False
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".autosave-restore-", dir=target.parent)
    try:
        if symlink:
            os.close(fd)
            os.unlink(name)
            os.symlink(os.fsdecode(content), name)
        else:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                os.fchmod(output.fileno(), mode) if hasattr(os, "fchmod") else os.chmod(name, mode)
        os.replace(name, target)  # Replace a destination symlink, never follow it.
    finally:
        if os.path.lexists(name):
            os.unlink(name)
    return dict(path=found["path"], restored_to=str(target), version=version, kind=saved["kind"], role=saved["role"])


def cli_handler(args, service=None):
    service = service or core.Autosave()
    command = getattr(args, "autosave_cmd", None) or "status"
    try:
        if command == "status":
            result = status(service)
        elif command == "find":
            result = find(service, args.path)
        elif command == "restore":
            result = restore(service, args.path, args.saved_version, args.destination)
        elif command == "list":
            repo = core.resolve_path(args.repo)
            repo = service.toplevel(repo) or repo
            result = dict(repo=repo, versions=service.versions(repo))
        elif command == "prune":
            result = service.prune(args.dry_run)
        elif command == "sweep":
            result = sweep(service, args.since_minutes, args.budget_seconds)
        else:
            result = dict(errors=["Unknown autosave command"])
    except Exception as exc:
        result = dict(errors=[f"{type(exc).__name__}: {exc}"])
    if getattr(args, "json", False):
        print(json.dumps(result))
    elif command == "sweep":
        issues = result.get("errors", []) + result.get("warnings", [])
        if issues:
            print("Autosave sweep: " + "; ".join(issues[:10]))
    elif command == "status" and "storage_root" in result:
        print(f"Storage root: {result['storage_root']}")
        print(f"Sizes (bytes): {json.dumps(result['sizes_bytes'])}")
        print(f"Repositories with snapshots in the last {result['keep_days']} days: {len(result['repos'])}")
        for repo in result["repos"]:
            print("  " + repo)
        print("Recent errors: " + json.dumps(result["recent_errors"]))
    elif "versions" in result:
        if not result["versions"]:
            print("No snapshots found.")
        for version in result["versions"]:
            print(version.get("restore") or f"{version['ref']} {version['commit']} {version['at']}")
        for note in result.get("notes", []):
            print(note)
    else:
        print(("Dry run: " if result.get("dry_run") else "") + json.dumps(result))
    # Restore errors are nonzero; sweep errors remain zero for no-agent cron delivery.
    return 1 if command == "restore" and result.get("errors") else 0
