---
name: dgx-spark-memory-forensics
description: Use when investigating memory state, device-free dips, WARN bands, or possible leaks on the DGX Spark GB10 — reading gpu_monitor sample lines and guard logs, bucketing device-free trends, computing the driver-memory estimator, deciding step-vs-leak, and naming a culprit process. Also covers the GB10 truths (MemAvailable lies, cudaMemGetInfo is the honest number) and what never to do during an investigation.
---

# Memory forensics on the DGX Spark (GB10)

## The two GB10 truths

1. **`MemAvailable` lies.** The CUDA pool IS system RAM; the driver's pages
   are booked nowhere `/proc/meminfo` can see. The engine can be ~1 GiB from
   `NV_ERR_NO_MEMORY` while MemAvailable still reads ~20 GiB. The honest
   number is **device-free via `cudaMemGetInfo`** — in the sample log it is
   the `F:` field. Trust order: `F:` and `N:` first, `A:` last.
2. **No OOM-killer rescue.** Pool exhaustion → `NV_ERR_NO_MEMORY` → the
   driver's error path deadlocks → hard lock, console silent, kernel watchdog
   disabled → power-cycle. That is why the guard kills *before* the floor.

## Reading the sample line

```
[ts] GB10 | U:% | T:°C | P:W | M:<compute MiB> | F:<device-free GiB> | A:<avail GiB> | N:<new nvrm lines> | proc: ... | guard:<tag>
```

- `F:` healthy ≥ ~4 GiB · WARN band 3–4 GiB · breach < 3.07 GiB (kill floor).
- `N:` > 0 = the driver refused an allocation since the previous sample.
- `guard:armed/pending/terming/draining/killed` = the confirmation chain.

Where to look: `~/gpu_monitor_log.txt` (samples), `~/gpu_monitor_guard.log`
(WARN / armed / kill decisions), `~/.gpu_monitor_guard_state` (current guard
state), `~/gpu_reports/` (per-sample reports, 7-day retention).

## Trend analysis: step vs leak

Bucket `F:` from the sample log into 30-min min/max:

```
grep -a -o '\[<date> [0-9:]*\].*F:[0-9.]*GiB' ~/gpu_monitor_log.txt \
 | sed 's/\[<date> //; s/\].*F:/ /; s/GiB//' \
 | awk -F'[ :.]' '{b=$1":"$2; if($2<30) b=b":00"; else b=b":30"; v=$4+$5/100;
                   if (!(b in min)||v<min[b]) min[b]=v; if(v>max[b]) max[b]=v}
                  END {for (b in min) printf "%s min=%.2f max=%.2f\n", b, min[b], max[b]}' | sort
```

- **Leak signature**: min drifting down bucket over bucket.
- **Step-held signature** (seen 2026-10-25 08:18): one clean drop, then flat
  ±0.15 GiB for hours — memory held by the driver/caching allocator, not a
  growing process; the memwatch lesson: "a permanent step up in `driver` with
  `free` flat is a request growing the CUDA caching allocator; that memory
  does not come back."
- **Transient dips** (drills, serving bursts): V-shaped, recover within
  minutes.

If flat: do NOT intervene — the guard's WARN band is the designed early
indicator; a step with margin above 3072 MiB is stable-held, and killing
something to "reclaim" it risks the serving engine for no gain.

## Naming a holder

- Driver-memory estimator (memwatch formula; the residual is memory that is
  neither free, cache, anon, slab nor page tables — i.e. through the NVIDIA
  driver, including the engine's CUDA pool):

  ```
  driver = MemTotal - MemFree - Buffers - Cached - AnonPages - Slab - PageTables - KernelStack
  ```

  Gotcha: in awk, `/proc/meminfo` `$1` carries the colon (`MemTotal:`) —
  strip it (`sub(":", "", $1)`) or every comparison silently fails and the
  residual prints 0. The engine's CUDA total (from compute-apps, e.g.
  86085 MiB) subtracted from `driver` gives "other driver/pinned" memory.
- `ps aux --sort=-rss` for the RSS ranking. The engine's ~11 GiB host RSS is
  by design (mmap'd weights — see `Mapped` in meminfo), not a leak.
- Compute-apps constant all day + MemAvailable flat + no RSS grower ⇒ the
  step is driver-side held memory, not a process to kill.

## Intervention rules

- Breach sustained (armed → confirmed → termed): the guard already acted;
  check `~/gpu_monitor_guard.log` for the victim and why-line before doing
  anything.
- A runaway build: it should have been under `safe-build.sh`; if it freezes
  (SIGSTOP group), it resumes on recovery — do not kill it manually unless
  frozen past `SAFE_FROZEN_MAX_S`.
- Never `pkill -f` from a tool shell (self-match); kill by exact PID.
- `sudo` needs a password — no unattended systemctl. Engine restart (last
  resort): `~/projects/maya/run-maya-maya-s-v2-iq2_xxs.sh`.

## Cross-references

Full design: `docs/MEMORY_WATCHDOGS_DEEP_DIVE.md` in the maya repo (and
`~/MEMORY_WATCHDOGS_DEEP_DIVE.md` + `.html`). Guard internals:
`tools/gpu_monitor.py` header block. Concurrency incident:
`docs/2026-10-25-guard-concurrency.md`.
