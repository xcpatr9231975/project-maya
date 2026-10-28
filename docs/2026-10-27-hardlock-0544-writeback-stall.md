# 2026-10-27 05:44 hard lock — first on the upgraded driver; the writeback stall caught in the act

Date: 2026-10-27 (post-mortem same day) · review due: week of 2026-11-02
Status: **unattributed** — same hard-lock class as 2026-10-24/25/26, but this
death adds the strongest storage-side evidence yet: a report the monitor
provably *closed* was still 0 bytes on disk 12 s later, while journald's
fsync'd writes kept landing. The writeback stall from
`.opencode/skills/gb10-hardlock-triage` §5b was caught mid-flight.
Experiments: **E0 armed and E1 live since 17:29 on 10-27**; E2 and E3 pending.
Affects: this DGX Spark (`carlosp-nvidia-dgx`), engine pid 119402.
Companion: `.opencode/skills/gb10-hardlock-triage` (triage procedure) and
`dgx-spark-memory-forensics` (the other failure class).

## The freeze

| | |
|---|---|
| Engine start | 19:56:23 on 10-26 (pid 119402, first monitor sample) |
| Last fully-persisted sample | 05:44:14 (report flushed, 743 B on disk) |
| Last code-complete sample | **05:44:25** — report closed, content never reached NVMe |
| Last journald entry | 05:44:37 (hermes-dashboard "Started") |
| Next boot | 16:07:25 (**10 h 23 m later** = manual power-cycle) |
| Survival | **~9 h 48 m — the longest of any run** |
| Config | headroom 40 GB (escalated), pool budget 55.03–55.10 GB |
| Device footprint at death | `M: 66237 MiB` (64.7 GB), `F: 16.61 GiB`, `N: 0` |

## Hard-lock signature — every tell present

| Evidence | Observed |
|---|---|
| `~/gpu_monitor_guard.log` | no entry naming the engine; last entry 10-26 11:49:44 — **not a guard kill** |
| Last samples | `F:` flat 16.5–16.6 GiB, `N:0`, 95% util, 81 °C, 45 W — healthy by every guard metric |
| 0-byte reports | `gpu_report_20261027_054425.txt` and `_054436.txt` — a small cluster of 2 |
| Engine log | only **54 trailing NUL bytes** in the final 4 KB — nearly fully flushed; ends on clean slot/session save lines |
| `journalctl -b -1` | stops mid-stream at 05:44:37, no shutdown sequence |
| Kernel log, boot -1 | clean: the only `NVRM` line is the module load; no Xid, oops, SError, hung task, MCE |
| `/var/crash/` | no new vmcore (only the 10-26 induced-kdump-test artifacts) |

## New evidence: the writeback stall, caught in the act

`tools/gpu_monitor.py:_main_locked()` order is: append+close the sample line
(1285-1286) → open the report (1291) → write the report and close it inside the
`with` block (1291-1314) → `print(f"report: {rp}")` **after** that close (1315).

For the 05:44:25 sample, `report: /home/cpatriaw/gpu_reports/
gpu_report_20261027_054425.txt` **reached journald at 05:44:25.778** — so the
code provably ran past the report's close and the 743 B was sitting in page
cache. The kernel's periodic writeback (5 s tick) then had ~12 s to flush it
before the box died. It never did: the file is **0 bytes on disk**. Meanwhile
journald's own entries persisted (fsync'd) through 05:44:37.

This is the first death where journald *outlived* the monitor's last write,
which separates two things previous deaths could not:

