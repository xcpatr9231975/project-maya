"""Tests for tools/nvme-stall-mitigations.sh.

Run with: python -m unittest tools.test_nvme_stall_mitigations
"""

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "nvme-stall-mitigations.sh"

FAKE_NVME = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -e
    printf 'nvme %s\\n' "$*" >> "$FAKE_NVME_LOG"
    case "$1" in
      get-feature)
        printf 'get-feature:0x08 (Interrupt Coalescing), value: %s\\n' \\
          "$(cat "$FAKE_COALESCE_STATE")"
        ;;
      set-feature)
        val=""
        while [ "$#" -gt 0 ]; do
          case "$1" in
            -v) val="$2"; shift 2 ;;
            *) shift ;;
          esac
        done
        printf '%s\\n' "$val" > "$FAKE_COALESCE_STATE"
        ;;
    esac
    """
)

FAKE_SYSTEMCTL = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -e
    printf 'systemctl %s\\n' "$*" >> "$FAKE_SVC_LOG"
    case "$1" in
      is-enabled) cat "$FAKE_SVC_STATE" 2>/dev/null || echo enabled ;;
      is-active) echo active ;;
      mask) echo masked > "$FAKE_SVC_STATE" ;;
      unmask) echo enabled > "$FAKE_SVC_STATE" ;;
      enable) echo enabled > "$FAKE_SVC_STATE" ;;
      disable) echo disabled > "$FAKE_SVC_STATE" ;;
    esac
    """
)

