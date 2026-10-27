---
name: gb10-hardlock-triage
description: Use when the DGX Spark GB10 freezes, hard-locks, or power-cycles with no shutdown sequence — the maya engine is simply gone, the guard log shows no kill for it, and journalctl for that boot stops mid-stream. Covers the hard-lock signature and how to tell it apart from a guard kill, the evidence files and the order to read them, the causes already ruled out on this box, the expert-pool headroom lever that reduces exposure, and the SBSA watchdog that converts a manual power-cycle into an auto-reboot.
---

# GB10 hard-lock triage

Repo: `~/projects/maya`. Companion to `dgx-spark-memory-forensics` (memory state,
leaks, WARN bands) — that skill covers *soft* pressure; this one covers the
*whole-box freeze* that no memory floor ever sees.

## 1. Is it a hard lock, or a guard kill?

A guard kill is **logged**. A hard lock is not. Check first:

```bash
tail -20 ~/gpu_monitor_guard.log        # any SIGTERM/SIGKILL naming the engine at that time?
```

Hard-lock signature — all of these together:

| Evidence | Hard lock | Guard kill |
|---|---|---|
| `gpu_monitor_guard.log` | no entry for the victim | `LOW device_free … -> SIGTERM <proc>` |
| Last sample before death | healthy: `F:` 7–15 GiB, `N:0`, PSI 0%/0% | `F:` below the 3072 MiB floor |
| `~/gpu_reports/gpu_report_<ts>.txt` at the death minute | **0 bytes** (monitor killed mid-write) | full, normal size |
| Engine log | truncated mid-line, tail padded with **NUL bytes** | ends with a clean `done:` line |
| `journalctl -b -1` | stops mid-stream, **no shutdown sequence** | nothing unusual |
| Engine process | gone, no core | gone |

The 0-byte report and the NUL padding are the two cheapest tells: the monitor
writes a report every sample, so a zero-length one at the death minute means
the box died inside that write. NUL padding is the engine's unflushed stdio
buffer, which only a freeze loses.

**Look for a report timestamp *after* the last log line.** On 2026-10-25 18:36
the last sample was `18:36:48`, journald's last entry was `18:36:48.680`, but
`gpu_report_20261025_183659.txt` exists and is 0 bytes. The monitor did start a
sample 11 s later and hung inside it — i.e. the GPU/`nvidia-smi` path died
first and the kernel followed. That timestamp is the tightest bound you get on
the freeze instant.

**Count the 0-byte reports — one vs. a cluster tells you how the box died.**
On 2026-10-25 each freeze left a *single* 0-byte report, bounding the freeze to
one sample. The 2026-10-26 15:21 freeze left **four consecutive** ones
(15:20:58, 15:21:02, 15:21:09, 15:21:20) spanning 22 s, while journald's last
entry was 15:20:47.761 — i.e. journald died ~30 s *before* the monitor did, and
the monitor's `nvidia-smi`/`/proc` queries still worked to the very end. A
cluster means a **progressive, layered freeze** (logging first, then the write
path, then everything), not an instant one. Mechanism: the sample line is
appended and closed (1285-1286) *before* the report is opened (1291), and the
report is ~743 B — under Python's 8 KiB stdio buffer — so it only lands on close.
A 0-byte report therefore means "died between `open(rp,'w')` and close".
Cross-check it: `print("report: …")` (1315) reaches syslog for healthy samples
and for none of the cluster.

## 2. Evidence, in the order to read it

```bash
journalctl --list-boots --no-pager | tail -6      # boot boundaries; the freeze is an unclean end
journalctl -b -1 --no-pager | tail -30            # the dead boot: ends mid-stream?
grep -a "17:4[0-9]:" ~/gpu_monitor_log.txt | tail -20   # last samples (see forensics skill for the field layout)
ls -la ~/gpu_reports/ | grep -E "17:4[89]"        # the 0-byte ones = the freeze instant
tail -c 600 <engine log> | tr -d '\0'             # last flushed engine line
dmesg | grep -iE "Power-on or device reset"       # next-boot reset cause
ls /var/crash/                                    # a vmcore means a panic; a hard lock leaves none
ls -la /var/crash/2*/                              # panic dmesg + vmcore are root:root 0600 -> one sudo read
zcat /var/log/syslog.2.gz 2>/dev/null | tail -30   # deaths before the current syslog survive HERE
tail -30 /var/log/syslog.1                         # syslog rotates at midnight; journalctl dies with the box
```

