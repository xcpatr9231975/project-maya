# 2026-10-26 15:21 hard lock — headroom 40, healthy memory, freeze during a 50K prompt read

Date: 2026-10-26 (post-mortem after the fact) · review due: week of 2026-11-02
Status: **unattributed** — same hard-lock class as 2026-10-24/25, but with the
healthiest memory and the smallest device footprint of any death so far, which
falsifies the §5 lead in `.opencode/skills/gb10-hardlock-triage`.
Affects: this DGX Spark (`carlosp-nvidia-dgx`), engine pid 327681.
Companion: `.opencode/skills/gb10-hardlock-triage` (the triage procedure this
follows) and `dgx-spark-memory-forensics` (the *other* failure class).

## The freeze

| | |
|---|---|
| Engine start | 14:28:51 (pid 327681) |
| Last healthy sample | 15:20:47 |
| Last sample of any kind | **15:21:20** |
| Next boot | 15:34:50 (13m30s later) |
| Survival | 52m29s |
| Config | headroom 40 GB, `--prefill 16384`, pool budget 55.39–55.73 GB |

Recovery was a **manual power-cycle**, not the watchdog (see §4).

## Hard-lock signature — every tell present

| Evidence | Observed |
|---|---|
| `~/gpu_monitor_guard.log` | no entry naming the engine; last entry 11:49:44 — **not a guard kill** |
| Last full report (15:20:47) | `F: 23.45GiB`, `N: 0`, PSI `0%/0%`, `T: 80°C`, `P: 40.68W` — healthy |
| `~/gpu_reports/gpu_report_2026102{58,152102,152109,152120}.txt` | **four consecutive 0-byte reports** |
| Engine log | truncated mid-line, 1916 trailing NUL bytes |
| `journalctl -b -1` | stops mid-stream at 15:20:47.761, no shutdown sequence |
| `/var/log/kern.log` | clean: no `Xid`, no `NVRM`, no `oops`, no `SError`, no `hung task`, no MCE |
| `/var/crash/` | no vmcore (kdump is armed; a panic would have left one) |

## New evidence: the freeze was progressive, over ~22 s

The 10-25 deaths each left a **single** 0-byte report, which bounded the freeze
to one sample. This one left **four**, at 15:20:58, 15:21:02, 15:21:09 and
15:21:20. Cross-referenced with the journal, the box died in layers:

```
15:20:47  last full report (743 B) AND journald's last entry
15:20:58  sample line written; report 0 B   <- journald already dead
15:21:02  sample line written; report 0 B
15:21:09  sample line written; report 0 B
15:21:20  sample line written; report 0 B   <- last sign of life
15:34:50  next boot
```

journald went first (~15:20:48); the monitor kept sampling for 22 more seconds;
then everything stopped.

### What a 0-byte report actually means

`tools/gpu_monitor.py:_main_locked()` appends and closes the sample line to
`LOG_PATH` (1285-1286) **before** it opens the report (1291). The report is
~743 bytes — far under Python's 8 KiB stdio buffer — so it only reaches disk on
close. A process that stops making progress between `open(rp, "w")` and the
implicit close leaves a 0-byte file. Confirmed independently: `print("report:
…")` (1315) reached syslog for every healthy sample and for **none** of the
four, so all four died before that line.

So each 0-byte report is one monitor invocation that got as far as opening its
report and then stalled. `nvidia-smi`, `/proc`, meminfo, PSI and the nvrm check
all still worked at 15:21:20 — the stall was in the write path, not the query
path.

## The engine's last state (from `~/gpu_engine_tail.txt`, captured 15:21:20)

```
[maya] done: 4930 tokens in 487 s (10.9 tok/s) (stop, cancel=False), expert cache 95.5% hit
[maya] this request waited 535 s for the engine (1 request(s) behind it)
glm slots: saved a 20219-token conversation (0.41 GB in 0.2 s); 1 kept, 0.41 GB
glm checkpoints: 3352 of the prompt's 50511 tokens from a checkpoint
[maya] WARNING: 2 request(s) have waited for the engine for up to 495 s ...
[maya] reading the prompt: 50,511 tokens, 10 s so far      <- last line
```

