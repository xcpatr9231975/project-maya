"""Focused tests for the stall / liveness guard in gpu_monitor.py.

The stall guard covers the failure the memory guard cannot see: the engine
stops making progress while the GPU stays pegged and device memory is still
healthy -- the signature the box hard-locked from on 2026-10-24/25.

    python -m unittest tools.test_gpu_monitor_stall
"""
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "gpu_monitor_stall_under_test", Path(__file__).with_name("gpu_monitor.py"))
gpu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gpu)


class _FakeStat:
    def __init__(self, mtime):
        self.st_mtime = mtime
        self.st_mtime_ns = int(mtime * 1e9)


class StallGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        gpu.STALL_STATE_PATH = os.path.join(self.tmp, "stall_state")
        gpu.STALL_TAIL_PATH = os.path.join(self.tmp, "engine_tail")
        gpu.STALL_SNAP_DIR = self.tmp
        gpu.STATE_PATH = os.path.join(self.tmp, "state")
        self.log = os.path.join(self.tmp, "engine.log")
        with open(self.log, "w") as f:
            f.write("[maya] thinking: 1 of max 32000 tokens\n")
        self.pid = str(os.getpid())

    # -- helpers -----------------------------------------------------------
    def _stale(self, seconds):
        """Patch os.stat so the engine log looks `seconds` old."""
        real_stat = os.stat
        mtime = time.time() - seconds

        def fake_stat(path, *a, **k):
            if path == self.log:
                return _FakeStat(mtime)
            return real_stat(path, *a, **k)

        return patch.object(gpu.os, "stat", side_effect=fake_stat)

    def _run(self, util, *, quiet_s=None, procs=None, mem_state="",
             alive=True, starttime=4242):
        procs = procs if procs is not None else [(self.pid, 80000.0, "maya")]
        quiet_s = gpu.STALL_QUIET_S + 30 if quiet_s is None else quiet_s
        with patch.object(gpu, "_engine_log_file", return_value=self.log), \
             patch.object(gpu, "_starttime", return_value=starttime), \
             patch.object(gpu, "_alive", return_value=alive), \
             patch.object(gpu, "_signal_pid", return_value=True) as send, \
             patch.object(gpu, "_state", return_value=mem_state), \
             patch.object(gpu, "_set_state") as set_state, \
             patch.object(gpu, "_set_stall_state") as set_stall, \
             patch.object(gpu, "guard_log") as log, \
             self._stale(quiet_s):
            tag = gpu.stall_guard(util, 84.0, procs, 15000, {"MemAvailable": 20 << 20})
        return tag, send, set_stall, set_state, log

    # -- discovery ---------------------------------------------------------
    def test_engine_log_file_follows_the_engines_own_stderr(self):
        real_listdir, real_readlink, real_stat = os.listdir, os.readlink, os.stat

        with patch.object(gpu.os, "listdir",
                          side_effect=lambda p: ["0", "1", "2"] if str(p).endswith("/fd") else real_listdir(p)), \
             patch.object(gpu.os, "readlink",
                          side_effect=lambda p: {"/proc/99/fd/0": "/dev/null",
                                                 "/proc/99/fd/1": "pipe:[1234]",
                                                 "/proc/99/fd/2": self.log}.get(p, "")), \
             patch.object(gpu.os.path, "isfile", side_effect=lambda p: p == self.log), \
             patch.object(gpu.os, "stat", side_effect=lambda p, *a, **k: real_stat(self.log)):
            self.assertEqual(gpu._engine_log_file(99), self.log)

    def test_engine_log_file_returns_none_when_every_fd_is_a_pipe(self):
        with patch.object(gpu.os, "listdir", return_value=["1", "2"]), \
             patch.object(gpu.os, "readlink",
                          side_effect=lambda p: "pipe:[1234]"):
            self.assertIsNone(gpu._engine_log_file(99))

    # -- log tail ----------------------------------------------------------
    def test_log_tail_strips_nul_padding_and_keeps_the_last_lines(self):
        with open(self.log, "wb") as f:
            f.write(b"[maya] thinking: 1\n\x00\x00\x00\x00[maya] thinking: 2\n")
        self.assertEqual(gpu._log_tail(self.log, 5),
                         ["[maya] thinking: 1", "[maya] thinking: 2"])
        self.assertEqual(gpu._log_tail(self.log, 1), ["[maya] thinking: 2"])
        self.assertEqual(gpu._log_tail(os.path.join(self.tmp, "gone"), 5), [])

    # -- the false-positive gates -----------------------------------------
    def test_idle_gpu_never_stalls_a_silent_engine(self):
        tag, send, set_stall, _, _ = self._run(4.0, quiet_s=100000)
        self.assertIsNone(tag)
        send.assert_not_called()
        set_stall.assert_not_called()

    def test_pegged_gpu_with_a_fresh_log_does_not_stall(self):
        tag, send, _, _, _ = self._run(95.0, quiet_s=1.0)
        self.assertIsNone(tag)
        send.assert_not_called()

    def test_no_compute_processes_means_nothing_to_watch(self):
        tag, send, _, _, _ = self._run(95.0, procs=[])
        self.assertIsNone(tag)
        send.assert_not_called()

    def test_unwatchable_log_keeps_the_guard_out_of_the_way(self):
        procs = [(self.pid, 80000.0, "maya")]
        with patch.object(gpu, "_engine_log_file", return_value=None), \
             patch.object(gpu, "STALL_LOG_PATH", ""), \
             patch.object(gpu, "_signal_pid") as send:
            self.assertIsNone(gpu.stall_guard(95.0, 84.0, procs, 15000, {}))
        send.assert_not_called()

    def test_disabled_guard_is_a_no_op(self):
        with patch.object(gpu, "STALL_ENABLED", False), \
             patch.object(gpu, "_signal_pid") as send:
            self.assertIsNone(gpu.stall_guard(95.0, 84.0,
                                              [(self.pid, 1.0, "maya")], 0, {}))
        send.assert_not_called()

    # -- the stall itself --------------------------------------------------
    def test_silent_engine_on_a_pegged_gpu_is_termed_and_snapshotted(self):
        tag, send, set_stall, _, log = self._run(95.0)
        self.assertEqual(tag, "stall:terming")
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], self.pid)
        self.assertTrue(send.call_args.args[1].name, "SIGTERM")
        state = set_stall.call_args.args[0]
        self.assertTrue(state.startswith(f"stall-termed:{self.pid}:"), state)
        snaps = [f for f in os.listdir(self.tmp) if f.startswith("stall_")]
        self.assertEqual(len(snaps), 1)
        with open(os.path.join(self.tmp, snaps[0])) as f:
            body = f.read()
        self.assertIn("engine log silent", body)
        self.assertIn("[maya] thinking: 1 of max 32000 tokens", body)
        self.assertIn("STALL", log.call_args.args[0])
        # the rolling tail is kept even on a healthy sample
        self.assertTrue(os.path.exists(gpu.STALL_TAIL_PATH))

    def test_stalled_engine_that_exits_clears_the_state(self):
        gpu._set_stall_state(f"stall-termed:{self.pid}:4242:{time.time() - 1}")
        with patch.object(gpu, "_starttime", return_value=4242), \
             patch.object(gpu, "_alive", return_value=False), \
             patch.object(gpu, "_set_stall_state") as set_stall, \
             patch.object(gpu, "guard_log") as log:
            self.assertIsNone(gpu.stall_guard(95.0, 84.0,
                                              [(self.pid, 1.0, "maya")], 0, {}))
        self.assertEqual(set_stall.call_args.args[0], "")
        self.assertIn("exited after the stall SIGTERM", log.call_args.args[0])

    def test_stalled_engine_is_escalated_to_sigkill_after_the_grace(self):
        gpu._set_stall_state(
            f"stall-termed:{self.pid}:4242:{time.time() - gpu.STALL_GRACE_S - 1}")
        with patch.object(gpu, "_starttime", return_value=4242), \
             patch.object(gpu, "_alive", return_value=True), \
             patch.object(gpu, "_signal_pid", return_value=True) as send, \
             patch.object(gpu, "_set_stall_state") as set_stall, \
             patch.object(gpu, "guard_log") as log:
            self.assertEqual(gpu.stall_guard(95.0, 84.0,
                                             [(self.pid, 1.0, "maya")], 0, {}),
                             "stall:killed")
        self.assertEqual(send.call_args.args[1], gpu.signal.SIGKILL)
        self.assertEqual(set_stall.call_args.args[0], "")
        self.assertIn("SIGKILL", log.call_args.args[0])

    def test_stalled_engine_inside_the_grace_only_drains(self):
        gpu._set_stall_state(f"stall-termed:{self.pid}:4242:{time.time()}")
        with patch.object(gpu, "_starttime", return_value=4242), \
             patch.object(gpu, "_alive", return_value=True), \
             patch.object(gpu, "_signal_pid") as send:
            self.assertEqual(gpu.stall_guard(95.0, 84.0,
                                             [(self.pid, 1.0, "maya")], 0, {}),
                             "stall:draining")
        send.assert_not_called()

    def test_restarted_engine_clears_a_stale_stall_termination(self):
        gpu._set_stall_state(f"stall-termed:{self.pid}:9999:{time.time()}")
        with patch.object(gpu, "_starttime", return_value=4242), \
             patch.object(gpu, "_set_stall_state") as set_stall, \
             patch.object(gpu, "_signal_pid") as send:
            self.assertIsNone(gpu.stall_guard(95.0, 84.0,
                                              [(self.pid, 1.0, "maya")], 0, {}))
        self.assertEqual(set_stall.call_args.args[0], "")
        send.assert_not_called()

    def test_malformed_stall_state_is_discarded_without_signaling(self):
        gpu._set_stall_state("stall-termed:not-a-pid")
        with patch.object(gpu, "_set_stall_state") as set_stall, \
             patch.object(gpu, "_signal_pid") as send, \
             patch.object(gpu, "guard_log") as log:
            self.assertIsNone(gpu.stall_guard(95.0, 84.0,
                                              [(self.pid, 1.0, "maya")], 0, {}))
        self.assertEqual(set_stall.call_args.args[0], "")
        send.assert_not_called()
        self.assertIn("malformed stall state", log.call_args.args[0])

    def test_stall_guard_stays_out_while_the_memory_guard_kills(self):
        with patch.object(gpu, "_state", return_value=f"termed:{self.pid}:4242:0"), \
             patch.object(gpu, "_engine_log_file", return_value=self.log), \
             patch.object(gpu, "_signal_pid") as send, \
             self._stale(100000):
            self.assertIsNone(gpu.stall_guard(95.0, 84.0,
                                              [(self.pid, 1.0, "maya")], 0, {}))
        send.assert_not_called()

    def test_a_stall_kill_clears_a_stale_memory_guard_arm(self):
        _, _, _, set_state, _ = self._run(95.0, mem_state=f"armed:{time.time() - 5}")
        set_state.assert_called_once_with("")

    def test_a_stall_kill_leaves_an_in_flight_termination_alone(self):
        _, _, _, set_state, _ = self._run(
            95.0, mem_state=f"termed:{self.pid}:4242:0")
        set_state.assert_not_called()

    def test_rolling_tail_records_the_engines_last_lines_every_sample(self):
        with patch.object(gpu, "_engine_log_file", return_value=self.log):
            gpu.stall_guard(30.0, 70.0, [(self.pid, 1.0, "maya")], 15000, {})
        with open(gpu.STALL_TAIL_PATH) as f:
            body = f.read()
        self.assertIn(f"engine log tail ({self.log})", body)
        self.assertIn("[maya] thinking: 1 of max 32000 tokens", body)


if __name__ == "__main__":
    unittest.main()