**The rotated syslogs are the evidence journalctl cannot give you.** journald
dies with the box and its journal ends mid-stream, but rsyslog's *files*
(`/var/log/syslog`, `.1`, `.2.gz`…) survive the power-cycle. Two tells:
- syslog **truncated mid-line** = an instant death;
- **journald's last entry minutes/seconds before the monitor's last sample**
  = a *layered* death (see §1's 0-byte-report method for the same event from
  the monitor's side). Layered deaths: 10-25 17:49 (+19 s), 18:36 (+22 s),
  10-26 15:21 (+33 s). Instant deaths: 10-24 21:08, 10-25 17:11:55.

Files that matter:

| Path | What |
|---|---|
| `~/gpu_monitor_log.txt` | samples — `F:` device-free, `N:` new NVRM refusals, `M:` device total, `guard:` |
| `~/gpu_monitor_guard.log` | every WARN / armed / SIGTERM / SIGKILL decision |
| `~/gpu_engine_tail.txt` | the monitor's rolling copy of the engine's last 40 lines |
| `~/gpu_reports/gpu_report_*.txt` | per-sample snapshots; **0 bytes = the freeze instant** |
| `~/gpu_reports/stall_*.txt` | stall-guard snapshots, if a `stall:` tag appears |

Trust order for memory numbers: `F:` and `N:` first, `A:` (MemAvailable) last.
`MemAvailable` lies on a GB10 — the CUDA pool *is* system RAM.

## 3. Triage procedure

1. **Confirm it is not a guard kill** (§1). If the guard killed it, stop — that
   is a memory problem, use the forensics skill.
2. **Pin the freeze timestamp** from the last sample line and the dead boot's
   final journal entry.
3. **Read the last full report** plus the 0-byte ones — `F:`, `N:`, PSI, temps,
   and the process table with RSS.
4. **Check the dead boot's kernel log** for `Xid`, `NVRM`, `oops`, `SError`,
   `Call trace`. On this box it has come back **clean every time**.
5. **Rule out the known non-causes** (§4) — do not re-derive them.
6. **Record the engine's device footprint at death** (`M:` in the last sample).
   That is the one live signal (§5).
7. **Check whether the watchdog fired**: freeze time → next boot time. With
   `RuntimeWatchdogSec=30s` a self-reset should land 30–60 s after the freeze;
   a multi-minute gap means someone power-cycled by hand.

## 4. Already ruled out on this box (2026-10-24 → 10-25, 5+ unclean deaths)

Do not spend a session re-testing these:

- **Memory pressure** — `N:0` (zero NVRM refusals), `F:` 7–15 GiB free,
  PSI 0%/0% at every death.
- **Xid / driver errors** — none logged in any dead boot. The driver's kernel
  log was clean right up to each freeze.
- **Kernel panic / oops** — none in any dead boot *before 2026-10-26 15:58*;
  the soft-lockup detector is enabled (`/proc/sys/kernel/watchdog` = 1) and
  never fired, so it was not even a CPU stall. The one 15:58 panic was an
  **induced kdump test** (user-confirmed), not a box death — see the kdump
  bullet. Note that a panic printk does *not* reach syslog/kern.log — rsyslog
  dies first — so a clean `journalctl -k -b -1` no longer rules out a panic.
  Only `/var/crash` does.
- **WiFi (mt7925e)** — driver clean; a roaming storm was real (564 auths on
  10-25) but only 1 of 5 crashes had a wifi event within 60 s. **Fixed** by
  pinning the BSSID and disabling powersave; it was never the cause.
- **Cron / systemd timers** — nothing fires in the crash windows.
- **The hermes-dashboard crash-loop** — it loops all day, including healthy
  periods.
- **Engine bugs** — the 10-25 crashes all caught it mid-decode of a routine
  thinking phase. The engine's own graceful error handlers never ran because the
  kernel froze underneath them. The 18:36:48 freeze came right after a 1001 s
  generation finished with `cancel=True` and the next queued request's 512-token
  prefill completed — so "a new request was admitted" is *not* excluded; treat
  the prefill/pool-borrow path as in scope.
  **Caveat on the 2026-10-26 15:58 event:** the box was idle with no maya
  process when it died, but that panic was an *induced kdump test*
  (user-confirmed) — so it says nothing about whether engine state matters for
  the unexplained deaths. Treat engine/pool/load involvement as still open.
- **Thermal / power** — 83–87 °C and 53–57 W are normal sustained load for a
  GB10; there is no spike at any freeze.
- **kdump** — armed, and it **works**: an induced test panic on 2026-10-26
  15:58 (user-confirmed, another agent) left
  `/var/crash/202610262258/{dmesg,dump}.202610262258` (134 KB + 1.5 GB,
  root-only) and the box **auto-rebooted ~90 s later, no human needed**. All
  *unexplained* deaths so far were hangs, not panics, and leave `/var/crash`
  empty — a hang is exactly the case kdump cannot catch. **Always check
  `/var/crash` first now**; a panic's only witness is there, since the printk
  never reaches syslog. Recovery-timing tell: ~90 s gap = kdump auto-reboot
  after a panic (or an induced test); a multi-minute gap = a hang that needed a
  manual power-cycle. See `docs/2026-10-26-kernel-panic-1558-idle.md`.

**Read the kernel message correctly:** `watchdog: Hard watchdog permanently
disabled` and `watchdog: NMI not fully supported` refer to the kernel's *NMI
hard-lockup detector*, **not** to the SBSA hardware watchdog. The SBSA timer
initializes normally — `sbsa-gwdt sbsa-gwdt.0: Initialized with 30s timeout …
action=1. [enabled]` — and is what the auto-reboot uses. Conflating the two
sends you down a dead end.

## 5. The one live hypothesis: expert-pool footprint

Every death so far happened with a **72–95 GB CUDA pool** held at 94–95% GPU
util. Observed on 2026-10-25:

| Freeze | Pool budget | Device held (`M:`) | Survived |
|---|---|---|---|
| 17:11:55 | 63.69 GB (headroom 32) | 72.8 GB | ~1h45m |
| 17:49:24 | 62.66 GB (headroom 32) | 83.3 GB | ~28 min |
| 18:36:48 | 73.54 GB (headroom 24) | 85.1 GB | ~17 min |

**Honest limits:** n=3, and the two headroom-32 runs differed by 10 GB in `M:`
and 4× in survival, so headroom alone is not a proven predictor — total device
footprint tracks survival better than the budget does. Treat this as the
strongest available lead, not a settled cause. The alternative hypothesis is a
driver/firmware bug (see §7).

> **2026-10-26 15:21 — this lead is falsified, do not spend a session on it.**
> That freeze ran headroom 40 GB (pool budget 55.4 GB) and held only
> `M: 66890 MiB` = 65.3 GB on the device — the *smallest* footprint and the
> *highest* `F:` (23.57 GiB) of any recorded death — and it froze anyway after
> 52m29s. See `docs/2026-10-26-hardlock-1521-headroom40.md`. So neither the
> budget nor the total footprint predicts survival, and the §6 lever is
> exhausted: it costs ~13 % decode per step and buys nothing measurable. Do not
> escalate headroom further on this hypothesis's strength. What *did* change:
> the freeze followed admission of a 50,511-token prompt (see §4's "a new
> request was admitted" note) — n=2 now, still a candidate, not a cause.

## 5b. The current ranked lead: a storage/writeback stall (NVMe)

From the same-evening cross-death pass (`docs/2026-10-26-hardlock-1521-headroom40.md`
§"Follow-up investigation"): **journald dies first, the monitor's file writes
survive +18–33 s.** Page-cache writes succeed while writeback is stalled;
journald blocks because it is the one process that fsyncs. GPU queries keep
working. Kernel messages cannot be persisted — so *silence in kern.log is no
longer evidence of an instant freeze*. Stressor: the engine streams 13–16 GB
per prefill chunk and ~25 reads/token while decoding; the disk is saturated
whenever the engine runs, and every unexplained death happened under engine
load. This also explains the missing vmcore at 15:21: the watchdog expires
only once the freeze is total (systemd's loop — which pets it — stays alive
during an I/O-only freeze), and kdump's 1.5 GB save then hangs on the same
stalled disk. Discriminating tests, in order: **USB-UART serial logger** (the
cmdline already has `console=ttyS0,115200`), **`kernel.hung_task_panic=1`** +
`hung_task_all_cpu_backtrace=1` (root, runtime — turns the next stall into a
vmcore), and **`nvme smart-log` / `nvme error-log`** (root; Samsung keeps a
persistent controller error log across power cycles).

### Worked example — 2026-10-25 18:36:48

```
18:09:47  kernel: NVRM NV_ERR_NO_MEMORY (F had fallen to ~1.3 GiB)
18:09:59  guard: SIGTERM + KILL maya (logged — this one *was* a memory kill)
18:12:09  guard: SIGTERM + KILL maya again
18:13     engine restart: headroom 24, pool budget 73.54 GB
18:14-32  F: ~8.0 GiB, flat, M: 85124 MiB, 92-94% util
18:33     F steps 8.0 -> 6.93 GiB and stays flat
18:36:48  last sample + journald's last entry; no shutdown sequence
18:36:59  gpu_report_20261025_183659.txt: 0 bytes (monitor hung in nvidia-smi)
18:40:30  next boot (3m42s later = manual power-cycle timing, not 30-60s watchdog)
```

The engine log is NUL-padded; its last flushed line is a completed 512-token
prefill, and the `gpu_engine_tail.txt` from the next boot shows the previous
1001 s generation finished with `cancel=True` just before it. The stall guard
needs `STALL_QUIET_S` (90 s) of engine silence while util >= 90% — it got ~11 s,
so it never armed.

Lesson: **the earlier `NV_ERR_NO_MEMORY` is a warning shot.** After any refusal,
the next engine run starts closer to the edge than its budget suggests. Restart
with more headroom and keep `F:` >= ~10 GiB; do not treat a flat 7-8 GiB as
"healthy enough" just because it is above the 3 GiB floor.

## 6. The lever: `MAYA_GLM_RAM_HEADROOM_GB`

On a unified-memory device (GB10 / Grace) the headroom sizes the expert pool:

```
pool budget = MemAvailable − headroom − 1 GB reserve
```

Engine default is **16** (`head_gb = 16.0` in `src/core/glm_fast_path.cu`).
`maya.py` only injects 16 for the HIP backend; on CUDA it sets nothing, so a
plain launch gets the default. Larger headroom → smaller pool → less device
footprint, at the cost of streaming more experts from disk (~13% slower decode
observed going 16 → 32).

**Set it in the config, never as an ad-hoc env var.** The 10-25 crashes were
three *different* headrooms with nothing recording which, because each run
inherited it from a one-off export — that silent drift also confounded the
lowthink A/B test.

```json
"env": {
  "MAYA_GLM_RAM_HEADROOM_GB": "32"
}
```

Why this form is durable:

- `tools/maya_names.py` maps `MAYA_*` → the `STRATA_*` names the engine reads;
  `serve/server.py` applies it on load and `child_env()` passes it through.
- **The config wins over any shell export** — `child_env` applies `cfg["env"]`
  *after* `os.environ`, so even `run-maya-*.sh` (which exports 24) yields 32.
- It survives `--calibrate`: `calibrate_glm.py`'s `CAL.apply` only owns
  `STRATA_GLM_PCIE_SHARE` / `CPU_LANE` / `READ_CHUNKS`.
- It prints in the engine's startup line and the dashboard's `Config: … env {…}`,
  so every run is self-documenting.

**Always verify from the engine log**, not from the launch command:

```bash
grep -a "unified memory:" <engine log> | tail -1
# glm fast: CUDA0 unified memory: MemAvailable 97.79 GB, headroom 32.00 GB + 1024 MB
#   reserve, experts need 80.17 GB, pool budget 64.79 GB
```

Escalation if the smaller pool also freezes: headroom 40 (pool ~47 GB). That is
untested here — take the known-good step first rather than over-correcting into
heavy disk streaming.

## 7. Turning a manual power-cycle into an auto-reboot

`tools/hardlock-watchdog.sh` (in the maya repo; `status` / `apply` / `revert`,
`--dry-run`). It arms the **SBSA Generic Watchdog**, which is two-stage
(`action=1`): WS0 raises an interrupt and panics, WS1 asserts a real hardware
reset one timeout later. The reset line is driven by the SoC, not by Linux, so
it fires even when userspace and the driver are wedged.

It writes `/etc/systemd/system.conf.d/50-hardlock-watchdog.conf`:

```ini
[Manager]
RuntimeWatchdogSec=30s
RebootWatchdogSec=30s
```

and sets `kernel.panic=15` so a panic reboots instead of hanging.

**Unproven — check it every time, and it has now failed twice.** The freeze
after install took 3m42s to come back (manual power-cycle timing, not the
expected 30–60 s). The 2026-10-26 15:21 freeze took **13m30s** — same story:
the timer was armed (`active / 30 / SBSA Generic Watchdog`,
`RuntimeWatchdogSec=30s`) and the board still needed a human. The likely
mechanism is visible in the evidence: journald and systemd's event loop stop
first (15:20:47), and `RuntimeWatchdogSec` is petted by that same loop, so the
expiry should panic (WS0) and then assert the SoC reset (WS1). Neither fired.
**Operational rule: assume a human must power-cycle; never plan around
auto-recovery.** Why WS1 does not assert is an open work item, separate from the
freeze cause. Verify with:

```bash
cat /sys/class/watchdog/watchdog0/{state,timeout,identity}   # active / 30 / SBSA Generic Watchdog
grep RuntimeWatchdogSec /etc/systemd/system.conf.d/50-hardlock-watchdog.conf
```

and compare the freeze timestamp against the next boot's first journal entry.
## 8. Getting a real root cause

Ranked, cheapest first:

1. **USB-UART on ttyS0** — the kernel already runs `console=ttyS0,115200` +
   earlycon, so a panic or oops prints to the physical UART, the only channel
   that survives a journal stall. Needs a 3.3 V USB-TTL adapter and a second
   machine. This is the definitive channel.
2. **Driver upgrade** — 580.95.05 → 580.178.04 plus the HWE kernel
   7.0.0-1019-nvidia (both from noble-updates/security, Canonical's published
   path for this hardware line). New GSP firmware
   (`nvidia-firmware-580-580.178.04`) is the part most likely to change freeze
   behaviour. Use `~/upgrade-gb10-580-careful.sh` (not the older
   `upgrade-gb10-580.sh`): it refuses to run with the engine holding the GPU,
   `dpkg-repack`s the exact installed .debs first, and arms a **one-shot** boot
   into the new kernel (`GRUB_DEFAULT=saved` + `grub-reboot`) so a hang falls
   back to 6.14.0-1015 on the next power-cycle. It does **not** reboot.
   Caveat: the upgrade removes the nvidia modules for the 6.14 kernels.
   The archive's 6.14 module is built for 580.173, **not** 580.178, so
   "old kernel + new driver" is not available. Rollback means the full 580.95.05
   set in `~/rollback-gb10-580/debs-driver/` (17 debs):
   `sudo dpkg --remove linux-modules-nvidia-580-open-7.0.0-1019-nvidia` then
   `sudo apt-get install -y --allow-downgrades ~/rollback-gb10-580/debs-driver/*.deb`
   on the old kernel. Do NOT glob `debs-exact/` (79 OEM packages).
   Pure software — nothing here can brick the GPU.
3. **grub prep** — add `softlockup_panic=1 hardlockup_panic=1`, drop `quiet`,
   add `loglevel=7`. Insurance only: these freezes never tripped the soft-lockup
   detector, so it may not fire.
4. **BMC / Redfish event log** from the management network — would settle who
   resets the box. `/dev/ipmi0` does not exist here, so this needs the
   management network.

## 9. Cross-references

- `dgx-spark-memory-forensics` — the *other* failure class: device-free dips,
  WARN bands, step-vs-leak, naming a holder.
- `maya-guard-hardening` — the guard itself (`tools/gpu_monitor.py`,
  `safe-build.sh`), its invariant-preserving workflow, and the deploy pattern.
- `docs/MEMORY_WATCHDOGS_DEEP_DIVE.md` in the maya repo — the full watchdog
  design, including the design-level avoidance section.
- `tools/gpu_monitor.py` header block — guard internals.
- `docs/DGX_SPARK.md` — the platform bring-up, including the pool sizing.
