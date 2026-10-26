"""tools/test_needle_bench_cap.py - needle_bench skips a length the device cannot hold.

    python -m unittest tools.test_needle_bench_cap

The bench used to cap its lengths against the server's context only, and then ran into the
memory: the gate refuses a request the device cannot hold (HTTP 503, after
STRATA_GLM_ADMIT_STALL_S), so a long length burned its wait and failed anyway.  Now the
same arithmetic the gate uses (the conversation's state ~15 B/token, the prompt path's
borrow, the 3072 MiB floor the memory guard keeps) is checked before the request, from
cudaMemGetInfo - the number NVML cannot report on GB10.

Hermetic: no server, no device, no model.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.needle_bench import BORROW_MIB, FLOOR_MIB, length_fits  # noqa: E402


class LengthFits(unittest.TestCase):
    def test_need_is_state_plus_borrow_plus_floor(self):
        fits, need = length_fits(27919, 10**9)          # a huge free: only the arithmetic matters
        self.assertTrue(fits)
        self.assertEqual(need, int(27919 * 15 / 1024) + BORROW_MIB + FLOOR_MIB)

    def test_a_small_length_fits_a_tight_device(self):
        # the morning's floor-scraper: 4391 MiB free - a 32k length needs ~8.0 GB, so it does not fit
        fits, need = length_fits(32768, 4391)
        self.assertFalse(fits)
        self.assertGreater(need, 4391)

    def test_at_the_floor_scraper_nothing_fits(self):
        # the morning's state: 4391 MiB free.  The gate's demand is FIXED (the borrow + the floor = 7489 MiB),
        # independent of the prompt's length - so no length fits, and the bench says so instead of burning a
        # 30 s wait per request on refusals.  (The engine was useless until a restart; that is what this shows.)
        for tokens in (2048, 32768, 239544):
            fits, need = length_fits(tokens, 4391)
            self.assertFalse(fits, f"{tokens} tokens")
            self.assertGreater(need, 4391)

    def test_a_tiny_length_fits_when_the_gate_demand_does(self):
        fits, need = length_fits(2048, 8000)
        self.assertTrue(fits)
        self.assertLess(need, 8000)

    def test_the_full_context_length_needs_the_most(self):
        _, small = length_fits(32768, 10**9)
        _, big = length_fits(239544, 10**9)
        self.assertGreater(big, small)

    def test_an_unreadable_device_admits(self):
        """`None` (no CUDA runtime readable): the bench runs and lets the gate be the judge."""
        fits, _ = length_fits(239544, None)
        self.assertTrue(fits)

    def test_the_constants_are_the_ones_the_box_measured(self):
        self.assertEqual(FLOOR_MIB, 3072)               # the memory guard's floor
        self.assertEqual(BORROW_MIB, 4417)              # the 262144 config's printed borrow (--prefill 16384)


if __name__ == "__main__":
    unittest.main()