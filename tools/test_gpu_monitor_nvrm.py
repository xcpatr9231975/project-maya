"""The NV_ERR_NO_MEMORY journal watch's cursor discipline: the epoch cursor in NVRM_PATH initialises on the
first run without counting (an old flood must not replay as a fresh breach), advances only after a good
journalctl read, and is held on timeout or failure so the next sample re-covers the same window; the
journalctl call is mocked throughout, no live cursor or journal is touched.

    python -m unittest tools.test_gpu_monitor_nvrm
"""
import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "gpu_monitor_under_test", Path(__file__).with_name("gpu_monitor.py"))
gpu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gpu)

LINES = "NVRM: ... NV_ERR_NO_MEMORY ...\n" * 3 + "unrelated\n"


class NvrmCursorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gpu.NVRM_PATH = os.path.join(tmp.name, "nvrm_cursor")

    def cursor(self):
        with open(gpu.NVRM_PATH) as f:
            return float(f.read())

    def test_first_run_initialises_without_counting(self):
        with patch("subprocess.run") as run:
            self.assertEqual(gpu.nvrm_errors(), 0)
            run.assert_not_called()
        self.assertLessEqual(self.cursor(), time.time())

    def test_success_counts_and_advances(self):
        with open(gpu.NVRM_PATH, "w") as f:
            t0 = time.time()
            f.write(f"{t0}")
        time.sleep(0.05)
        with patch("subprocess.run",
                   return_value=SimpleNamespace(returncode=0, stdout=LINES)) as run:
            self.assertEqual(gpu.nvrm_errors(), 3)
            run.assert_called_once()
            self.assertIn(f"--since=@{t0}", " ".join(run.call_args[0][0]))
        self.assertGreater(self.cursor(), t0)

    def test_timeout_holds_cursor_and_recovered_window(self):
        with open(gpu.NVRM_PATH, "w") as f:
            t0 = time.time()
            f.write(f"{t0}")
        time.sleep(0.05)
        boom = subprocess.TimeoutExpired(cmd="journalctl", timeout=10)
        with patch("subprocess.run", side_effect=boom):
            self.assertIsNone(gpu.nvrm_errors())
        self.assertEqual(self.cursor(), t0)
        # the next sample after the failure re-covers from the held cursor
        time.sleep(0.05)
        with patch("subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="NVRM: NV_ERR_NO_MEMORY\n")) as run:
            self.assertEqual(gpu.nvrm_errors(), 1)
            since = [a for a in run.call_args[0][0] if a.startswith("--since=")][0]
            self.assertEqual(since, f"--since=@{t0}")

    def test_failed_journalctl_holds_cursor(self):
        with open(gpu.NVRM_PATH, "w") as f:
            t0 = time.time()
            f.write(f"{t0}")
        with patch("subprocess.run",
                   return_value=SimpleNamespace(returncode=1, stdout="")):
            self.assertIsNone(gpu.nvrm_errors())
        self.assertEqual(self.cursor(), t0)

    def test_future_cursor_skew_counts_nothing(self):
        with open(gpu.NVRM_PATH, "w") as f:
            f.write(f"{time.time() + 300}")
        with patch("subprocess.run") as run:
            self.assertEqual(gpu.nvrm_errors(), 0)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
