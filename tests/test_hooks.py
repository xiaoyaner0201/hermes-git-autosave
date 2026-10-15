import importlib
import os

import pytest

from .conftest import shadow

DESTRUCTIVE = [
    "rm file", "rm -rf folder", "rmdir folder", "unlink file", "mv old new", "sudo rm file",
    "cp source target", "cp -r source target", "rsync -a source/ target/", "truncate -s 0 file",
    "echo x > file", "printf x >> file", "cat input > output", "sed -i 's/a/b/' file",
    "sed -i.bak 's/a/b/' file", "perl -pi -e 's/a/b/' file", "git checkout -- file",
    "git checkout HEAD -- file", "git restore file", "git restore --staged file", "git reset --hard",
    "git reset --hard HEAD~1", "git clean -fd", "git stash", "git rebase main", "git switch -f main",
    "git switch --force main", "find . -name '*.pyc' -delete", "find . -exec rm {} +",
    "printf x | tee file", "cd repo && rm file", "true; mv a b", "command rm file",
    "git checkout main", "git checkout .", "git reset HEAD~1", "git reset --soft HEAD~1",
    "git rm a", "git mv a b", "git apply p.diff", "git am x.patch", "git merge x", "git pull",
    "git cherry-pick abc", "git revert abc", "git switch main", "dd if=/dev/zero of=x", "shred -u f",
    "install -m 644 a b", "patch -p1 < x.diff", "unzip -o a.zip", "tar xf a.tar", "tar -xzf a.tar.gz",
    "tar --extract --file=a.tar", "tar -f a.tar --extract", "perl -i -e 's/a/b/' file",
    "find . -exec /bin/rm {} +", "find . -execdir rm {} +", "git -C repo checkout main",
    "git -c core.autocrlf=false reset HEAD~1", "git --no-pager checkout main",
]
BENIGN = [
    "git status", "git diff", "git log --oneline", "git show HEAD:file", "git branch", "git rev-parse HEAD",
    "git ls-files", "pwd", "ls -la", "cat file", "sed -n '1p' file", "find . -name '*.py'",
    "echo 'rm file'", "printf '>'", "python --version", "rg pattern .", "cd repo && git status",
    "tee -a log", "echo x > " + os.devnull, "cat f > /dev/null", "2>/dev/null ls", "make -n",
    "tar tf archive.tar", "tar -czf archive.tar src", "tee --append log",
]


def hooks(plugin):
    return importlib.import_module(plugin.__name__ + ".hooks")


@pytest.mark.parametrize("command", DESTRUCTIVE)
def test_destructive_commands(plugin, command):
    assert hooks(plugin).destructive(command), command


@pytest.mark.parametrize("command", BENIGN)
def test_benign_commands(plugin, command):
    assert not hooks(plugin).destructive(command), command


def test_terminal_trigger_and_cwd(plugin, service, repo, tmp_path):
    h = plugin.make_hook(service)
    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")
    assert h("terminal", {"command": "git status", "workdir": str(repo)}) is None
    assert not service.refs(str(repo))
    assert h("terminal", {"command": "rm tracked.txt", "workdir": str(repo)}) is None
    assert shadow(repo)
    module = hooks(plugin)
    command = {'command': 'cd "repo" && rm file'}
    assert module.terminal_cwd(command, str(tmp_path)) == str(repo)
    command["workdir"] = str(tmp_path)
    assert module.terminal_cwd(command, str(tmp_path)) == str(repo)  # workdir is the initial cwd; cd applies next.


def test_patch_v4a(plugin, service, tmp_path):
    files = [tmp_path / name for name in ("update", "delete", "move", "destination")]
    for p in files:
        p.write_text("pre-edit", encoding="utf-8")
    body = "\n".join(["*** Begin Patch", "*** Update File: update", "*** Delete File: delete",
                      "*** Move File: move", "*** Move to: destination", "*** Add File: new", "*** End Patch"])
    args = {"mode": "patch", "patch": body}
    extracted, dirs = hooks(plugin).targets("patch", args, str(tmp_path))
    assert extracted == [str(p) for p in files] + [str(tmp_path / "new")]
    assert dirs == []
    plugin.make_hook(service)("patch", args)
    assert all(len(service.file_versions(str(p))) == 1 for p in files)


def test_register_settings_and_isolated_host(plugin, tmp_path):
    class Context:
        def __init__(self):
            self.read = []
            self.callback = None
        def get_config(self, key, default=None):
            self.read.append(key)
            return str(tmp_path / "override") if key == "storage_dir" else default
        def register_hook(self, name, callback):
            assert name == "pre_tool_call"
            self.callback = callback
        def register_cli_command(self, *args, **kwargs):
            raise NotImplementedError("Isolated host")
    ctx = Context()
    plugin.register(ctx)
    assert set(ctx.read) == set(plugin.core.DEFAULTS)
    p = tmp_path / "outside"
    p.write_text("before", encoding="utf-8")
    assert ctx.callback("patch", {"path": str(p)}) is None
    configured = plugin.core.Autosave({"storage_dir": str(tmp_path / "override")})
    versions = configured.file_versions(str(p))
    assert len(versions) == 1 and versions[0].read_bytes() == b"before"


def test_register_cli_tree(plugin, tmp_path):
    import argparse
    class Context:
        def get_config(self, key, default=None):
            return str(tmp_path / "override") if key == "storage_dir" else default
        def register_hook(self, *args):
            pass
        def register_cli_command(self, name, **kwargs):
            assert name == "autosave"
            parser = argparse.ArgumentParser()
            kwargs["setup_fn"](parser)
            assert kwargs["handler_fn"](parser.parse_args(["status", "--json"])) == 0
    plugin.register(Context())
