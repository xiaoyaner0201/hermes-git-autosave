"""Standalone fault probe. Only synthetic HOME/repos; supervised by test_bounded_hook."""
import importlib.util
import types
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def load():
    base = os.environ.get("AUTOSAVE_PROBE_BASE")
    if base:
        assert base == "b4b12d6bf79318c6708ab468327d52e7671d63f9"
        package = types.ModuleType("probe_plugin")
        package.__path__ = [str(ROOT)]
        package.__package__ = "probe_plugin"
        sys.modules[package.__name__] = package
        for name in ("core", "hooks", "__init__"):
            source = subprocess.run(["git", "-C", str(ROOT), "show", f"{base}:{name}.py"],
                                    capture_output=True, check=True, timeout=2).stdout
            module = package if name == "__init__" else types.ModuleType("probe_plugin." + name)
            module.__package__ = "probe_plugin"
            sys.modules[module.__name__] = module
            if name != "__init__":
                setattr(package, name, module)
            exec(compile(source, str(ROOT / (name + ".py")), "exec"), module.__dict__)
        return package
    spec = importlib.util.spec_from_file_location("probe_plugin", ROOT / "__init__.py",
                                                 submodule_search_locations=[str(ROOT)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


def inject(plugin, directory, boundary):
    def blocked(*args, **kwargs):
        with open(Path(directory) / "owned", "a") as stream:
            stream.write(str(os.getpid()) + "\n")
        time.sleep(60)
    if boundary == "snapshot":
        plugin.core.Autosave._snapshot = blocked  # Inside the real repo lock.
    elif boundary == "stat":
        plugin.core.os.lstat = blocked
    elif boundary == "read":
        if hasattr(plugin.core, "read_regular"):
            plugin.core.read_regular = blocked
        else:
            plugin.core.Path.read_bytes = blocked
    elif boundary == "log":
        plugin.core.Autosave.log = blocked


def main():
    directory, boundary = sys.argv[1:3]
    directory = Path(directory)
    plugin = load()
    # New implementation: inject into the exec'd interpreter, not a fake success path.
    try:
        if os.environ.get("AUTOSAVE_PROBE_BASE"):
            raise ImportError("base has no isolated worker")
        from probe_plugin import isolation
    except ImportError:
        inject(plugin, directory, boundary)
    else:
        original = isolation.BOOTSTRAP
        injection = (f"import runpy; probe=runpy.run_path({str(Path(__file__).resolve())!r}); "
                     f"probe['inject'](plugin, {str(directory)!r}, {boundary!r}); ")
        isolation.BOOTSTRAP = original.replace("worker.main(", injection + "worker.main(")
    settings = {"storage_dir": str(directory / "storage"), "hook_budget_seconds": 0.6}
    class Context:
        def get_config(self, key, default=None):
            return settings.get(key, default)
        def register_hook(self, name, callback):
            self.hook = callback
        def register_cli_command(self, *args, **kwargs):
            raise NotImplementedError
    ctx = Context()
    plugin.register(ctx)
    target = directory / "repo" / "tracked.txt"
    if boundary == "read":
        target = directory / "standalone"
        target.write_bytes(b"exact pre-edit bytes\\x00")
    elapsed = []
    def invoke():
        start = time.monotonic()
        assert ctx.hook("write_file", {"path": str(target)}) is None
        elapsed.append(time.monotonic() - start)
    for _ in range(3):
        if sys.argv[3:] == ["threaded"]:
            thread = threading.Thread(target=invoke)
            thread.start()
            assert ctx.hook("read_file", {"path": str(target)}) is None
            thread.join(2)
            assert not thread.is_alive()
        else:
            invoke()
        assert ctx.hook("read_file", {"path": str(target)}) is None
    if 'isolation' in locals():
        isolation.BOOTSTRAP = original
    else:
        raise AssertionError("Unbounded candidate unexpectedly survived fault")
    # A normal full snapshot gets the ordinary production budget.
    if 'isolation' in locals():
        settings["hook_budget_seconds"] = 8
        plugin.register(ctx)
    assert ctx.hook("write_file", {"path": str(target)}) is None
    service = plugin.core.Autosave(settings)
    assert service.file_versions(str(target)) if boundary == "read" else service.versions(str(target.parent))
    # The ordinary worker and diagnostic writer must both have been reaped.
    try:
        child, _ = os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        child = None
    assert child is None, f"Unreaped owned child: {child}"
    print(json.dumps({"elapsed": elapsed, "normal_snapshot": True}), flush=True)


if __name__ == "__main__":
    main()