A 4930-token generation finished **cleanly** (`cancel=False`), the session was
saved, a **new request with a 50,511-token prompt was admitted**, and the box
froze ~10 s into reading that prompt.

This is the same class the triage skill already flags in §4 — "a new request was
admitted is *not* excluded; treat the prefill/pool-borrow path as in scope" — the
2026-10-25 18:36:48 freeze also followed a request admission. The difference is
prompt size: 50,511 tokens here vs. 512 there, ~100× larger. n is still 2, so
prompt size is a candidate, not a cause.

### Why the engine log is shorter than the monitor's copy

The on-disk log ends at `… 4908 … 485 s`; the monitor's 15:21:20 capture runs
eleven lines further. The monitor only ever *reads* the engine log
(`_roll_engine_tail` → `_atomic_write` to `gpu_engine_tail.txt`), so it did not
truncate it. The 1916 trailing NULs are the **ext4 delayed-allocation hole left
by the unclean power-cycle**: the tail was still in the page cache, never
reached the disk, and the file size survived in the journal without its data.
The rolling tail copy is the only reason those eleven lines exist at all — the
skill's design is doing exactly its job.

## What this death rules out

- **Memory pressure.** `N: 0`, `F: 23.57 GiB` at the last sample. That is the
  *highest* device-free of any recorded death (10-25 deaths sat at 7–15 GiB).
- **The expert-pool footprint lead (`gb10-hardlock-triage` §5) is falsified.**
  This run used headroom 40 GB (pool budget 55.4 GB) and held only
  `M: 66890 MiB` = 65.3 GB on the device — the smallest footprint of any
  recorded death — and it still froze. The §6 lever (headroom 16 → 32 → 40) is
  therefore exhausted as a mitigation; it costs ~13 % decode throughput per step
  and this run proves it does not buy survival. Do not escalate to headroom 40+
  on the strength of that hypothesis.
- **Thermal.** 88 °C was recorded in *every* hour from 06:00 to 15:00 today
  (9 samples), and 62–69 W sits inside the day's p95 (69.4 W). There is no
  thermal excursion at the freeze; 88 °C is this box's normal load plateau.
- **Stall.** No `stall:` tag, no `stall_*.txt` snapshot — the engine was
  actively generating, so the stall guard correctly never armed.
- **Engine bug.** Its graceful handlers never ran; the kernel froze underneath.
- **Panic.** kdump armed, `/var/crash` empty.

## The watchdog is not delivering auto-recovery

`/sys/class/watchdog/watchdog0/` reads `active / 30 / SBSA Generic Watchdog` and
`RuntimeWatchdogSec=30s` is set — yet the freeze took **13m30s** to recover
instead of the expected 30–60 s. This is the second confirmation of the triage
skill's §7 "Unproven — check it every time" caveat.

The evidence now explains the likely mechanism: journald and systemd's event
loop stopped at 15:20:47. `RuntimeWatchdogSec` is petted by systemd's own loop,
so a kernel-wide stall that blocks that loop should let the timer expire, panic
(WS0) and then assert the hardware reset (WS1). Neither happened. So either the
panic path hung, or WS1's SoC reset line is not actually asserted on this board.

**Operational consequence: assume a human must power-cycle. Do not plan around
auto-recovery.** Investigating why WS1 does not fire is a separate work item —
it is a distinct failure from the freeze itself.

## Next actions, ranked

1. **USB-UART on ttyS0** (`console=ttyS0,115200` + earlycon is already in the
   cmdline). It is the only channel that survives a journal stall, and it is the
   only thing that will turn these from "unattributed" into a root cause. Needs
   a 3.3 V USB-TTL adapter and a second machine. Until it exists, expect the
   same outcome next time.
2. **Do not restart the engine on this evidence.** With the footprint lead
   falsified there is no config change with a known payoff, and any restart
   re-exposes the box to an unattributed freeze.
3. **Watchdog WS1 investigation** — why the armed SBSA timer does not reset the
   board. Independent of the freeze cause; worth fixing on its own.
4. If a restart is needed anyway, keep headroom 40 (it is the best-measured
   throughput/safety point) and treat the 50K-prompt admission as the thing to
   watch, not the memory numbers.