# Simulates update-grub: renders the drop-in's tokens into a linux line.
FAKE_UPDATE_GRUB = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -e
    toks=""
    if [ -f "$FAKE_GRUB_DROPIN" ]; then
      toks="$(sed -n 's/^GRUB_CMDLINE_LINUX_DEFAULT="\\$GRUB_CMDLINE_LINUX_DEFAULT *\\(.*\\)"$/\\1/p' \\
        "$FAKE_GRUB_DROPIN")"
    fi
    mkdir -p "$(dirname "$FAKE_GRUB_CFG")"
    printf 'linux /boot/vmlinuz quiet splash nomodeset %s\\n' "$toks" > "$FAKE_GRUB_CFG"
    """
)


class NvmeStallMitigationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.dropin = root / "etc" / "default" / "grub.d" / "99-nvme-stall-mitigations.cfg"
        self.grub_cfg = root / "boot" / "grub" / "grub.cfg"
        self.nvme_log = root / "nvme.log"
        self.nvme_log.write_text("")
        self.svc_log = root / "svc.log"
        self.svc_log.write_text("")
        self.coalesce_state = root / "coalesce"
        self.coalesce_state.write_text("0x00000107\n")
        self.svc_state = root / "svc.state"
        self.svc_state.write_text("enabled\n")
        # status must read the APST cap and the kernel cmdline from files we
        # control: on the DGX itself APST is already off, so a test that reads
        # the live host asserts the opposite of reality (and CI, which has no
        # nvme_core at all, would pass by accident).
        self.apst_param = root / "default_ps_max_latency_us"
        self.apst_param.write_text("100000\n")
        self.cmdline = root / "cmdline"
        self.cmdline.write_text("BOOT_IMAGE=/boot/vmlinuz-7.0.0-1019-nvidia root=UUID=abc ro\n")

        self._fake("nvme", FAKE_NVME)
        self._fake("systemctl", FAKE_SYSTEMCTL)
        self._fake("update-grub", FAKE_UPDATE_GRUB)

        self.env = dict(os.environ)
        self.env.update(
            PATH=f"{self.bin}:{os.environ.get('PATH', '')}",
            NVME_DEV=str(root / "fake-nvme0"),
            NVME_GRUB_DROPIN=str(self.dropin),
            NVME_GRUB_CFG=str(self.grub_cfg),
            NVME_COALESCE_SVC="fake-coalesce.service",
            NVME_APST_PARAM=str(self.apst_param),
            NVME_CMDLINE=str(self.cmdline),
            FAKE_GRUB_DROPIN=str(self.dropin),
            FAKE_GRUB_CFG=str(self.grub_cfg),
            FAKE_NVME_LOG=str(self.nvme_log),
            FAKE_SVC_LOG=str(self.svc_log),
            FAKE_COALESCE_STATE=str(self.coalesce_state),
            FAKE_SVC_STATE=str(self.svc_state),
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

    def _root_env(self):
        return {} if os.geteuid() == 0 else {"NVME_ALLOW_NONROOT": "1"}

    def test_syntax(self):
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_status_clean_state(self):
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("APST enabled", result.stdout)
        self.assertIn("100000 us", result.stdout)
        self.assertIn("0x00000107", result.stdout)
        self.assertIn("<not installed>", result.stdout)
        self.assertIn("cmdline nvme    : <none>", result.stdout)

    def test_status_reports_apst_disabled_from_the_redirected_path(self):
        self.apst_param.write_text("0\n")
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("APST disabled", result.stdout)
        self.assertIn("0 us", result.stdout)

    def test_status_shows_the_cmdline_nvme_tokens(self):
        self.cmdline.write_text(
            "BOOT_IMAGE=/boot/vmlinuz ro nvme_core.default_ps_max_latency_us=0"
            " nvme_core.io_timeout=10\n"
        )
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nvme_core.default_ps_max_latency_us=0", result.stdout)
        self.assertIn("nvme_core.io_timeout=10", result.stdout)

    def test_status_reports_nothing_pending_when_the_cmdline_already_has_it(self):
        self.cmdline.write_text(
            "BOOT_IMAGE=/boot/vmlinuz ro nvme_core.default_ps_max_latency_us=0\n"
        )
        self.run_script("apply", "apst", extra_env=self._root_env())
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pending reboot  : none", result.stdout)

    def test_dry_run_writes_nothing(self):
        for target in ("apst", "io-timeout", "coalescing"):
            result = self.run_script("apply", target, "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.coalesce_state.read_text().strip(), "0x00000107")
        self.assertNotIn("systemctl mask", self.svc_log.read_text())

    def test_apply_apst_adds_token_and_updates_grub(self):
        result = self.run_script("apply", "apst", extra_env=self._root_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = self.dropin.read_text()
        self.assertIn("nvme_core.default_ps_max_latency_us=0", text)
        self.assertIn("$GRUB_CMDLINE_LINUX_DEFAULT", text)
        self.assertIn("nvme_core.default_ps_max_latency_us=0", self.grub_cfg.read_text())

    def test_apply_io_timeout_respects_seconds(self):
        env = dict(self._root_env(), NVME_IO_SEC="5")
        result = self.run_script("apply", "io-timeout", extra_env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nvme_core.io_timeout=5", self.dropin.read_text())

    def test_apply_is_idempotent(self):
        first = self.run_script("apply", "apst", extra_env=self._root_env())
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self.run_script("apply", "apst", extra_env=self._root_env())
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.dropin.read_text().count("default_ps_max_latency_us"), 1)

    def test_revert_keeps_other_tokens(self):
        self.run_script("apply", "apst", extra_env=self._root_env())
        self.run_script("apply", "io-timeout", extra_env=self._root_env())
        result = self.run_script("revert", "apst", extra_env=self._root_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = self.dropin.read_text()
        self.assertNotIn("default_ps_max_latency_us", text)
        self.assertIn("nvme_core.io_timeout=10", text)

    def test_revert_last_token_removes_dropin(self):
        self.run_script("apply", "apst", extra_env=self._root_env())
        result = self.run_script("revert", "apst", extra_env=self._root_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.dropin.exists())

    def test_apply_coalescing_is_live_and_masks_service(self):
        result = self.run_script("apply", "coalescing", extra_env=self._root_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("set-feature", self.nvme_log.read_text())
        self.assertIn("-v 0", self.nvme_log.read_text())
        self.assertEqual(self.coalesce_state.read_text().strip(), "0")
        self.assertIn("mask", self.svc_log.read_text())

    def test_revert_coalescing_restores_nvidia_value(self):
        self.run_script("apply", "coalescing", extra_env=self._root_env())
        result = self.run_script("revert", "coalescing", extra_env=self._root_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.coalesce_state.read_text().strip(), "0x107")

    def test_status_reports_pending_reboot(self):
        self.run_script("apply", "apst", extra_env=self._root_env())
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pending reboot", result.stdout)

    def test_apply_refuses_without_root(self):
        if os.geteuid() == 0:
            self.skipTest("running as root; the refusal path cannot be exercised")
        result = self.run_script("apply", "apst")
        self.assertEqual(result.returncode, 1)
        self.assertIn("must run as root", result.stderr)
        self.assertFalse(self.dropin.exists())


if __name__ == "__main__":
    unittest.main()
