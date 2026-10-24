"""A host that reaps every child (waitpid(-1)) must not turn a finished backup into a failure."""
import os
import threading
import time

import pytest

from .conftest import git


@pytest.mark.skipif(os.name == "nt", reason="POSIX waitpid(-1) reaper")
def test_host_reaper_does_not_fail_finished_backups(plugin, service, repo):
    stop = threading.Event()

    def reaper():  # Same pattern as a host dispatcher reaping its own workers.
        while not stop.is_set():
            try:
                os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                pass
            time.sleep(0.0005)

    thread = threading.Thread(target=reaper, daemon=True)
    thread.start()
    try:
        hook = plugin.make_hook(service)
        for i in range(12):
            (repo / "tracked.txt").write_text(f"v{i}\n")
            assert hook("write_file", {"path": str(repo / "tracked.txt")}, session_id="s", turn_id=str(i)) is None
    finally:
        stop.set()
        thread.join(2)
    assert hook.health == {"failures": 0, "last_error": None}
    assert len(git(repo, "reflog", "refs/hermes-autosave/main").splitlines()) == 12
