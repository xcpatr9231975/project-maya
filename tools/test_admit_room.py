"""tools/test_admit_room.py - the admission gate decides the same thing a test can check.

    python -m unittest tools.test_admit_room

The gate (admit_room in src/program/generate.cpp) is the one thing standing between a
request the device cannot hold and the two failures that must never happen on this box:
the memory guard's SIGKILL, and the driver's NV_ERR_NO_MEMORY, which deadlocks it.  It
has no C++ test target (generate.cpp is only built into the engine binary), so
--admit-check is its seam: the same knobs and the same arithmetic, with no model, no
tokens and no request, exiting 0 (admit) or 1 (refuse).

Hermetic: the engine binary is run once per case with the knobs in its environment, and
the only thing read from the machine is the device's free memory - which the cases either
ignore (an absurd floor refuses whatever the device says) or accept (a zero floor admits
whatever it says).  No model is loaded, so no GPU memory is taken.
"""
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAYA = ROOT / "build" / "maya"


def admit_check(env: dict | None = None, exe: Path = MAYA) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of `maya --admit-check` with these knobs."""
    if not exe.exists():
        raise unittest.SkipTest(f"the engine is not built: {exe} (run ./safe-build.sh)")
    e = dict(os.environ)
    e.pop("STRATA_GLM_ADMIT_FLOOR_MIB", None)
    e.pop("STRATA_GLM_ADMIT_BORROW_MIB", None)
    e.pop("STRATA_GLM_ADMIT", None)
    e.update(env or {})
    p = subprocess.run([str(exe), "--admit-check"], capture_output=True, text=True, env=e, timeout=120)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


class AdmitRoom(unittest.TestCase):
    def test_a_roomy_device_admits(self):
        """Nothing to keep free: whatever the device has is enough."""
        code, out, _ = admit_check({"STRATA_GLM_ADMIT_FLOOR_MIB": "0", "STRATA_GLM_ADMIT_BORROW_MIB": "0"})
        self.assertEqual(code, 0, out)
        self.assertIn("-> admit", out)

    def test_an_absurd_floor_refuses(self):
        """The floor the device can never meet: refuse, and say what was needed.  This is the case that must
        never silently admit - a request the device cannot hold is the one that gets the engine killed."""
        code, out, _ = admit_check({"STRATA_GLM_ADMIT_FLOOR_MIB": "1000000", "STRATA_GLM_ADMIT_BORROW_MIB": "0"})
        self.assertEqual(code, 1, out)
        self.assertIn("-> refuse", out)
        self.assertIn("out of memory", out)          # serve/server.py maps this to a 503, not a 400
        self.assertIn("1000000 MiB floor", out)      # the numbers are in the message, not just the verdict
    def test_the_borrow_counts_against_the_floor(self):
        """need = borrow + floor, so the same device admits with no borrow and refuses with a big one."""
        floor = {"STRATA_GLM_ADMIT_FLOOR_MIB": "0"}
        code_no, out_no, _ = admit_check({**floor, "STRATA_GLM_ADMIT_BORROW_MIB": "0"})
        code_big, out_big, _ = admit_check({**floor, "STRATA_GLM_ADMIT_BORROW_MIB": "1000000"})
        self.assertEqual(code_no, 0, out_no)
        self.assertEqual(code_big, 1, out_big)
        self.assertIn("borrow 1000000 MiB", out_big)

    def test_the_defaults_are_the_guards_floor(self):
        """With no knobs at all the gate keeps the memory guard's 3072 MiB free - the two must not drift apart,
        or the engine is killed for something the gate thought was fine."""
        code, out, _ = admit_check({})
        self.assertIn("floor 3072 MiB", out)
        self.assertIn(code, (0, 1))

    def test_the_check_off_admits_everything(self):
        """STRATA_GLM_ADMIT=0 is the no-rebuild rollback: nothing is refused, whatever the device says."""
        code, out, _ = admit_check({"STRATA_GLM_ADMIT": "0", "STRATA_GLM_ADMIT_FLOOR_MIB": "1000000"})
        self.assertEqual(code, 0, out)
        self.assertIn("admit", out)
        self.assertIn("off", out)

    def test_no_model_is_loaded(self):
        """The seam's whole point: it answers without taking the GPU memory a request would.  If this ever loads a
        pack it stops being hermetic and the test starts competing with the engine for the device."""
        code, out, err = admit_check({"STRATA_GLM_ADMIT_FLOOR_MIB": "1000000"})
        self.assertEqual(code, 1, out)
        self.assertNotIn("reading the model", out + err)
        self.assertNotIn("expert pool", out + err)


if __name__ == "__main__":
    unittest.main()
