import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_plugin(name="autosave_test_plugin"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def inject_worker(plugin, monkeypatch, code):
    """Instrumentation runs after exec, never replaces the public hook factory."""
    module = importlib.import_module(plugin.__name__ + ".isolation")
    monkeypatch.setattr(module, "BOOTSTRAP", module.BOOTSTRAP.replace("worker.main(", f"exec({code!r}); worker.main("))


def git(repo, *args, env=None):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode(errors="surrogateescape").strip()


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    hh = tmp_path / "hermes"
    hh.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hh))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for key in list(os.environ):
        if key.startswith("GIT_") and key not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def plugin():
    return load_plugin()


@pytest.fixture
def service(plugin, tmp_path):
    return plugin.core.Autosave({"storage_dir": str(tmp_path / "storage")})


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Test Author")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "Baseline")
    return path


def shadow(repo):
    return git(repo, "rev-parse", "refs/hermes-autosave/main")


def tree(repo, sha=None):
    return git(repo, "ls-tree", "-r", "--name-only", sha or shadow(repo)).splitlines()
