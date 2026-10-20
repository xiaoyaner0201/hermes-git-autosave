"""Real registration + filesystem/snapshot faults, with an outer hard deadline."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest


@pytest.mark.parametrize("boundary", ["snapshot", "stat", "read", "log"])
@pytest.mark.parametrize("caller", ["main", "threaded"])
def test_blocked_hook_is_reaped(repo, tmp_path, boundary, caller):
    probe = Path(__file__).with_name("hook_probe.py")
    process = subprocess.Popen([sys.executable, str(probe), str(tmp_path), boundary, caller],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        try:
            stdout, stderr = process.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            pytest.fail("Hook exceeded outer 4s deadline (configured budget 0.6s)")
        assert process.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        assert len(result["elapsed"]) == 3
        assert all(t < 0.85 for t in result["elapsed"]), result
        owned = tmp_path / "owned"
        assert len(owned.read_text().splitlines()) == 3
        for pid in map(int, owned.read_text().splitlines()):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        # Each PID comes exclusively from this synthetic probe; never enumerate/kill other Git.
        owned = tmp_path / "owned"
        if owned.exists():
            for pid in map(int, owned.read_text().splitlines()):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=1)
