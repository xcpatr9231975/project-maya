"""The RSS-growth baseline's process identity: entries are pid:starttime:rss (stat field 22), so a pid that
died and was reused inside the window counts as a fresh process with its full RSS as growth, a pid with no
/proc stat any more died between sample and pick and is skipped, and the legacy pid:rss format still parses
as a same-process delta; uses spawned throwaway children, no GPU and no live guard state.

    python -m unittest tools.test_gpu_monitor_baseline
"""
import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "gpu_monitor_under_test", Path(__file__).with_name("gpu_monitor.py"))
gpu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gpu)

ME = 999999           # fake "self" pid, excluded from every pick


class StarttimeBaselineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gpu.RSS_BASE_PATH = os.path.join(tmp.name, "rss_base")
        gpu.STATE_PATH = os.path.join(tmp.name, "state")
        gpu.RSS_GROW_MIN_KIB = 100

    def write_base(self, cur, ts=None):
        with open(gpu.RSS_BASE_PATH, "w") as f:
            f.write(f"{ts if ts is not None else time.time()} "
                    + " ".join(f"{p}:{s}:{r}" for p, (s, r) in cur.items()) + "\n")

    def child(self):
        c = subprocess.Popen(["sleep", "60"])
        self.addCleanup(c.wait)      # LIFO: kill runs first, wait reaps it
        self.addCleanup(c.kill)
        return c

    def test_same_starttime_delta(self):
        c = self.child()
        self.write_base({c.pid: (777, 500_000)})
        with patch.object(gpu, "_starttime", lambda p: 777):
            v = gpu._rss_grow_victim({c.pid: 700_000, ME: 1_000_000}, ME)
        self.assertIsNotNone(v)
        self.assertEqual(v[0], c.pid)
        self.assertEqual(v[2], 200_000)

    def test_reused_pid_full_rss(self):
        c = self.child()
        self.write_base({c.pid: (777, 500_000)})
        with patch.object(gpu, "_starttime", lambda p: 999):
            v = gpu._rss_grow_victim({c.pid: 700_000, ME: 1_000_000}, ME)
        self.assertIsNotNone(v)
        self.assertEqual(v[0], c.pid)
        self.assertEqual(v[2], 700_000)

    def test_dead_pid_skipped(self):
        c = self.child()
        self.write_base({c.pid: (777, 500_000)})
        with patch.object(gpu, "_starttime", lambda p: None):
            v = gpu._rss_grow_victim({c.pid: 700_000, ME: 1_000_000}, ME)
        self.assertIsNone(v)

    def test_unlisted_pid_full_rss(self):
        c = self.child()
        self.write_base({ME: (777, 500_000)})
        with patch.object(gpu, "_starttime", lambda p: 555):
            v = gpu._rss_grow_victim({c.pid: 300_000, ME: 1_000_000}, ME)
        self.assertIsNotNone(v)
        self.assertEqual(v[0], c.pid)
        self.assertEqual(v[2], 300_000)

    def test_legacy_two_token_baseline(self):
        c = self.child()
        with open(gpu.RSS_BASE_PATH, "w") as f:
            f.write(f"{time.time()} {c.pid}:500000\n")
        with patch.object(gpu, "_starttime", lambda p: 777):
            v = gpu._rss_grow_victim({c.pid: 700_000, ME: 1_000_000}, ME)
        self.assertIsNotNone(v)
        self.assertEqual(v[0], c.pid)
        self.assertEqual(v[2], 200_000)

    def test_below_min_growth_no_victim(self):
        c = self.child()
        gpu.RSS_GROW_MIN_KIB = 262_144
        self.write_base({c.pid: (777, 500_000)})
        with patch.object(gpu, "_starttime", lambda p: 777):
            v = gpu._rss_grow_victim({c.pid: 500_100, ME: 1_000_000}, ME)
        self.assertIsNone(v)

    def test_round_trip_carries_starttime(self):
        c = self.child()
        c2 = self.child()
        real_st = gpu._starttime(c.pid)
        self.assertIsInstance(real_st, int)
        gpu._write_rss_base({c.pid: 123_000, c2.pid: 45_000})
        ts, base = gpu._load_rss_base()
        self.assertEqual(base[c.pid], (123_000, real_st))
        self.assertEqual(base[c2.pid], (45_000, gpu._starttime(c2.pid)))
        with open(gpu.RSS_BASE_PATH) as f:
            toks = f.read().split()[1:]
        self.assertTrue(all(len(t.split(":")) == 3 for t in toks))

    def test_fresh_pid_after_another_died(self):
        c1 = subprocess.Popen(["sleep", "60"])
        gpu._write_rss_base({c1.pid: 100_000})
        c1.kill()
        c1.wait()
        c2 = self.child()
        v = gpu._rss_grow_victim({c2.pid: 400_000, ME: 1_000_000}, ME)
        self.assertIsNotNone(v)
        self.assertEqual(v[0], c2.pid)
        self.assertEqual(v[2], 400_000)

    def test_self_and_kernels_never_picked(self):
        self.write_base({})
        with patch.object(gpu, "_starttime", lambda p: 1):
            v = gpu._rss_grow_victim({ME: 5_000_000, 1: 5_000_000, 2: 5_000_000}, ME)
        self.assertIsNone(v)


if __name__ == "__main__":
    unittest.main()
