"""Tests for tools/arm-gb10-crash-witness.sh.

Run with: python -m unittest tools.test_arm_gb10_crash_witness

The script arms the kernel's netconsole witness and (unless --no-apst) the
NVMe APST mitigation.  Everything it writes lives under /etc, so every test
here redirects the paths into a scratch directory and uses --dry-run or
--status: no file outside the temp dir is created, and nothing on the host
is touched.
"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "arm-gb10-crash-witness.sh"

# A stand-in for tools/nvme-stall-mitigations.sh: it records what it was
# asked to do instead of touching /etc/default/grub.d.
STUB_NVME_TOOL = """#!/usr/bin/env bash
set -e
printf 'nvme-tool %s\\n' "$*" >> "$STUB_NVME_LOG"
if [ "${1:-}" = "status" ]; then
    echo "stub status"
fi
"""


class ArmGb10CrashWitnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self.modprobe_conf = root / "etc" / "modprobe.d" / "netconsole.conf"
        self.modules_conf = root / "etc" / "modules-load.d" / "netconsole.conf"
        self.stub = root / "stub-nvme-tool.sh"
        self.stub.write_text(STUB_NVME_TOOL)
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IXUSR)
        self.stub_log = root / "nvme-tool.log"
        self.stub_log.write_text("")

        self.env = dict(os.environ)
        self.env.update(
            GB10_WITNESS_MODPROBE=str(self.modprobe_conf),
            GB10_WITNESS_MODULES=str(self.modules_conf),
            GB10_WITNESS_NVME_TOOL=str(self.stub),
            GB10_WITNESS_DEV="enP7s7",
            GB10_WITNESS_IP="192.168.105.178",
            GB10_WITNESS_BCAST="192.168.105.255",
            STUB_NVME_LOG=str(self.stub_log),
        )

    def run_script(self, *args, extra_env=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            text=True,
            capture_output=True,
        )

    def test_syntax(self):
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dry_run_writes_nothing(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.modprobe_conf.exists())
        self.assertFalse(self.modules_conf.exists())
        # the nvme tool is called, but only to print what it would do
        self.assertIn("apply apst --dry-run", self.stub_log.read_text())
        self.assertNotIn("apply apst\n", self.stub_log.read_text())

    def test_dry_run_prints_the_broadcast_witness(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "netconsole=6666@192.168.105.178/enP7s7,6666@192.168.105.255/ff:ff:ff:ff:ff:ff",
            result.stdout,
        )

    def test_dry_run_honours_an_explicit_receiver(self):
        result = self.run_script(
            "--dry-run",
            extra_env={
                "GB10_WITNESS_BCAST": "192.168.105.106",
                "GB10_WITNESS_MAC": "e8:cf:83:87:38:cf",
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "netconsole=6666@192.168.105.178/enP7s7,6666@192.168.105.106/e8:cf:83:87:38:cf",
            result.stdout,
        )

    def test_dry_run_calls_the_nvme_tool_for_apst(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("apply apst --dry-run", self.stub_log.read_text())

    def test_no_apst_leaves_the_nvme_tool_alone(self):
        result = self.run_script("--dry-run", "--no-apst")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.stub_log.read_text(), "")

    def test_a_malformed_receiver_mac_is_refused(self):
        result = self.run_script("--dry-run", extra_env={"GB10_WITNESS_MAC": "not-a-mac"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("not an ethernet address", result.stderr)
        self.assertFalse(self.modprobe_conf.exists())

    def test_status_is_read_only_and_reports_the_receiver(self):
        result = self.run_script("--status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("receiver mac     : ff:ff:ff:ff:ff:ff", result.stdout)
        self.assertIn("broadcast", result.stdout)
        self.assertIn("<not installed>", result.stdout)
        self.assertFalse(self.modprobe_conf.exists())
        self.assertFalse(self.modules_conf.exists())

    def test_status_flags_a_unicast_receiver(self):
        result = self.run_script(
            "--status", extra_env={"GB10_WITNESS_MAC": "e8:cf:83:87:38:cf"}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unicast", result.stdout)


if __name__ == "__main__":
    unittest.main()