- **page-cache writeback for `/home` stalled** (743 B, 12 s, never flushed);
- **the fsync path still worked** (journald's journal kept landing).

The 05:44:36 instance sharpens it. systemd logged `Started` and `Deactivated
successfully` for it in the same second (rc=0), journald captured **zero
stdout** from it, the monitor log has no 05:44:36 line, and its report file is
0 bytes. Per the code order, creating that report file means the instance
passed the 1287 sample print — so its sample-line append, its report content,
*and* its stdout (flushed at exit) were all lost. Only systemd's own
force-synced unit-state messages survived. In the final second even journald's
stdout-ingest path lost data.

Layered order this time (compare 15:21, where journald died ~30 s *before* the
monitor): monitor's persistence first (05:44:25 content), then journald's
stdout ingest (05:44:36), then journald/systemd (05:44:37). The freeze instant
is bounded to 05:44:25–47.

## The engine's last state

The run survived 9 h 48 m at headroom 40 with `M:` peaking at 66.2 GB and `F:`
flat at ~16.6 GiB — healthy by every metric the guard watches. The last
flushed engine lines:

```
glm slots: saved a 2163-token conversation (0.18 GB in 0.1 s); 1 kept, 0.18 GB
[maya] sessions: saved a 2163-token conversation (0.18 GB in 0.1 s); 1 kept, 0.18 GB
```

preceded by the admission of a new request:

```
[maya] req openai: msgs=9 tools=80 max_tokens_raw=32000/None max_new=32000
       thinking=True stream=True prompt_tokens=31011
       last='user':'Continue if you have next steps, or stop and ask …'
```

Two things to note:

1. A **31,011-token prompt was admitted** right before death. That is now
   n=3 for the "freeze followed admission of a large prompt" pattern
   (50,511 on 10-26 15:21 → 31,011 here). A 31k-token read is ~2 prefill
   chunks ≈ 26–32 GB of streaming reads — exactly the disk pressure §5b
   names as the stressor.
2. The slot save moved **0.18 GB in 0.1 s** — a burst of dirty page cache
   seconds before the writeback path visibly stalled (the 0-byte report).

## What this rules out or updates

- **Driver/firmware (§8 lead 2) is now exhausted.** The dead boot ran the
  fully upgraded stack — HWE kernel `7.0.0-1019-nvidia` and driver
  **580.178.04** (loaded 10-26 15:59:24). This is the first freeze on the new
  GSP firmware and it did not change the failure.
- **`kernel.hung_task_panic=1` was active from boot** (sysctl.d, 120 s
  timeout) and did not fire; the soft-lockup detector did not fire either; no
  panic, no vmcore. The end state is a total freeze with no >120 s D-state
  window — software detectors on the box cannot see it. The **USB-UART console
  logger is now the only definitive channel left**.
- **The expert-pool/headroom lever (§5/§6) stays dead.** The longest survival
  ever (9 h 48 m) came at the escalated headroom 40 and the second-smallest
  footprint of any death — and it froze anyway.
- **SBSA watchdog failed a third time.** Freeze 05:44:37 → next boot 16:07:25
  (10 h 23 m = a human power-cycle). Confirmed armed: `active / 30 / SBSA
  Generic Watchdog`, `RuntimeWatchdogSec=30s`. Side note: the driver
  initializes at 10 s (ACPI GTDT) and systemd re-arms it to 30 s.
- **`sd 0:0:0:0` and the DOE message are noise.** The 16:15 `Power-on or
  device reset` is the KVM USB-emulation device re-enumerating, not storage.
  `DOE: failed to reset mailbox` appears in boots -2 and -1 alike — chronic,
  benign.
- **smartd** has logged nothing adverse for `nvme0` (SAMSUNG MZALC4T0HBL1,
  4.09 TB) in syslog or syslog.1.
- **NVMe SMART is clean (checked 16:39, post-mortem).** `media_errors: 0`,
  `num_err_log_entries: 0` (the persistent Samsung controller error log is
  empty — no error-log dump needed), `critical_warning: 0`,
  `available_spare: 100%`, `percentage_used: 0%`, 48–50 °C, no thermal events.
  `unsafe_shutdowns: 84/224` is just our manual power-cycles. The writeback
  stall is therefore **not media damage** — it lives in the kernel
  NVMe/ext4 write path, a controller/firmware wedge that logs nothing
  (FW NXHB202Q), or a SoC/fabric-level stall.

## Ranked experiments

The analysis above is inference. These four experiments are ordered so the
first one can turn it into fact, and the rest are cheap, reversible fixes for
the two plausible triggers. Run them **one at a time** — stacking loses
attribution.

**State as of 2026-10-27 18:00** (after the post-mortem, on the live box):

| # | Experiment | Status | Persistence | Cost | Question it answers |
|---|---|---|---|---|---|
| E0 | **netconsole witness** | **armed** 17:29:58, re-armed by the boot at 17:32:03 | `/etc/modprobe.d` + `modules-load.d` | none | Did an NVMe timeout / abort / reset precede the freeze? (the single missing fact) |
| E1 | **disable APST** (`nvme_core.default_ps_max_latency_us=0`) | **applied and live** — installed 17:29, in `/proc/cmdline` since the 17:31 reboot | GRUB drop-in | tiny idle power | If freezes stop: power-state transitions were the trigger |
| E2 | **disable interrupt coalescing** (`0x08=0`) + mask the NVIDIA service | **pending** — the service is still `enabled`/`active`, so `0x107` is still in force | live + `systemctl mask` | more IRQs / CPU | If freezes stop: NVIDIA's `0x107` coalescing loses completions under sustained load |
| E3 | **shorten `nvme_core.io_timeout=10`** | pending | GRUB drop-in | none | Forces the abort/reset path to run while the box can still log it — and a completed reset may save the box instead of hanging |

E0 and E1 are in place; **E1 is the only variable changed so far**, so the next
freeze is still attributable.

**E0 — netconsole.** The NIC is independent of the stalled disk, so the next
freeze will stream the driver's own words (`I/O … timeout, aborting`, `reset
controller`, `queue-freeze`) to the LAN *before* the box goes dark. That is
the one thing no artifact on the box could tell us. Set it up with the block in
"Next actions"; capture with
`socat -u UDP-RECVFROM:6666,fork - >> gb10-console.log` (add timestamps with
`ts` if available).

Two caveats that decide whether the witness is worth anything:

- **The receiver must be a different host.** The console is aimed at the
  subnet broadcast (`192.168.105.255`), and a host does not receive the
  broadcast frames it sends itself — socat on this box would stay empty
  forever while the console was working fine.
- **A switch may suppress broadcast frames.** The upstream kernel docs warn
  that the default target ethernet address is the broadcast address and that
  "some LAN switches may be configured to suppress ethernet broadcasts so it
  is advised to explicitly specify the remote agents' MAC addresses". If the
  receiver's address is known, aim the console at it directly:
  `GB10_WITNESS_BCAST=<receiver-ip> GB10_WITNESS_MAC=<receiver-mac> sudo
  tools/arm-gb10-crash-witness.sh --no-apst`.

Note that the witness is silent unless the kernel prints something: on an
idle box the log stays empty, and only a freeze's own last messages arrive.
`sudo tools/arm-gb10-crash-witness.sh` writes a test line to `/dev/kmsg` when
it arms, which is the cheapest proof that the path works end to end.

**E1 — APST, highest prior.** `default_ps_max_latency_us` was **100000** µs
until 17:29 today, so the kernel allowed power states with up to 100 ms exit
latency. "Samsung OEM NVMe hangs under sustained load, error log stays empty,
comes back perfectly after a power-cycle" is the classic APST/power-state
failure class, and the engine's bursty pattern (13–16 GB prefill chunks, then
read bursts) creates exactly the idle windows that race a transition against
an outstanding command. `=0` disables APST outright, and **that is now the
live kernel command line** (`/proc/cmdline` carries
`nvme_core.default_ps_max_latency_us=0`; the sysfs parameter reads 0). It
took effect on the 17:31 reboot, so E1 needs no further action — only the
kill criterion below.
Kill criterion: 2–3 overnight engine sessions (>2 h each) with no freeze —
the observed survival spread is 17 min … 9 h 48 m.

**E2 — interrupt coalescing.** `nvidia-nvme-interrupt-coalescing.service` sets
`0x107` on this Samsung (vendor `0x144d`) at every boot, and it is still
`enabled`/`active` — E2 has not been applied yet. A coalesced/lost
completion is the cleanest explanation of the measured asymmetry: one request
never completes (the 743 B report) while other tags keep finishing (journald's
fsync). Disable live with `nvme set-feature /dev/nvme0 -f 0x08 -v 0` and mask
the service so boot does not restore it.

**E3 — io timeout, diagnostic.** `nvme_core.io_timeout` defaults to 30 s; the
box died ~11 s after the first stalled write, so the timeout→abort→reset
sequence never ran. Shortening it to 10 s puts that sequence inside the
pre-freeze window: with E0 armed we will see it, and if the reset completes
the box may survive instead of hard-locking. Not applied yet.

All three persistent changes are managed by `tools/nvme-stall-mitigations.sh`
(GRUB drop-in `/etc/default/grub.d/99-nvme-stall-mitigations.cfg`, which
appends to `GRUB_CMDLINE_LINUX_DEFAULT` the same way the existing DGX/NVIDIA
drop-ins do, then re-runs `update-grub`); the witness (E0) is armed by
`tools/arm-gb10-crash-witness.sh`, which applies E1 as well:

```bash
tools/nvme-stall-mitigations.sh status          # what is installed / active
sudo tools/nvme-stall-mitigations.sh apply apst             # E1 (next boot)
sudo tools/nvme-stall-mitigations.sh apply coalescing       # E2 (live)
sudo tools/nvme-stall-mitigations.sh apply io-timeout       # E3 (next boot)
sudo tools/nvme-stall-mitigations.sh revert apst            # undo (any of them)
# add --dry-run to print the files/commands without changing anything

sudo tools/arm-gb10-crash-witness.sh            # E0 + E1 (netconsole, then APST)
sudo tools/arm-gb10-crash-witness.sh --status   # read-only
```

## Next actions

1. ~~**NVMe persistent logs (needs sudo)**~~ — done: SMART clean, persistent
   error log empty (see above). Media is not the problem.
2. ~~**Arm netconsole (E0)**~~ — done 17:29:58 on 10-27: the module is loaded,
   `network logging started`, and the config survives reboots
   (`/etc/modprobe.d/netconsole.conf` + `/etc/modules-load.d/netconsole.conf`).
   What is left is the **receiver**, and it has two requirements:

   ```bash
   # on ANOTHER host on 192.168.105.0/24 (this box cannot receive its own
   # broadcast frames):
   socat -u UDP-RECVFROM:6666,fork - >> gb10-console.log

   # then prove the path end to end from this box:
   sudo tools/arm-gb10-crash-witness.sh --no-apst   # writes a test line
   # -> the receiver must show it within a second
   ```

   If the receiver stays empty, the cause is the broadcast path, not the
   witness: aim it at the receiver directly with
   `GB10_WITNESS_BCAST=<receiver-ip> GB10_WITNESS_MAC=<receiver-mac>`.
3. **Run the next overnight session with E1 live** (APST is already off in
   the running kernel) and keep the same headroom, so the run stays
   attributable to E1 alone. Kill criterion: 2–3 sessions >2 h with no
   freeze.
4. **Then E2** (`sudo tools/nvme-stall-mitigations.sh apply coalescing`) —
   it takes effect live, and the service is still enabled, so `0x107` is
   currently the one suspect still in force.
5. The `ttyS0` console behind the MTKI0511 companion is still the definitive
   channel if the management network exposes it (serial-over-LAN / Redfish
   SOL); netconsole covers the same ground for a storage stall at zero cost.