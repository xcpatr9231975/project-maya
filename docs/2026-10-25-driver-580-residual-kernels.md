# Driver 580.178.04 upgrade: boot default fixed, three stale kernels remain

Date: 2026-10-25 (upgrade + verification) · review due: week of 2026-10-26
Status: **boot default fixed and verified**; one residual left open on purpose
(the stale kernels) -- this note is the hand-off.
Affects: this DGX Spark (`carlosp-nvidia-dgx`), `/boot/grub/grubenv`,
`/etc/default/grub`, dpkg state. Companions: `~/rollback-gb10-580/`
(`ROLLBACK.txt`, `promote-new-kernel.sh`, `upgrade-20261025-185846.log`) and
`.opencode/skills/gb10-hardlock-triage` §8.

## What was upgraded

| | Before (boot -1) | After (boot 0) |
|---|---|---|
| Kernel | `6.14.0-1015-nvidia` | `7.0.0-1019-nvidia` (HWE) |
| Driver | 580.95.05 | **580.178.04** (open module) |
| CUDA | 12.x | 13.0 (V13.0.88), device `sm_121` |
| GSP firmware | `nvidia-firmware-580-580.95.05` | `nvidia-firmware-580-580.178.04` |

Boot 0 started 19:09:54 PDT. Boot -1 ended with a clean shutdown sequence
(`Journal stopped` after `Sending SIGTERM`), so the upgrade did not cost a
freeze. Engine smoke test on the new driver: pool 69.7 GB warmed in 8 s
(8.76 GB/s), decode 23.0 tok/s with 97 % draft acceptance, clean exit,
`N:0` (zero NVRM refusals) across all 142 samples of the boot.

## What is fixed

`promote-new-kernel.sh` ran at 19:24 and made the new kernel the permanent
default. Verified directly:

- `/boot/grub/grubenv`: `saved_entry=DGX OS GNU/Linux, with Linux 7.0.0-1019-nvidia`
- `/etc/default/grub`: `GRUB_DEFAULT=saved` + `GRUB_SAVEDEFAULT=false`
  (the saved entry cannot drift back at boot)
- `next_entry` empty -- the one-shot `grub-reboot` was consumed by this boot
- `modinfo -k 7.0.0-1019-nvidia -F version nvidia` -> `580.178.04`
- the entry title is real: `GRUB_DISTRIBUTOR="DGX OS"` from
  `/etc/default/grub.d/menu.cfg` yields exactly
  `DGX OS GNU/Linux, with Linux <release>`, and that is the title the
  `grub-reboot` one-shot used to boot this kernel.

Evidence caveat: `/boot/grub/grub.cfg` is root-only (`600`), so the generated
menu was not read directly; the title match above is inferred from
`GRUB_DISTRIBUTOR` plus the successful `grub-reboot` boot.

## The residual

Three kernels are still installed and still appear as GRUB entries, but their
nvidia modules were removed by the upgrade (packages now `rc` = removed,
config files only):

| Kernel | `linux-image-*` | `linux-modules-nvidia-580-open-*` | nvidia module | left in `/lib/modules/<k>` |
|---|---|---|---|---|
| `7.0.0-1019-nvidia` | `ii` | `ii` | **580.178.04** | the driver |
| `6.14.0-1015-nvidia` | `ii` | `rc` | none | in-tree only: `hid-nvidia-shield`, `i2c-nvidia-gpu`, `nvidiafb`, `nvidia_cspmu`, `typec_nvidia`, `nvidia-fs` 2.26.6 |
| `6.14.0-1013-nvidia` | `ii` | `rc` | none | same |
| `6.11.0-1014-nvidia` | `ii` | `rc` | none | same |

**Risk:** manually selecting one of the three old entries boots a kernel with
no `nvidia.ko`/`nvidia-uvm`/`nvidia-modeset`/`nvidia-drm` -- the box comes up
with no GPU driver, which looks like a hardware failure from the outside.

**Why it is not reachable by accident:** the GRUB menu is hidden.
`/etc/default/grub.d/no-grubmenu.cfg` sets `GRUB_TIMEOUT=0` and
`GRUB_TIMEOUT_STYLE=hidden`, and `grub-mkconfig` sources `/etc/default/grub.d/*.cfg`
in sorted order, so `no-grubmenu.cfg` (`n`) overrides `menu.cfg` (`m`, 5 s menu)
and `/etc/default/grub` (10 s). A plain reboot goes straight to the saved
default. (Same root-only caveat: inferred from the drop-in sort order.)

## The fix

Purge the stale kernels and their inert `nvidia-fs` modules, then let the
kernel hooks regenerate the menu. Needs `sudo`; **not run** -- this is the
open item:

```bash
sudo apt purge -y \
  linux-image-6.14.0-1013-nvidia linux-image-6.14.0-1015-nvidia \
  linux-image-6.11.0-1014-nvidia \
  linux-modules-nvidia-fs-6.14.0-1013-nvidia linux-modules-nvidia-fs-6.14.0-1015-nvidia
sudo update-grub
```

No reboot is required for the purge itself, but do it **before** the next
reboot so the menu never offers a dead entry. Do not touch
`linux-image-7.0.0-1019-nvidia` or `linux-image-nvidia-hwe-24.04` (the
meta-package that tracks the HWE kernel).

Optional hardening if the purge is postponed: set `GRUB_TIMEOUT=1` in
`/etc/default/grub.d/no-grubmenu.cfg` so there is a window to reach the
recovery entry at all.

## Rollback caveat (changed by this upgrade)

The upgrade log's plan A restores the exact pre-upgrade set with
`~/rollback-gb10-580/debs-exact/*.deb`, which assumed the 6.14 nvidia modules
were still installed. They are `rc` now, so that path no longer reproduces the
old stack. The working rollback is the one in `~/rollback-gb10-580/ROLLBACK.txt`:
`sudo dpkg --remove linux-modules-nvidia-580-open-7.0.0-1019-nvidia`, then
`sudo apt-get install -y --allow-downgrades ~/rollback-gb10-580/debs-driver/*.deb`
(17 debs) on the old kernel. Do **not** glob `debs-exact/` (79 OEM packages).

## Verify after the fix

```bash
dpkg -l | grep -E 'linux-(image|modules-nvidia)'      # only 7.0.0-1019 (+ hwe meta)
for k in $(ls /lib/modules); do printf '%s -> %s\n' "$k" "$(modinfo -k $k -F version nvidia 2>&1 | head -1)"; done
grub-editenv list                                     # saved_entry still 7.0.0-1019-nvidia
uname -r && nvidia-smi --query-gpu=driver_version --format=csv,noheader
```

## Cross-references

- `.opencode/skills/gb10-hardlock-triage` §8 -- why the upgrade was done, the
  rollback set, and the warning that the 6.14 modules would not survive it
- `docs/DGX_SPARK.md` -- platform bring-up and expert-pool sizing
- `docs/MEMORY_WATCHDOGS_DEEP_DIVE.md` -- the guard that watches device-free
