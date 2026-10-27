# PSI-io on the DGX Spark GB10 — what it is, what this box reads, and the anticipate/prevent plan

Date: 2026-10-26 · status: **measured baselines captured live** · companion skill:
`.opencode/skills/gb10-io-pressure` (the triage procedure) · review due: week of 2026-11-02

## 1. What PSI-io is

**Pressure Stall Information** (kernel ≥ 4.20) measures how much wall-clock time
tasks spend *waiting* for a resource — not how much of the resource is used.
`/proc/pressure/io` reports, per line (`some` / `full`):

| Field | Meaning |
|---|---|
| `some avg10=13.07` | over the last 10 s, tasks were stalled on I/O **13 % of the time** (at least one non-idle task was waiting) |
| `avg60`, `avg300` | the same over 60 s / 300 s — the trend |
| `full avg10=12.86` | **all** non-idle tasks were stalled simultaneously that fraction of the time — the whole workload is bottlenecked on one resource |
| `total` | cumulative stall microseconds since boot |

The three meters: `io` (blocked on disk/blk I/O: read, write, fsync, writeback),
`memory` (blocked on allocation/reclaim), `cpu` (runnable, not scheduled). PSI-io
is the *only* one that saw nothing in this box's history — the 10 s monitor
samples PSI **memory** only (`tools/gpu_monitor.py`, `psi_memory()`), which is why
every historical sample line says `0%/0%` through runs that streamed 1.5 GB/s
from the SSD.

## 2. Why it matters here (the 15:21 hard lock in one paragraph)

The 2026-10-26 15:21 freeze died at the decode→prefill boundary with every
observable subsystem healthy (F: 23.57 GiB, N:0, memory PSI 0, guard silent), and
left only writeback artifacts behind: 0-byte reports, a 0-byte 0.41 GB slot save,
a journal cut at 15:20:58, NUL-padded engine-log tail. The engine's own telemetry
names the mechanism class: a prompt read re-streams the expert shortfall from the
SSD for every chunk (`pcie 27-28 GB, ram 0.00, disk 27-28` at 5.2-6.2 GB/s),
decode streams `disk 20.6-27.6 reads/token` continuously, and a slot save drops
0.41 GB of writes at the same boundary. PSI-io is the **direct, free, per-second
witness** of exactly that symptom class — if the write path had begun stalling at
15:20:58, `io full avg10` would have climbed minutes before the freeze. Nothing
else on the box can see it: `sar` runs at 10-minute resolution and never sampled
the fatal interval.

## 3. Measured baselines on this box (2026-10-26, headroom 40, engine pid 74893)

All readings taken live with the engine loaded and serving; `U:` is the monitor's
GPU util at the same moment.

| Regime | PSI-io `some/full avg10` | Notes |
|---|---|---|
| **True idle** (U:0 %, between requests) | **0.00 / 0.00** (avg60 0.22 draining) | the clean baseline; also captured as a full avg10 decay 4.4 → 0.9 over ~18 s after a request ends |
| **Decode steady** (U:95 %, 10.8 tok/s, `disk 20.6 reads/tok`) | **5.4-10.4 %**, avg60 7-9 % | the streaming baseline under load — *expected, not a fault* |
| **Async burst** (1.65 GB/s read over 6 s) | 3.62 / 3.62 | PSI tracks **stall time, not throughput**: the async copy lane moves more bytes with less stall than decode's latency-bound sync reads |
| **Prefill copy-lane burst** (5.2-6.2 GB/s, 24-28 GB/chunk) | **not yet sampled** — capture on the next prefill; expect ≥ the decode band | the one number the plan needs before thresholds are final |
| **Slot save** (0.41 GB write at the boundary) | not yet sampled | the write-path canary (§5) covers this instead |
| Memory PSI (any regime) | 0.00 / 0.00 / 0.00 | memory is never the pressure on this box |
| Swap | 0 B of 80 GiB used | Dirty ~7 MB idle, Writeback 0 |

**The `some ≈ full` signature** (observed within 0.5 everywhere): a stall hits
*all* work at once — the box is single-purpose and everything queues on the same
NVMe. When `full` and `some` diverge, something new is stalling that the engine's
workload does not depend on.

## 4. How to read it (commands)

```bash
cat /proc/pressure/io                                  # one reading
# a 60 s watch alongside the monitor's util:
while :; do printf '%s ' "$(date +%T)"; tr '\n' ' ' </proc/pressure/io; \
        tail -1 ~/gpu_monitor_log.txt | grep -ao 'U:[0-9]*%\|F:[0-9.]*GiB' | tr '\n' ' '; echo; sleep 5; done
# D-state tasks (what PSI is counting):
ps -eo state,pid,comm | awk '$1=="D"'
# the blocked count sar records (10-min resolution):
sar -f /var/log/sysstat/sa26 -q
# who is reading (per-process I/O delta, 3 s):
#   see the snippet in .opencode/skills/gb10-io-pressure/SKILL.md §5
```

