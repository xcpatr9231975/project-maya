"""tools/test_engine_room.py - the server starts ONE engine at a time.

    python -m unittest tools.test_engine_room

Two engines do not fit on this box: one expert pool of 65 GB each in 119 GB of shared
memory.  The newcomer that finds the device full is refused by the driver
(NV_ERR_NO_MEMORY - the warning shot before a hard lock), and the guard then kills the
engine that was working.  So a start waits for the GPU to be free of our engine, and an
engine the server does not track (an orphan of an earlier start) is ended rather than
left holding the pool.

Hermetic: gpu_pids, the process command lines and the clock are all mocked, and no
engine is ever started.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve.server import StrataEngine  # noqa: E402

EXE = "/home/cpatriaw/projects/maya/build/maya"
OTHER = "/usr/bin/python3 /home/cpatriaw/projects/gpu-dash/server.py"


def engine(*a, **kw):
    """A StrataEngine that never starts anything: only what the room check needs."""
    e = object.__new__(StrataEngine)
    e.spawn = (EXE, list(a), kw.get("cwd"), kw.get("log"), kw.get("env"))
    return e


class EngineRoom(unittest.TestCase):
    def test_the_gpu_is_free(self):
        e = engine()
        with patch("serve.telemetry.gpu_pids", return_value={4242}), \
             patch("serve.server._proc_cmdline", return_value=OTHER):
            self.assertEqual(e._engine_pids(), set())

    def test_only_our_engine_binary_counts(self):
        e = engine()
        with patch("serve.telemetry.gpu_pids", return_value={4242, 4243}), \
             patch("serve.server._proc_cmdline", side_effect=lambda pid: EXE if pid == 4243 else OTHER):
            self.assertEqual(e._engine_pids(), {4243})

    def test_a_start_waits_for_the_engine_to_let_go(self):
        """The old engine's memory comes back a few seconds after the process is gone: the start waits for it."""
        e = engine()
        seen = []

        def pids():
            seen.append(1)
            return {4243} if len(seen) < 4 else set()

        with patch("serve.telemetry.gpu_pids", side_effect=pids), \
             patch("serve.server._proc_cmdline", return_value=EXE), \
             patch("serve.server.time.sleep", lambda s: None):
            e._make_room(timeout=5, wait=5, sigkill_after=5)
        self.assertGreaterEqual(len(seen), 4, "the start did not wait for the GPU to be free")

    def test_an_engine_we_do_not_track_is_ended(self):
        """An orphan of an earlier start serves nothing we know of and keeps the new engine from fitting: it is
        ended the way the guard ends one - SIGTERM first (the engine frees the pool), SIGKILL after."""
        e = engine()
        killed = []
        gone = {"now": False}

        def pids():
            return set() if gone["now"] else {4243}

        def kill(pid, sig):
            killed.append((pid, sig))
            if sig == 15:
                gone["now"] = True          # the engine ended: the driver frees its memory

        with patch("serve.telemetry.gpu_pids", side_effect=pids), \
             patch("serve.server._proc_cmdline", return_value=EXE), \
             patch("serve.server.os.kill", side_effect=kill), \
             patch("serve.server.time.sleep", lambda s: None):
            e._make_room(timeout=5, wait=0, sigkill_after=5)
        self.assertTrue(killed, "the stray engine was left holding the GPU")
        self.assertEqual(killed[0], (4243, 15), "it was not ended with SIGTERM first")

    def test_a_free_gpu_starts_at_once(self):
        e = engine()
        with patch("serve.telemetry.gpu_pids", return_value=set()), \
             patch("serve.server.time.sleep", lambda s: None):
            e._make_room(timeout=5, wait=0, sigkill_after=0)      # returns at once: nothing to wait for

    def test_the_start_spawns_nothing_before_the_room_is_free(self):
        """The regression: the engine's process is started only after the GPU is free of our engine."""
        e = engine()
        order = []
        held = {"now": True}

        def pids():
            return {4243} if held["now"] else set()

        def fake_popen(*a, **kw):
            order.append(("popen", pids()))
            return None

        with patch("serve.telemetry.gpu_pids", side_effect=pids), \
             patch("serve.server._proc_cmdline", return_value=EXE), \
             patch("serve.server.subprocess.Popen", side_effect=fake_popen), \
             patch("serve.server.time.sleep", lambda s: (held.update(now=False) if s >= 0 else None)), \
             patch("serve.server.threading.Thread"):
            try:
                StrataEngine(EXE, [], log=None)
            except Exception:  # noqa: BLE001 - the fake Popen has no process to read from
                pass
        self.assertTrue(order, "the engine was never started")
        self.assertEqual(order[0][1], set(), "the engine was started while another one held the GPU")


if __name__ == "__main__":
    unittest.main()
