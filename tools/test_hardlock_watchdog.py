import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "hardlock-watchdog.sh"

FAKE_SYSTEMCTL = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -e
    case "$1" in
      daemon-reexec)
        if [ -f "$FAKE_SYSTEMD_DROPIN" ]; then
          printf 'active\\n' > "$FAKE_STATE"
          printf '30s\\n' > "$FAKE_RUNTIME"
          printf '30s\\n' > "$FAKE_REBOOT"
        else
          printf 'inactive\\n' > "$FAKE_STATE"
          printf '0\\n' > "$FAKE_RUNTIME"
          printf '10min\\n' > "$FAKE_REBOOT"
        fi
        ;;
      show)
        prop=""
        while [ "$#" -gt 0 ]; do
          case "$1" in
            -p) prop="$2"; shift 2 ;;
            --value) shift ;;
            *) shift ;;
          esac
        done
        case "$prop" in
          RuntimeWatchdogUSec) cat "$FAKE_RUNTIME" ;;
          RebootWatchdogUSec) cat "$FAKE_REBOOT" ;;
        esac
        ;;
    esac
    """
)

FAKE_SYSCTL = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -e
    file=""
    kv=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -p) file="$2"; shift 2 ;;
        -w) kv="$2"; shift 2 ;;
        -q) shift ;;
        *) shift ;;
      esac
    done
    if [ -n "$file" ]; then
      sed -n 's/^kernel.panic *= *//p' "$file" | head -1 > "$FAKE_PANIC"
    elif [ -n "$kv" ]; then
      printf '%s\\n' "${kv#kernel.panic=}" > "$FAKE_PANIC"
    fi
    """
)


class HardlockWatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.sysfs = root / "sysfs"
        self.sysfs.mkdir()
        self.dev = root / "watchdog0"
        self.dev.write_text("")
        self.panic = root / "panic"
        self.panic.write_text("0\n")
        self.state = self.sysfs / "state"
        self.state.write_text("inactive\n")
        self.runtime = root / "runtime"
        self.runtime.write_text("0\n")
        self.reboot = root / "reboot"
        self.reboot.write_text("10min\n")
        self.systemd_dropin = root / "etc" / "system.conf.d" / "50-hardlock-watchdog.conf"
        self.sysctl_dropin = root / "etc" / "sysctl.d" / "60-hardlock-panic.conf"

        self._fake("systemctl", FAKE_SYSTEMCTL)
        self._fake("sysctl", FAKE_SYSCTL)

        (self.sysfs / "identity").write_text("SBSA Generic Watchdog\n")
        (self.sysfs / "timeout").write_text("10\n")
        (self.sysfs / "nowayout").write_text("0\n")
        (self.sysfs / "options").write_text("0x81a0\n")

        self.env = dict(os.environ)
        self.env.update(
            PATH=f"{self.bin}:{os.environ.get('PATH', '')}",
            HARDLOCK_WATCHDOG_DEV=str(self.dev),
            HARDLOCK_WATCHDOG_SYSFS=str(self.sysfs),
            HARDLOCK_PROC_PANIC=str(self.panic),
            HARDLOCK_SYSTEMD_DROPIN=str(self.systemd_dropin),
            HARDLOCK_SYSCTL_DROPIN=str(self.sysctl_dropin),
            FAKE_STATE=str(self.state),
            FAKE_RUNTIME=str(self.runtime),
            FAKE_REBOOT=str(self.reboot),
            FAKE_PANIC=str(self.panic),
            FAKE_SYSTEMD_DROPIN=str(self.systemd_dropin),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _fake(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

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

    def test_status_reports_unarmed(self):
        result = self.run_script("status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("SBSA Generic Watchdog", result.stdout)
        self.assertIn("NOT ARMED", result.stdout)

    def test_dry_run_writes_nothing(self):
        result = self.run_script("apply", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RuntimeWatchdogSec=30s", result.stdout)
        self.assertFalse(self.systemd_dropin.exists())
        self.assertFalse(self.sysctl_dropin.exists())

    def test_apply_arms_and_revert_disarms(self):
        if os.geteuid() == 0:
            env_extra = {}
        else:
            env_extra = {"HARDLOCK_ALLOW_NONROOT": "1"}

        result = self.run_script("apply", extra_env=env_extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RuntimeWatchdogSec=30s", self.systemd_dropin.read_text())
        self.assertIn("kernel.panic = 15", self.sysctl_dropin.read_text())
        self.assertEqual(self.state.read_text().strip(), "active")
        self.assertEqual(self.panic.read_text().strip(), "15")
        self.assertIn("ARMED", result.stdout)

        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stdout)

        result = self.run_script("revert", extra_env=env_extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.systemd_dropin.exists())
        self.assertFalse(self.sysctl_dropin.exists())
        self.assertEqual(self.state.read_text().strip(), "inactive")
        self.assertEqual(self.panic.read_text().strip(), "0")

    def test_apply_refuses_without_root(self):
        if os.geteuid() == 0:
            self.skipTest("running as root; the refusal path cannot be exercised")
        result = self.run_script("apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("must run as root", result.stderr)
        self.assertFalse(self.systemd_dropin.exists())


if __name__ == "__main__":
    unittest.main()