## 5. Thresholds for THIS box (decision table)

| Reading | Meaning | Action |
|---|---|---|
| `full avg10 < 1 %` with U:0 % | idle baseline | nothing |
| `full avg10 2-20 %` with U > 50 % | decode/prefill streaming — the measured baseline under load | nothing; do **not** alarm on this |
| `full avg10 > 20 %` sustained 3+ samples, U > 50 % | heavier than anything measured (a bigger prefill, a competing reader) | WARN: name the reader (§4), check `F:` and the queue depth; keep sampling |
| `full avg10 > 40 %` for 2+ samples **or** `full ≈ some` climbing while U < 20 % **or** fsync canary > 2 s | the write-path-stall class — the pre-freeze signature | ALARM: snapshot immediately (D-state list with stacks, `/proc/meminfo` Dirty/Writeback, `nvme0n1` diskstats, the engine tail), then decide; hung_task panics at 120 s anyway |
| D-state tasks > 5 for > 60 s | a stuck I/O path | ALARM (same snapshot) |
| `full avg10 > 60-80 %` any | near-total I/O stall | the box is about to wedge: capture what can be captured |

The fsync canary (a new file: `open` → `write` → `fsync` → `unlink`, once a
minute, recording its latency) is the cheapest direct test of the **write** path
— the exact thing the 15:21 artifacts imply, which read-side PSI alone cannot
separate from the read streaming.

## 6. Anticipate (instrumentation, ranked)

1. **Monitor patch** (`tools/gpu_monitor.py`, deployed to `~/gpu_monitor.py` —
   the service restarts per sample, so a file replace arms it without a restart):
   - read `/proc/pressure/io` some/full avg10 into the sample line (`I:5.4/5.4%`)
     and the report — the field the history never had;
   - the fsync canary once a minute with its latency in the sample line;
   - `/proc/diskstats` for `nvme0n1`: read/write rate, await, aqu-sz;
   - a D-state task count.
2. **The memory-watch cron** (`3bc1a3c5`): add the §5 thresholds and the canary
   check to its prompt so the 7-minute watch reports `io ok` / `io WARN` / `io
   ALARM` alongside `mem ok`.
3. **`kernel.hung_task_timeout_secs` 120 → 60** (policy call): every measured
   I/O wait on this box is < 5 s, so a 60 s D-state is already abnormal; halves
   the zombie-to-vmcore time. The tripwire must be re-applied after reboots only
   via the sysctl file (it is persisted in `/etc/sysctl.d/70-crash-tripwires.conf`).

## 7. Prevent

**Shipped 2026-10-26** (commits `43360d4`, `50dd4e3`):

- **Prompt cap** `MAYA_MAX_PROMPT_TOKENS=49152` (3 × `--prefill 16384`): the
  50,511-token prompt class — 4 chunks, ~112 GB of staging — is refused with a
  400 before the engine reads a token.
- **Admission cooldown** `MAYA_ADMIT_COOLDOWN_S=4`: a release that leaves requests
  queued keeps the device idle 4 s before the next waiter's prefill starts — idle
  between the decode steady-state and the staging burst, on the exact boundary
  both prefill-adjacent freezes sat on.
- **Panic tripwires** (`hung_task_panic=1`, `softlockup_panic=1`) + the validated
  kdump chain (deliberate panic → 1.56 GB vmcore → auto-reboot in 76 s).
- **SBSA watchdog** `RuntimeWatchdogSec=30s` — armed, but it cannot catch a
  wedge that leaves PID 1 alive to keep petting; assume a human must power-cycle.

**Proposed on alarm** (policy, not yet automatic): the guard's io-ALARM path may
SIGTERM the engine to stop feeding the burst — this trades a serving session for
a clean recovery, and false-positives during a legitimate 3-chunk prefill are the
risk; default is snapshot + report, human decides.

**Do not**: restart the engine, or kill anything, on io pressure alone — the
decode band (§5 row 2) is the designed cost of streaming experts on a
unified-memory box.

## 8. Cross-references

- `.opencode/skills/gb10-io-pressure` — the triage procedure this doc backs.
- `.opencode/skills/gb10-hardlock-triage` — the freeze class; its §1 0-byte tell
  and §7 watchdog caveat are updated by `docs/2026-10-26-hardlock-1521-headroom40.md`.
- `.opencode/skills/dgx-spark-memory-forensics` — the *memory* class (PSI-memory,
  `F:`/`N:`, the estimator); this doc is its I/O sibling.
- `docs/MEMORY_WATCHDOGS_DEEP_DIVE.md` — the guard design the monitor patch extends.
- Engine telemetry for the same phenomenon: `glm stat: disk <n> reads/tok in
  <m> waits/tok, <ms>/tok` (decode) and `glm prefill: … copies <n> (<GB/s>)` (the burst).