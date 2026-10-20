"""Fresh-interpreter entry point. All filesystem work and durable logging lives here."""
import json
import os
from pathlib import Path
import socket
import sys
import time

from . import core
from .hooks import make_inline_hook
from .isolation import MAX_REPLY, MAX_REQUEST


def main():
    channel = socket.socket(fileno=int(sys.argv[2]))
    data = bytearray()
    while True:
        part = channel.recv(65536)
        if not part:
            break
        data.extend(part)
        if len(data) > MAX_REQUEST:
            return
    request = json.loads(data)
    if request.get("home"):
        os.environ["HERMES_HOME"] = request["home"]
    service = core.Autosave(request["settings"])
    service._invalid = request["invalid_settings"]
    service._warned = request["settings_warned"]
    if request["root"]:
        service.root = Path(request["root"])
    # Child Git belongs to the worker's process group, not independent sessions.
    service._hook_worker = True
    service._large_command_warned = request["large_warned"]
    session, terminal = request["cwd"]
    def cwd():
        for candidate in (session, terminal):
            if candidate and Path(candidate).expanduser().is_dir():
                return str(Path(candidate).expanduser())
        return os.getcwd()
    core.agent_cwd = cwd
    seen = {tuple(key): set(paths) for key, paths in request["seen"]}
    # Budget covers interpreter startup/IPC too, not another full budget after exec.
    original_budget = core.Budget
    core.Budget = lambda seconds=8: original_budget(deadline=request["deadline"])
    error = None
    original_log = service.log
    def log(record, *args, **kwargs):
        nonlocal error
        if record.get("error") or record.get("skipped"):
            error = "Backup incomplete; inspect ledger"
        original_log(record, *args, **kwargs)
    service.log = log
    service.warn_settings()
    make_inline_hook(service, seen)(request["tool"], request["args"], task_id=request["task_id"],
                                    session_id=request["session_id"], turn_id=request["turn_id"])
    if time.monotonic() >= request["deadline"]:
        return
    response = dict(ok=True, large_warned=service._large_command_warned, settings_warned=service._warned,
                    seen=[[list(key), sorted(paths)] for key, paths in seen.items()], error=error)
    encoded = json.dumps(response).encode()
    if len(encoded) > MAX_REPLY:
        encoded = json.dumps(dict(ok=True, seen=[], error=error, settings_warned=service._warned)).encode()
    channel.sendall(encoded)
    channel.close()
