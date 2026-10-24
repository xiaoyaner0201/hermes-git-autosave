"""Host-side bounded IPC. No repository, state, ledger or source-path filesystem I/O here."""
import json
from multiprocessing.util import spawnv_passfds
import os
import selectors
import signal
import socket
import sys
import threading
import time

MAX_REQUEST = 1024 * 1024
MAX_REPLY = 256 * 1024
# Source location is lexical: resolving it in the host could itself stall.
ROOT = os.path.dirname(__file__)
BOOTSTRAP = (
    "import os; os.setsid(); os.close(1); os.close(2); "
    "import importlib.util,sys; "
    "root=sys.argv[1]; "
    "spec=importlib.util.spec_from_file_location('_autosave_worker',root+'/__init__.py',"
    "submodule_search_locations=[root]); "
    "plugin=importlib.util.module_from_spec(spec); sys.modules[spec.name]=plugin; "
    "spec.loader.exec_module(plugin); from _autosave_worker import worker; worker.main()"
)


def context_cwd():
    # Do not import host modules (imports can do filesystem I/O). The public logical
    # accessors don't stat paths, unlike resolve_agent_cwd(). Validate in the worker.
    runtime = sys.modules.get("agent.runtime_cwd")
    if runtime is None:
        return "", os.environ.get("TERMINAL_CWD", "")
    return (runtime.scoped_session_cwd(), runtime.scope_terminal_cwd())


def home_override():
    constants = sys.modules.get("hermes_constants")
    getter = getattr(constants, "get_hermes_home_override", None)
    return getter() if getter is not None else None


def exchange(payload, deadline):
    parent, child = socket.socketpair()
    pid = None
    status = None
    try:
        # CPython's spawn helper returns the PID without waiting on the exec
        # error pipe (Popen/posix_spawn may wait for a stalled exec). No Python
        # runs between fork and exec, no preexec_fn or multiprocessing tracker.
        # -I -S prevents user startup code/import paths before setsid.
        pid = spawnv_passfds(os.fsencode(sys.executable),
                             [sys.executable, "-I", "-S", "-c", BOOTSTRAP, ROOT, str(child.fileno())],
                             [child.fileno()])
        child.close()
        parent.setblocking(False)
        received = bytearray()
        offset = 0
        with selectors.DefaultSelector() as selector:
            selector.register(parent, selectors.EVENT_WRITE | selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("worker deadline")
                events = selector.select(remaining)
                if not events:
                    raise TimeoutError("worker deadline")
                for _, mask in events:
                    if mask & selectors.EVENT_WRITE and offset < len(payload):
                        try:
                            offset += parent.send(payload[offset:offset + 65536])
                        except BlockingIOError:
                            pass
                        if offset == len(payload):
                            parent.shutdown(socket.SHUT_WR)
                            selector.modify(parent, selectors.EVENT_READ)
                    if mask & selectors.EVENT_READ:
                        try:
                            data = parent.recv(65536)
                        except BlockingIOError:
                            continue
                        if not data:
                            result = json.loads(received)
                            if result.get("ok") is not True:
                                raise RuntimeError("worker failed")
                            return result
                        received.extend(data)
                        if len(received) > MAX_REPLY:
                            raise ValueError("oversized worker reply")
    finally:
        parent.close()
        child.close()
        if pid is not None:
            # Git children in a hook stay in this group, even if pipes were closed.
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                # Exec/startup may have stalled before setsid. This PID is ours;
                # never kill the inherited (host) group.
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            # Never wait indefinitely for a kernel-uninterruptible process. SIGKILL
            # releases ordinary filesystem waits/locks; kernel D-state is an OS limit.
            end = time.monotonic() + 0.05
            while time.monotonic() < end:
                try:
                    status, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    break  # A host-wide waitpid(-1) reaper already collected it.
                if status:
                    break
                time.sleep(0.001)


def report_failure():
    """A short-lived writer: a blocked stderr must not strand a host thread."""
    pid = None
    fd = None
    try:
        fd = os.dup(sys.stderr.fileno())
        code = ("import os,sys; os.setsid(); os.write(int(sys.argv[1]), "
                "b'git-autosave: backup incomplete (timeout/error); recovery is not guaranteed.\\n')")
        pid = spawnv_passfds(os.fsencode(sys.executable),
                             [sys.executable, "-I", "-S", "-c", code, str(fd)], [fd])
        end = time.monotonic() + 0.08
        while time.monotonic() < end:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                done = pid  # Reaped by a host-wide waitpid(-1) reaper.
            if done:
                pid = None
                return
            time.sleep(0.001)
    except Exception:
        pass
    finally:
        if fd is not None:
            os.close(fd)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
                # Bounded polling only, as for the main worker.
                end = time.monotonic() + 0.02
                while time.monotonic() < end:
                    done, _ = os.waitpid(pid, os.WNOHANG)
                    if done:
                        break
                    time.sleep(0.001)
            except (ProcessLookupError, ChildProcessError):
                pass


def make_hook(service):
    seen = []
    large_warned = False
    mutex = threading.Lock()
    # Visible in-memory diagnostic even if both storage and host log sinks are stuck.
    health = {"failures": 0, "last_error": None}

    def on_pre_tool_call(tool_name, args, task_id="", **kwargs):
        nonlocal seen, large_warned
        if tool_name not in ("write_file", "patch", "terminal") or not isinstance(args, dict):
            return None
        seconds = min(12, service.settings["hook_budget_seconds"])
        deadline = time.monotonic() + seconds
        acquired = False
        try:
            acquired = mutex.acquire(timeout=max(0, seconds - 0.06))
            if not acquired:
                raise TimeoutError("busy hook")
            # Never send new file contents, replacement strings or unrelated args.
            fields = {"write_file": ("path",), "patch": ("path", "mode", "patch"),
                      "terminal": ("command", "workdir")}[tool_name]
            # None is an omitted optional argument in the original target/cwd logic.
            selected = {k: args[k] for k in fields if k in args and args[k] is not None}
            if isinstance(selected.get("command"), str) and len(selected["command"]) > 65536:
                selected["command"] = "x" * 65537  # Same conservative cwd-only policy; no raw command in IPC.
            if any(not isinstance(value, str) or len(value) > MAX_REQUEST // 4 for value in selected.values()):
                raise ValueError("oversized/invalid hook arguments")
            request = dict(settings=service.settings, root=str(service.root) if service.root else None,
                           invalid_settings=service._invalid, settings_warned=service._warned,
                           tool=tool_name, args=selected, task_id=task_id,
                           session_id=kwargs.get("session_id", ""), turn_id=kwargs.get("turn_id", ""),
                           seen=seen, large_warned=large_warned, deadline=deadline - 0.16, cwd=context_cwd(), home=home_override())
            payload = json.dumps(request).encode()
            if len(payload) > MAX_REQUEST:
                raise ValueError("oversized hook request")
            result = exchange(payload, deadline - 0.06)
            seen = result.get("seen", [])
            large_warned = result.get("large_warned", large_warned)
            service._warned = result.get("settings_warned", service._warned)
            health["last_error"] = result.get("error")
            if health["last_error"]:
                health["failures"] += 1
                report_failure()
        except Exception as exc:
            # No args or exception text: errors may contain secrets. No FS logging
            # here; the worker handles the durable ledger under the same deadline.
            seen = []
            health["failures"] += 1
            health["last_error"] = type(exc).__name__
            report_failure()
        finally:
            if acquired:
                mutex.release()
        return None

    on_pre_tool_call.health = health
    return on_pre_tool_call
