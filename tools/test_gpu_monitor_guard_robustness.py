"""Focused tests for fail-closed guard state and PID signaling.

    python -m unittest tools.test_gpu_monitor_guard_robustness
"""
import importlib.util
import os
import platform
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "gpu_monitor_guard_under_test", Path(__file__).with_name("gpu_monitor.py"))
gpu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gpu)


class GuardRobustnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        gpu.STATE_PATH = os.path.join(self.tmp, "state")
        gpu.LOCK_PATH = os.path.join(self.tmp, "lock")
        gpu._LOCK_FD = None

    def test_atomic_state_write_preserves_previous_value_on_replace_failure(self):
        gpu._set_state("armed:123.0")
        with patch.object(gpu.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                gpu._set_state("termed:1:2:123.0")
        self.assertEqual(gpu._state(), "armed:123.0")
        self.assertEqual(os.listdir(self.tmp), ["state"])

    def test_lock_contention_skips_but_lock_setup_failure_raises(self):
        self.assertTrue(gpu._try_lock())
        self.addCleanup(gpu._unlock)
        self.assertFalse(gpu._try_lock())

        with patch.object(gpu.os, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                gpu._try_lock()

    def test_main_reports_lock_setup_failure(self):
        with patch.object(gpu, "_try_lock", side_effect=PermissionError("denied")):
            with patch("builtins.print") as output:
                self.assertEqual(gpu.main(), 1)
        self.assertIn("cannot acquire", output.call_args.args[0])

    @unittest.skipUnless(
        (hasattr(os, "pidfd_open") or
         platform.machine().lower() in gpu._PIDFD_SYSCALLS) and
        (hasattr(signal, "pidfd_send_signal") or
         platform.machine().lower() in gpu._PIDFD_SYSCALLS),
        "pidfd signaling is unavailable")
    def test_pidfd_signal_requires_matching_process_starttime(self):
        starttime = gpu._starttime(os.getpid())
        self.assertIsNotNone(starttime)
        self.assertTrue(gpu._signal_pid(os.getpid(), 0, starttime))
        with patch.object(signal, "pidfd_send_signal") as send:
            self.assertFalse(gpu._signal_pid(os.getpid(), signal.SIGKILL, starttime + 1))
            send.assert_not_called()

    def test_stale_termed_state_is_cleared_without_signaling(self):
        gpu._set_state(f"termed:123:{456}:{gpu.time.time() - 60}")
        with patch.object(gpu, "_rss_map", return_value={}), \
             patch.object(gpu, "_starttime", return_value=789), \
             patch.object(gpu, "_signal_pid") as send, \
             patch.object(gpu, "guard_log"):
            result = gpu.memory_guard({}, None, [])
        self.assertEqual(result, "guard:state-reset")
        self.assertEqual(gpu._state(), "")
        send.assert_not_called()

    def test_sigterm_precedes_persisting_escalation_state(self):
        events = []
        armed_at = gpu.time.time() - 10

        def set_state(value):
            events.append("state")
            if value.startswith("termed:"):
                raise OSError("state storage unavailable")

        with patch.object(gpu, "_rss_map", return_value={}), \
             patch.object(gpu, "_state", return_value=f"armed:{armed_at}"), \
             patch.object(gpu, "_starttime", return_value=123), \
             patch.object(gpu, "_signal_pid", side_effect=lambda *args: events.append("signal") or True), \
             patch.object(gpu, "_set_state", side_effect=set_state), \
             patch.object(gpu, "guard_log"), \
             patch.object(gpu, "OS_VICTIM_BY_RSS", False):
            with self.assertRaises(OSError):
                gpu.memory_guard({}, 0, [(str(os.getpid()), 1.0, "test")])
        self.assertEqual(events, ["signal", "state"])

    def test_legacy_termed_state_is_discarded(self):
        gpu._set_state(f"termed:123:{gpu.time.time() - 60}")
        with patch.object(gpu, "_rss_map", return_value={}), \
             patch.object(gpu, "guard_log"):
            self.assertIsNone(gpu.memory_guard({}, None, []))
        self.assertEqual(gpu._state(), "")

    def test_float_environment_values_reject_nonfinite_or_negative_values(self):
        for value in ("nan", "inf", "-1"):
            with self.subTest(value=value), \
                 patch.dict(os.environ, {"TEST_GUARD_VALUE": value}):
                with self.assertRaises(ValueError):
                    gpu._env_float("TEST_GUARD_VALUE", "1")
        with patch.dict(os.environ, {"TEST_GUARD_VALUE": "0"}):
            with self.assertRaises(ValueError):
                gpu._env_float("TEST_GUARD_VALUE", "1", exclusive=True)


if __name__ == "__main__":
    unittest.main()