## Follow-up investigation (same evening) — the cross-death pattern

A pass over every death in the record (rotated `syslog.1`/`syslog.2.gz` +
`kern.log` + monitor log + guard log) found a consistent signature that the
original post-mortem did not have:

| Death | Boot end | journald's last entry | Monitor's last sample | Mode |
|---|---|---|---|---|
| 10-24 21:08 | `-7` | **truncated mid-line** 21:08:07.499 | 21:08:07 | instant |
| 10-25 17:11 | `-6` | 17:11:55.626 | 17:11:55 | instant |
| 10-25 17:49 | `-5` | 17:49:04.99 | 17:49:23 | **layered (+19 s)** |
| 10-25 18:36 | `-4` | 18:36:26.67 | 18:36:48 | **layered (+22 s)** |
| 10-26 15:21 | `-2` | 15:20:47.76 | 15:21:20 | **layered (+33 s)** |

**journald dies first; the monitor's file writes survive +18–33 s.** In the
layered deaths the monitor kept completing full samples (`nvidia-smi` ioctls,
`/proc`, meminfo, PSI all worked) and kept appending to its own log file while
journald was already gone. Writes that land in page cache succeed while
writeback is stalled; journald blocks because it is the one process that
fsyncs. **Ranked hypothesis: a storage/writeback stall (NVMe), not a CPU or
GPU freeze** — it explains journald-first, the surviving page-cache writes,
working GPU queries, zero kernel messages (nothing can be persisted, and the
ring buffer dies with the power-cycle), and the 1916-NUL delayed-allocation
holes at the tail of every log. It also now has a plausible stressor: the
engine streams **13–16 GB from the NVMe per prefill chunk** and ~25
reads/token while decoding — the disk is saturated whenever the engine runs,
and every unexplained death happened under engine load.

**It also explains the missing vmcore at 15:21.** The SBSA watchdog is petted
by systemd's userspace loop, which stays alive during an I/O-only freeze
(hermes-dashboard was still being (re)started 19 s before the 17:49 death) —
so the 30 s timer never expires *until the freeze becomes total*. When it
finally does expire (~30–60 s after the last sign of life), the panic path
runs kdump — and **kdump's save writes 1.5 GB to `/var/crash` on the same
stalled disk, so it hangs**: no vmcore, no reboot, 13m30s of dead box. The
induced 15:58 panic on a healthy box saved its vmcore in ~40 s and rebooted in
~90 s — the kdump path works when storage is healthy.

**What this changes in the ruled-out list:** the earlier "no kernel messages
anywhere" observation is no longer evidence of an instant freeze — under this
hypothesis messages *cannot* be persisted. "Engine bug" stays ruled out
(handlers never ran), but the trigger is now a candidate platform/storage
fault, not the engine's own state.

**Discriminating tests, ranked** (all need either hardware or one sudo):
1. **USB-UART serial logger** (`console=ttyS0,115200` is already in the
   cmdline): a hung-task backtrace, an `nvme ... completion polled/timeout`
   line, or total silence settles it at the next freeze.
2. **`sysctl -w kernel.hung_task_panic=1 kernel.hung_task_all_cpu_backtrace=1`**
   (root, runtime, no reboot): the next I/O stall panics at 120 s instead of
   hanging forever → kdump gets a vmcore *if* the disk recovers, and the
   hung-task stacks name the blocked subsystem.
3. **`nvme smart-log` + `nvme error-log /dev/nvme0`** (root): Samsung drives
   keep a persistent controller error log across power cycles — the first
   place a dying/firmware-buggy NVMe leaves a trace.

## Cross-references

- `.opencode/skills/gb10-hardlock-triage` — the triage procedure; §5's
  footprint hypothesis and §7's watchdog caveat are both updated by this event.
- `dgx-spark-memory-forensics` — the *other* failure class (soft pressure).
- `docs/MEMORY_WATCHDOGS_DEEP_DIVE.md` — the guard design.
- `docs/2026-10-25-driver-580-residual-kernels.md` — the driver upgrade this
  box has been running since 2026-10-25 19:09; the freezes predate and postdate
  it, so the driver version is not the discriminator.
