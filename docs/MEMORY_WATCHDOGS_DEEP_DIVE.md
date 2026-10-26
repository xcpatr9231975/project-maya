# Memory Watchdogs on the DGX Spark — Deep Dive

*Three independent guards, one recovery layer, one failure mode: on GB10 unified memory, exhaustion hangs the kernel instead of raising OOM.*

**Scope**: `tools/gpu_monitor.py` + `safe-build.sh` (project-maya, this box), `tools/hardlock-watchdog.sh` (the SBSA hardware-watchdog recovery) and `memwatch.sh` from [MiaAI Lab's Qwen3.8-Flash-Next-Single-DGX-Spark](https://github.com/MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark/blob/main/files/memwatch.sh) (the vLLM deployment). Written 2026-10-25, re-verified through commits `b0304ab` (concurrency hardening) and `360107e` (journal watch) and after reading the upstream MiaAI-Lab source.

---

## 0. Why all three exist

The DGX Spark GB10 has *unified memory*: the CUDA device pool **is** system RAM. Two consequences drive every design decision below:

1. **`MemAvailable` lies.** A process can be ~1 GiB from `NV_ERR_NO_MEMORY` while `MemAvailable` still reads a healthy ~20 GiB — the driver's pool drains through a channel `/proc/meminfo` books under other names. The only honest probe is `cudaMemGetInfo` (device-free).
2. **No OOM-killer rescue.** When the pool runs out, the driver returns `NV_ERR_NO_MEMORY` and its error path deadlocks: the box hard-locks with nothing on the console. The kernel's hard-lock detector is disabled here ("NMI not fully supported"), so the kernel never panics and never reboots; historically a human power-cycled the box. Nobody reaps the offender for you; something must kill it *before* the floor is crossed. Recovery is a separate layer: the SoC's SBSA Generic Watchdog (`tools/hardlock-watchdog.sh`, layer 4) resets the box in hardware when userspace stops pinging it.

So the box runs defense-in-depth:

| Layer | Tool | Cadence | Acts on | Reaction |
|---|---|---|---|---|
| 1 — build wrapper | `safe-build.sh` | every 5 s (device probe 15 s) | the build's own process group | SIGSTOP (freeze) → resume / abort |
| 2 — system guard | `gpu_monitor.py` | ~10 s (systemd timer + `Restart=always`) | any process on the box | SIGTERM → SIGKILL after 30 s grace |
| 3 — container watchdog | `memwatch.sh` (MiaAI Lab) | every 1 s, debounced | the vLLM container | archive + marker + alert + `docker stop` |
| 4 — recovery | `hardlock-watchdog.sh` (SBSA GWDT) | 30 s ping, two-stage | the whole box | hardware reset at 2x timeout |

The layering is deliberate: **safe-build's freeze floors sit above the guard's kill floors** (device-free 4096 MiB vs 3072 MiB; PSI 5.0 % on both, but safe-build polls at 5 s vs the guard's ~10 s two-strike cadence), so a runaway build freezes itself before the guard ever has to pick a victim — and the guard's victim pick is designed so that when it *does* fire, it kills the culprit, not the engine.

---

## 1. `tools/gpu_monitor.py` — the system guard

*Current at commit `b0304ab` (fork/main + private/main); deployed copy byte-identical.*

### 1.1 Architecture: one sample per invocation

The unit is not a daemon loop; systemd re-invokes `main()` every ~10 s (`Restart=always` + `RestartSec=10`). Each invocation is one *sample*. Anything spanning samples lives in files:

- `~/.gpu_monitor_guard_state` — `armed:<ts>` (first breach, awaiting confirmation) or `termed:<pid>:<ts>` (SIGTERM sent, awaiting exit/grace)
- `~/.gpu_monitor_guard_rss` — the pid→RSS baseline for growth attribution
- `~/.gpu_monitor_guard_lock` — the whole-run `flock` (below)
- `~/.gpu_monitor_guard_warned` — warning-band throttle state

This gives crash-free restart semantics at the cost of two-sample confirmation for every trigger.

### 1.2 Trigger order

Evaluated per sample, in the order they matter:

1. **device-free** < `DEVICE_FREE_FLOOR_MIB` (3072 MiB) — via `cudaMemGetInfo`; *the only signal that saw the failure coming* (measured: ~0.3 s and ~170 MiB of device memory per probe call).
2. **MemAvailable** < `MEM_FLOOR_GIB` (6 GiB) — OS-side exhaustion.
3. **GPU compute-app total** > `GPU_CEIL_MIB` (112640 MiB) — backstop when the CUDA probe is unavailable.
4. **PSI memory full avg10** ≥ 5.0 % for two consecutive samples — the kernel saying allocation is already stalling.
5. **swap used** ≥ 8 GiB — the pre-lock thrash signature.
6. **`NV_ERR_NO_MEMORY` lines new in the kernel log** since the previous sample (commit `360107e`) — the driver literally refusing an allocation. No thresholds, no CUDA probe; works even when `cudaMemGetInfo` is unavailable. Technique credited to MiaAI Lab's `memwatch.sh` in `NOTICE` (AGPL-3.0-or-later; reimplemented from scratch, no code taken).

Any breach arms; a second breach within `GUARD_CONSEC_S` (30 s) confirms and picks a victim.

### 1.3 Victim selection — the 2026-10-25 design

```
high_gpu?                     → top GPU consumer (holder of that memory, by definition)
low_free + OS_VICTIM_BY_RSS?  → RSS-growth attribution, else top GPU consumer
OS-side only (avail/psi/swap) → biggest-RSS process (_os_victim), else GPU pick
```

**RSS-growth attribution** (commit `64dd854`) fixes the GB10 blind spot: a process holding *no* GPU memory (an unwrapped build) can drive device-free below the floor, and the old pick killed the engine for it — while the engine's own CUDA allocations never show in RSS, so it could never win an RSS pick either.

Mechanics:

- Every **healthy** sample writes a `pid→(RSS, starttime)` baseline (`~/.gpu_monitor_guard_rss`) — starttime from `/proc/<pid>/stat` field 22 binds each entry to that exact process, so a pid **reused** within the 300 s window counts as a fresh process (full RSS = growth) instead of inheriting the old tenant's RSS, and a pid with no `/proc` stat any more is skipped (commit `cbef66d`); the legacy two-token format parses as a same-process delta until the next healthy rewrite.
- On a device-free breach, the victim is the process whose RSS grew the most since the baseline — *new processes count as full growth* (`base.get(pid, 0)`), so a fresh build is caught on its first confirming sample.
- Growth must exceed `RSS_GROW_MIN_KIB` (256 MiB) — page-noise filter.
- Baselines older than `RSS_BASE_MAX_AGE_S` (300 s) are discarded rather than mis-attributed.
- The baseline is **not rewritten while a breach is active** (`_REBASE_OK` gates the rebase to healthy samples only), so growth accumulates across the armed→confirm window.

### 1.4 Kill path

```
confirm → victim picked → state termed:<pid>:<ts> → SIGTERM
next samples: alive after GUARD_GRACE_S (30 s) → SIGKILL → "guard:killed"
              exited (zombie-aware via /proc/<pid>/stat 'Z') → state cleared
```

`GPU_MONITOR_PROTECT` (comma list of pids or /proc names) always skips a victim.

### 1.5 Concurrency hardening — commit `b0304ab` (the 05:16:28 incident)

Two-sample confirmation **collapsed under parallel invocations**, seen live on 2026-10-25 05:16:28: the systemd service plus a manual run from an SSH session both logged `armed` *in the same second* — the second invocation read the first's freshly-written state as its own confirming sample and SIGTERMed on a single dip. In that drill the victim was a test dummy (a `sleep` with synthetic breach values), but on a genuine one-sample dip an innocent process would have taken the kill. The defect, precisely: invocation B reads `armed:<ts>` written by A moments earlier, computes `now − ts ≈ 0 ≤ GUARD_CONSEC_S`, and confirms on B's *first* sighting — the two-sample rule becomes a one-sample kill. State-file and baseline write races compound it (B's `termed:<pid>` can overwrite A's `armed` mid-decision).

Two independent guards, either sufficient alone, per `docs/2026-10-25-guard-concurrency.md`:

1. **Whole-run exclusive `flock`** (`.gpu_monitor_guard_lock`, `LOCK_EX|LOCK_NB`, held across `memory_guard` *and* the baseline write): a concurrent invocation skips sampling and guard logic entirely (exit 0, logs nothing) — the holder is the live sampler for that instant. Every read-modify-write cycle is single-threaded by construction. The kernel drops the lock when the fd closes or the holder dies (even SIGKILLed), so no crashed run can wedge the next sample. If the lockfile cannot even be opened, the run proceeds *unlocked rather than blind*.
2. **Armed-age floor** — `GPU_MONITOR_MIN_CONFIRM_AGE_S` (default 8 s, ~80 % of the 10 s cadence): a confirming sample must observe an armed state written by an **earlier** invocation; a fresher one logs `guard:pending` and keeps the original timestamp untouched, so the honest first sighting survives even without the lock (cron with a different cadence, or a future lock removal).

Verified by the sandbox suite, now **38 checks**, including two threads racing `main()`: exactly one sample line, no kill, and a later invocation confirms normally.

### 1.6 The journal watch — commit `360107e` (cross-pollination from memwatch.sh)

memwatch.sh's best signal — `NV_ERR_NO_MEMORY` delta counting — is now in the guard too, reimplemented from scratch and credited in `NOTICE`. The port is not a copy: the one-shot-process constraint demanded a different mechanism.

| memwatch.sh (daemon) | gpu_monitor.py (one shot per sample) |
|---|---|
| `nvrm_since` datetime string advanced in-process | epoch cursor in `~/.gpu_monitor_guard_nvrm` |
| fixed cadence windows | adjacent `[last, now]` epoch windows, one per sample |
| counts via `journalctl -k --since/--until` | same query, epoch form `--since=@last --until=@now` |

Design points worth keeping:

- **First run initialises only** — writes the cursor, returns 0. There is no lower bound on the first window, so counting then could replay an *old* flood as a fresh breach; init-only avoids it.
- **Boundary double-count is accepted, not engineered away.** `journalctl --since/--until` are inclusive at the boundary instant, so a line stamped exactly at the boundary counts in both adjacent windows. Documented as harmless for a trigger — preferred over a `last + ε` window that could silently drop a line arriving within the epsilon (the same lesson memwatch learned with its 12 s/10 s overlap).
- **`pool_breach = low_free or nvrm_hit`** — an NVRM-only breach (device probe unavailable, say) routes through the *growth-attribution* victim pick, because on GB10 the refuser may hold no GPU memory. high_gpu stays a separate first branch (victim = top GPU consumer by definition).
- **Two-sample confirmation applies unchanged**: one transient refusal the engine retried past recovers; persistent refusals confirm and kill.
- **Journal unavailable → `None` → inert** (no false kills from a broken `journalctl`); timeout is 10 s.
- **The cursor advances only after a good read** (commit `e1f8f4b`): a timed-out or failed `journalctl` leaves it at `last`, so the next sample re-covers the whole `[last, next]` window — no lines lost, and a long journal outage counts its lines once, on the first sample that succeeds.
- Sample lines and reports now carry `N:<count>` every sample.

Minor accepted cost resolved by `e1f8f4b`: the cursor now advances only after a successful query, so a timed-out window is re-covered by the next sample instead of losing its lines.

### 1.7 Verified behavior (commits `64dd854` + `b0304ab` + `360107e` + `cbef66d` + `e1f8f4b`)

9 offline victim-pick tests (healthy rebase, round-trip, arm/no-rebase, below-min fallback, stale baseline, self-exclusion, full arm→confirm→SIGTERM→state-clear cycle, name-protect, empty baseline), the 38-check concurrency suite including two threads racing `main()`, the journal-watch suite (**46 checks** total, including first-run cursor init and inert-on-unavailable paths), a **5-check cursor-discipline suite** (first-run init / success advances / timeout holds / failure holds / clock-skew no-query), and an **11-check starttime-baseline suite** (same-starttime delta, reused-pid full-RSS attribution, dead-pid skip, unlisted-pid growth, legacy-format tolerance, below-min, real-child round-trip, 3-token format, end-to-end fresh-pid-after-death, self/1/2 exclusion) — which caught a real parse bug (a legacy `pid:rss` token yielded `int("")` → silently empty baseline) before it shipped. The baseline and cursor suites now live in-repo and run in CI on every push: `tools/test_gpu_monitor_baseline.py`, `tools/test_gpu_monitor_nvrm.py` (commit `67745c3`).

Residual risks: pid reuse within the 300 s baseline window could mis-attribute (falls back to the old GPU pick); a breach from many processes each growing <256 MiB falls back to the engine kill.

### 1.8 The stall guard — the failure no floor ever saw (2026-10-25)

The four hard locks the box actually died from (2026-10-24 19:40, 2026-10-24 21:08, 2026-10-25 17:11, 2026-10-25 17:49) had nothing to do with any trigger above: device-free was 7–15 GiB against a 3.07 GiB floor, `N:` was 0, no guard state was ever armed, and the kernel log ended mid-sentence with no `NV_ERR_NO_MEMORY`, no Xid and no shutdown sequence. What the samples *do* show is the GPU at 94–95 % and 84–85 °C in the last line before the box went silent while memory was still healthy. On GB10 device memory *is* system RAM and the kernel watchdog is disabled ("NMI not fully supported"), so a GPU-side fault takes the whole box; no userspace watchdog can save it, but a hang that is still only the *engine* (host-side deadlock, stuck copy lane) is recoverable, and in both cases the evidence worth having is the engine's own last output, which dies with the box.

So the guard gained a second, independent state machine:

- **What it watches:** the file the engine actually writes to — `/proc/<engine>/fd/2`, discovered per sample (fd 1 is a pipe to the server). No hardcoded path, so it follows whichever run is live.
- **When it fires:** that file silent for `GPU_MONITOR_STALL_QUIET_S` (90 s) *while* the GPU is at ≥ `GPU_MONITOR_STALL_UTIL_PCT` (90 %). The utilization gate is the false-positive control: an idle engine is silent by design, and the longest legitimate silent stretch is one 32768-token prefill chunk (~46 s, chunk stage line silenced — normally the launcher prints a progress line every 10 s).
- **What it does:** snapshots `~/gpu_reports/stall_<ts>.txt` (why, util/temp/device-free, `/proc/<pid>/status`, the engine's last 40 lines), SIGTERMs the engine, escalates to SIGKILL after `GPU_MONITOR_STALL_GRACE_S` (30 s). The launcher restarts the engine on the next request, so a false positive costs one restart.
- **Always on, stall or not:** a rolling copy of the engine's tail in `~/gpu_engine_tail.txt`, rewritten every sample. A hard lock costs the engine its unflushed stdio buffer and the kernel leaves the lost region as NUL padding inside the log (1806 bytes measured at the 17:11 lock), so this is the only post-mortem record of what the model was doing.
- **Its own state file** (`~/.gpu_monitor_stall_state`, `stall-termed:<pid>:<starttime>:<ts>`) — merging it into the memory guard's state would have the "unknown guard state" branch discard it. No two-sample rule: the mtime age *is* the confirmation. A stall kill clears a stale memory-guard `armed:` state so it cannot frame another process, and leaves an in-flight `termed:` alone.

Suite: `tools/test_gpu_monitor_stall.py` (18 checks — fd discovery, NUL-stripping tail, the idle/fresh/no-proc/unwatchable/disabled gates, the full term→drain→SIGKILL→restart cycle, stale-arm clearing, the rolling tail). `GPU_MONITOR_STALL_GUARD=0` disarms it.

The 17:49 lock exposed the stall guard's limit: its trigger is *silence*, and the engine was still emitting `thinking:` lines at 14.7 tok/s in the 17:49:23 sample — one second before the box died. A lock that kills the machine in the same sample as the engine's last line gives a 90 s silence gate no runway. The answer to that case is not another userspace check but the hardware timer: `tools/hardlock-watchdog.sh` arms the SoC's SBSA Generic Watchdog through systemd (`RuntimeWatchdogSec=30s`; WS0 panic then WS1 hardware reset) and sets `kernel.panic=15`, so a stalled PID 1 ends in a reset even when the kernel cannot run the panic path. That is recovery, not prevention — worst case ~2x the timeout.

---

## 2. `safe-build.sh` — the build watchdog

*Current at commits `b4c59af` + `2a2447d`.*

### 2.1 Contract

Wrap any command so it **can never push the box to the guard's kill floors**, and never dies unwatched:

```
./safe-build.sh                      # ninja -C build -j4
./safe-build.sh -C build-tests       # ninja -C build-tests -j4
./safe-build.sh -j2 -C build         # ninja -C build -j2
./safe-build.sh -- cmake --build ... # any command under the watchdog
```

Exit codes: `0` ok · `124` watchdog aborted · `125` pre-flight refused · `126/127` command not executable/not found · else the command's own rc.

### 2.2 The four stages

0. **Pre-flight** — refuse (rc 125) when MemAvailable < 8 GiB or device-free < 4096 MiB *before* starting.
1. **Isolation** — `setsid nice -n 10 ionice -c2 -n7`: the build becomes its own session leader (`pgid == pid`), every compiler it spawns joins that group → **one signal stops all of it**. Niced for CPU, ioniced for I/O so the engine's serving latency is protected even before any freeze.
2. **Watchdog loop** (every `SAFE_POLL_S`=5 s) — four freeze triggers, any one suffices:
   - MemAvailable < 8 GiB
   - swap used ≥ 4 GiB
   - PSI memory full avg10 ≥ 5.0 % for two consecutive samples (`PSI_STRIKES`)
   - device-free < 4096 MiB — probed every `SAFE_DEVICE_PROBE_EVERY_S`=15 s via the same `cudaMemGetInfo` trick the guard uses
   → breach: `kill -STOP -$PGID` (the whole group freezes; the box gets memory back since stopped build pages reclaim).
3. **Escalation** — frozen ≥ `SAFE_FROZEN_MAX_S` (300 s), or while frozen MemAvailable sinks below the abort floor (4 GiB): SIGTERM the group, 2 s, SIGCONT+SIGKILL, exit 124. Recovered for a check while frozen → SIGCONT, resume.

### 2.3 Signal handling (fixed 2026-10-25, commit `b4c59af`)

- `INT` trapped → exit 130 (the old handler couldn't stop the loop: the build kept running after Ctrl-C).
- `TERM` trapped → TERM the group, 2 s, CONT+KILL, exit 124 (an untrapped TERM left the build running unwatched).
- `EXIT` → cleanup: CONT the group either way, so a finished/killed build never stays SIGSTOPped.

### 2.4 The floor-ordering invariant

```
safe-build device-free freeze 4096 MiB  >  gpu_monitor kill 3072 MiB
safe-build PSI strike at 5.0 %, polls 5 s  → strike in ~5–10 s
gpu_monitor PSI kill at 5.0 %, samples ~10 s → confirm ~20 s
```

So on any build-caused breach, safe-build freezes first and the guard never fires. The 2026-10-25 drill proved the device probe had to be 15 s not 60 s (commit `2a2447d`): a -j4 compiler ramp crossed the guard's 3072 MiB floor in ~40 s with a 2 GB/s allocator — *inside* one 60 s probe window.

### 2.5 Env knobs

`SAFE_J` (4) · `SAFE_DIR` (repo `build/`) · `SAFE_POLL_S` (5) · `SAFE_FLOOR_GIB` (8) · `SAFE_ABORT_GIB` (4) · `SAFE_SWAP_FLOOR_GIB` (4) · `SAFE_PSI_FULL` (5.0) · `SAFE_DEVICE_FLOOR_MIB` (4096) · `SAFE_DEVICE_PROBE_EVERY_S` (15) · `SAFE_FROZEN_MAX_S` (300) · `SAFE_NO_PREFLIGHT` (0).

---

## 3. `memwatch.sh` — the container watchdog (MiaAI Lab)

*Source of truth: [upstream `files/memwatch.sh`](https://github.com/MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark/blob/main/files/memwatch.sh), AGPL-3.0, MiaAI Lab. Same box, different deployment: it protects the host from the vLLM container by stopping the container. The local copy in `~/qwen38-flash-next-vllm/files/` runs *tighter* floors than upstream but is missing upstream's newer features — see §3.7.*

### 3.1 Philosophy: second line of defence

Behind `start.sh`'s `HOST_RESERVE_GIB` budget — a userspace poller cannot catch a GiB/s collapse alone. On unified memory "exhausted pool" means *kernel hang*, so its remedy is to stop the container (`docker stop -t 30`, i.e. SIGTERM; a SIGKILL leaks the container's POSIX shm onto the host's `/dev/shm` until reboot because of `--ipc host`), falling back to `docker kill`.

### 3.2 Two independent triggers, each debounced

Every 1 s sample; a trigger must hold for `CONSEC` **consecutive** samples — a lone excursion is logged and resets the counter:

| Trigger | Upstream default | Local copy | Rationale |
|---|---|---|---|
| MemAvailable < `MIN_GIB` | 6 GiB (arg 2) | **3 GiB** | page cache gone; PLE lookups about to hit NVMe |
| MemFree < `MEMWATCH_MIN_FREE_GIB` **while** MemAvailable < `FREE_GATE_GIB` (10 GiB) | 2 GiB | **1 GiB** | the driver refuses allocations (NV_ERR_NO_MEMORY) at MemFree ≈ 3 GiB while MemAvailable still reads 6+ — the avail floor alone reacts late |

The MemFree gate is the subtle part, with measured evidence in the header (2026-09-05 00:49, during weight loading): MemFree 0.9 GiB, MemAvailable 32 GiB, **zero NVRM errors**. With stock kernel watermarks MemFree legitimately falls to the ~170 MB low watermark whenever the page cache is full of reclaimable data — *free pages backed by reclaimable cache are not what the driver runs out of; free pages with no cache left to reclaim are.* Hence the AND-gate.

### 3.3 The earliest signal: NV_ERR delta counting

Every 10 s it counts `NV_ERR_NO_MEMORY` lines the kernel log gained **since the previous check** (`journalctl -k --since/--until`), logging the delta and a running total. Two measured lessons live in the comments:

- A fixed 12 s window every 10 s double-counted lines in the 2 s overlap (6 counted for 5 logged, 2026-09-05 09:00) → now advancing since/until windows.
- `journalctl --since` is inclusive at second granularity → the boundary second is excluded on the next pass by advancing `nvrm_since` past each check.

### 3.4 Leak-trend detector (upstream-only, §4.3 of their review)

The technique the user pointed at: a permanent step-up in the **`driver`** estimator signals the CUDA caching allocator growing — `driver = MemTotal − MemFree − Buffers − Cached − AnonPages − Slab − PageTables − KernelStack` — memory that is neither free, page cache, anon, slab nor page tables, i.e. taken through the NVIDIA driver (GPU allocations, pinned host buffers). Upstream turns that into a *slow leak* detector:

- Baseline window: only after `TREND_WARMUP_S` (900 s ≈ 15 min — **the weight-load ramp grows `driver` immediately, so a min including that ramp would make every cold start look like a leak**), over the next `TREND_BASELINE_S` (600 s) take the **minimum** `driver` seen, then freeze it.
- Flag when `driver − baseline ≥ TREND_GIB` (4 GiB), **at most once per day** (`trend_logged_day`): *"The 2–3 GiB per-request growth is accumulating; scheduled relaunch is the response."*

So the design answer to a slow leak here is not a kill — the leak is the allocator, and the remedy is a scheduled relaunch — but the detector tells you *when* the relaunch is needed. Env knobs: `MEMWATCH_TREND_GIB` (4), `MEMWATCH_TREND_WARMUP_S` (900), `MEMWATCH_TREND_BASELINE_S` (600).

### 3.5 Emergency-stop marker + alert hook

Before stopping, `stop_container` appends `WATCHDOG EMERGENCY STOP <reason>` to the live log — the supervisor distinguishes an emergency stop from a clean `stop.sh` because **only the emergency path emits it** (their review §4.1); it goes into the log *before* the final archive copy so both carry it. Then, after the stop, an optional `scripts/alert.sh "memwatch emergency stop: $1"` fires (failure must not change control flow, §4.6). Archival order: container log tail (3000 lines) + watchdog log → `logs/archive/<container>-<ts>-*.log`, then `docker stop -t 30` → `docker kill`, then exit 2 with the NVRM total since start.

Adopted into the local copy on 2026-10-25 (`scripts/alert.sh` created alongside, keeping upstream's silent-safe posture: `ALERT_WEBHOOK` unset in this deployment's `.env`, so alerts log to `logs/alert.log` and exit 0 until a webhook is configured).

### 3.6 The diagnostic heartbeat

Every 5 s (and every sample within 1 GiB of either floor) it logs the meminfo breakdown plus the container's own cgroup `memory.current` (`/sys/fs/cgroup/system.slice/docker-<id>.scope/`).

### 3.7 The weight-load ramp, as actually logged (2026-10-21)

`logs/memwatch-vllm-fn-tp1.log` shows exactly why the estimator exists — 45 s of container ramp:

```
05:48:41  avail=106701MiB free=101426MiB container= 5032MiB driver= 2712MiB
05:48:46  avail= 60421MiB free= 55117MiB container= 6517MiB driver=47322MiB   ← weights land
05:48:51  avail= 30344MiB free= 22519MiB container= 9093MiB driver=76948MiB
05:49:17  avail= 29630MiB free=  2205MiB container=27000MiB driver=76932MiB   ← cache eats free
05:49:27  avail= 28447MiB free=   896MiB container=31070MiB driver=76859MiB   ← MemFree at floor,
                                                                              avail still 28 GiB
```

MemFree collapses ~100× (101 GiB → 0.9 GiB) while MemAvailable stays at ~28 GiB — the exact signature the MemFree gate exists for. `driver` steps 2.7 → 77 GiB permanently: the CUDA caching allocator, never returned.

### 3.8 Local copy vs upstream — divergence summary

| | Upstream main | Local `~/qwen38-flash-next-vllm/files/` |
|---|---|---|
| MemAvailable floor / debounce | 6 GiB / 5 consecutive | **3 GiB / 15** (tightened) |
| MemFree floor | 2 GiB | **1 GiB** (tightened) |
| Leak-trend detector | yes | yes (synced 2026-10-25) |
| Emergency-stop marker | yes | yes (synced 2026-10-25) |
| `scripts/alert.sh` hook | yes | yes (created 2026-10-25, no-op until `ALERT_WEBHOOK` is set) |
| MemFree gate condition | `avail < FREE_GATE` | `avail < FREE_GATE` (pre-sync copy gated on `< MIN_GIB` — found and fixed in the sync) |

Upstream's header prose matches its code, and since the 2026-10-25 sync the local header does too (the pre-sync copy said 6/2 while its code ran 3/1 — trust the code, not the header; preserved as `files/memwatch.sh.pre-sync-20261025`). The floor tightening stays a deliberate response to the 2026-09-05 evidence and is now documented in the header itself.

---

## 4. Cross-cutting comparison

| Property | gpu_monitor.py | safe-build.sh | memwatch.sh |
|---|---|---|---|
| Scope | whole box | one wrapped command | one container |
| Poll | ~10 s / sample | 5 s (device 15 s) | 1 s |
| Debounce | armed → confirm, ≥8 s apart, ≤30 s | PSI ×2; others immediate | 5–15 consecutive |
| Honest mem probe | `cudaMemGetInfo` + kernel-log NVRM watch | `cudaMemGetInfo` (borrowed) | none (meminfo + journal) |
| Victim | RSS-growth → GPU pick → RSS pick | its own process group | the container |
| Kill | TERM → 30 s → KILL | STOP → resume/TERM/KILL | docker stop -t 30 → kill |
| Concurrency | whole-run flock + armed-age floor | single instance by design | single instance |
| State | files (state, RSS, lock, warn) | in-process vars | in-process counters |
| Failure posture | survives restarts (systemd) | traps INT/TERM/EXIT | archives + marker + alert before acting |

### Shared lessons, independent rediscoveries

1. **`cudaMemGetInfo` is the only honest *numeric* floor on GB10** — gpu_monitor and safe-build probe it; memwatch compensates with the MemFree-gate + cgroup current because a docker container gets neither. The **journal watch** (`NV_ERR_NO_MEMORY` delta counting) is the honest *evidential* signal, needs no thresholds and survives a dead CUDA probe — discovered by memwatch, now ported into gpu_monitor (`360107e`, credited in `NOTICE`, AGPL-3.0-or-later).
2. **Two-sample / debounced confirmation everywhere** — memwatch: 5–15 consecutive; gpu_monitor: armed→confirm, now hardened to ≥8 s apart across invocations; safe-build: PSI ×2.
3. **Kill the culprit, not the biggest name** — RSS-growth attribution (gpu_monitor), own-group freezing (safe-build), container scoping (memwatch).
4. **Grace before SIGKILL** — 30 s everywhere; SIGKILL leaves residue (leaked shm for memwatch, lost ninja state for safe-build).
5. **Concurrency defeats debounce** — memwatch guards it by scoping to one container and one instance; gpu_monitor needed explicit `flock` + armed-age hardening after the 05:16:28 incident.
6. **Slow leaks are a scheduling problem, not a kill problem** — memwatch's leak-trend says "scheduled relaunch is the response"; gpu_monitor's RSS-growth attribution handles the fast case and leaves the slow one to the WARN band.

### Why the qwen38FN deployment never reaches its watchdog — design-level avoidance (2026-10-25)

The 2026-10-25 pressure drills against maya-S (four confirmed kills, all external `python3`/`sleep` growers, `F:` to within ~360 MiB of the floor) raise the question: why does the vLLM deployment (`vllm-fn-tp1`, Qwen3.8-Flash-Next, :8009) never hit the `NV_ERR_NO_MEMORY` hard-lock at all? Not because memwatch is better — because the *allocator itself is capped*. Three layers, none of which maya-S currently has:

1. **Static, pre-allocated pool.** vLLM reserves its entire KV-cache pool at startup (`GPU_MEMORY_UTILIZATION=0.78`, budget ≈ 93.4 GiB). During serving it never allocates beyond the reservation; requests that do not fit are **queued or preempted**, never OOM'd. A static pool cannot win an "allocation growth" contest against the driver floor. (maya-S is the same shape — 86085 MiB flat all day — which is why the guard never named the engine as victim.)
2. **The hard cgroup ceiling.** The container runs under `memory.max = budget + HOST_SLACK_GIB` (2 GiB) ≤ the deployment ceiling (pool 119.7 − OS_RESERVE 16 = 103 GiB). An over-allocation *fails inside the container* instead of exhausting the driver. On GB10, device memory **is** system RAM, so a cgroup cap on the allocator is a direct bound on driver pressure — this is the actual crash-avoider, not the watchdog.
3. **Margin tuning + watchdog as backstop only.** GMU 0.78 instead of 0.80 deliberately adds ~2.4 GiB of host margin so serving-time MemAvailable stays above memwatch's `FREE_GATE` (10 GiB); the 2026-09-08 history shows kills firing at avail 9.4–10.2 GiB when the margin was thinner. The watchdog's job is the allocators *outside* the engine — it never has to fire for qwen's own allocations.

**The same recipe applied to maya-S**: put the engine under a systemd slice with `memory.max` ≈ pool + slack so a runaway engine hits its own failed allocation instead of deadlocking the driver (untested — worst case is an engine crash + ~10 min restart, still far cheaper than a power-cycle); shrink the context/kv ceiling so `F:` rests at 8+ GiB instead of 4.2 (the GMU-margin move); make the engine refuse or queue new requests below a headroom threshold (vLLM's preemption semantics). Then the guard demotes itself to what memwatch already is for qwen: a backstop for *external* allocators, not the primary defense.

### Recovery after the lock — the SBSA hardware watchdog (2026-10-25)

Every guard above assumes the kernel is still scheduling. The 17:49 lock broke that assumption: the engine was still producing tokens and the journal simply ended. Userspace cannot recover that box, but the SoC can. The board exposes `/dev/watchdog0`, the SBSA Generic Watchdog (`sbsa_gwdt`), loaded in two-stage mode (`action=1`): at the first timeout (WS0) the driver panics; one more timeout later (WS1) the hardware asserts a reset. WS1 is a SoC line, not a Linux code path, so it fires even when the kernel is wedged with interrupts disabled.

- **Arming it:** systemd 255 opens the device when `RuntimeWatchdogSec` is set and pings it from PID 1. When the box hard-locks, the pings stop and the hardware timer runs out.
- **The panic stage:** `kernel.panic=15` makes WS0's panic reboot after 15 s instead of hanging at the default `panic=0`. If the panic cannot run, WS1 resets at ~2x the timeout anyway (30 s default → worst case ~60 s).
- **What it is not:** this is recovery, not prevention. In-flight generation is lost; the launcher restarts the engine. It does not replace the memory floors or the stall guard — it ends the manual power-cycle when they lose.
- **Tool:** `tools/hardlock-watchdog.sh status` (read-only, no root), `sudo tools/hardlock-watchdog.sh apply` (writes the systemd drop-in and sysctl file, `systemctl daemon-reexec`, verifies `state=active`), `sudo tools/hardlock-watchdog.sh revert`. The kernel's own hard-lock detector is still disabled ("NMI not fully supported") and is not what this uses.

### Gaps / future work

- ~~gpu_monitor: pid-reuse mis-attribution window~~ — closed by `cbef66d` (starttime-bound baseline entries).
- ~~gpu_monitor: journal-watch cursor advances before the query, so a timed-out `journalctl` (10 s) loses that window's lines permanently~~ — closed by `e1f8f4b` (cursor advances only after a good read; failed windows re-covered next sample).
- gpu_monitor: baseline and state files carry no writer identity — the flock + age floor cover it, but a lockfile owned by a *stale* NFS/other-filesystem context is untested.
- safe-build's `device_free_mib` spawns python per probe; fine at 15 s, wasteful if ever polled faster.
- ~~The vLLM deployment's local `memwatch.sh` is behind upstream main (leak trend, marker, alert)~~ — closed 2026-10-25: leak-trend detector, `WATCHDOG EMERGENCY STOP` marker, `scripts/alert.sh` hook and the MemFree-gate semantics adopted from upstream; tightened 3/1/15 floors kept and now documented in the header.
- The engine itself is ~10.6 GiB RSS by design — a genuine engine leak would self-attribute and get killed; correct trade, but remember it.
- maya-S runs uncapped — no container, no cgroup, so every process on the box can push the driver toward its floor and the guard is the *only* defense. The qwen38FN layers above (cgroup ceiling, GMU margin, queue/preempt instead of grow) are the candidate design fix; adopting any of them is future work.

---

*Companion sources: maya repo commits `dd37817`, `b4c59af`, `64dd854`, `2a2447d`, `b0304ab`, `360107e`, `cbef66d`, `e1f8f4b`, `67745c3` (+ `docs/2026-10-25-guard-concurrency.md`, `NOTICE`, `tools/hardlock-watchdog.sh`); MiaAI-Lab upstream `files/memwatch.sh` + `scripts/alert.sh`; local `logs/memwatch-vllm-fn-tp1.log`; memwatch sync of 2026-10-25 (leak-trend + marker + alert + gate fix, pre-sync preserved as `files/memwatch.sh.pre-sync-20261025`); sandbox suites of 2026-10-25 (9 victim-pick + 38 concurrency + 5 cursor-discipline + 11 starttime-baseline checks); qwen38FN deployment config (`~/qwen-autostart.sh`, GMU 0.78 / HOST_SLACK_GIB 2 / FREE_GATE 10) and the 2026-10-25 maya-S pressure drills (`~/gpu_monitor_guard.log`: four kills, all external growers); the 2026-10-25 17:49 hard lock (`~/gpu_engine_tail.txt`, `~/gpu_monitor_log.txt`, `journalctl -b -1`) and the SBSA Generic Watchdog driver (`drivers/watchdog/sbsa_gwdt.c`, ARM DEN0029B).*
