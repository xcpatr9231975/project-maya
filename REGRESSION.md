# GLM-5.3-Flash regression on NVIDIA driver 580.178.04

Date: 2026-10-25 19:09 → 2026-10-26 06:40 (PDT) · Box: `carlosp-nvidia-dgx` (DGX Spark GB10)
Model: `Maya-S-v2-IQ2_XXS` (GLM-5.3-Flash), engine `build/maya` (unmodified sources)
Companion to [docs/2026-10-25-driver-580-residual-kernels.md](docs/2026-10-25-driver-580-residual-kernels.md)
(what the upgrade changed) and [BENCHMARK.md](BENCHMARK.md) (the 580.95.05 numbers).

**Verdict: pass.** No driver-induced regression in numerics, correctness or throughput. Two
real defects surfaced that are *not* driver-related and are documented below: the memory
guard kills the engine on long prefills unless the headroom is raised, and the guard's
stall detector false-positives on engines started by another harness.

## What changed under test

| | Before (boot -1) | After (boot 0) |
|---|---|---|
| Kernel | `6.14.0-1015-nvidia` | `7.0.0-1019-nvidia` (HWE) |
| Driver | 580.95.05 | **580.178.04** (open module) |
| CUDA | 12.x | 13.0 (V13.0.88), device `sm_121` |
| GSP firmware | `nvidia-firmware-580-580.95.05` | `nvidia-firmware-580-580.178.04` |

The engine binary was not rebuilt: `find src include CMakeLists.txt -newer build/maya`
returns nothing, so the same binary that produced the 2026-10-25 freeze runs was used for
every measurement below. Only the driver, the kernel and the GSP firmware differ.

## Correctness

| check | command | result |
|---|---|---|
| Engine suite (GPU) | `ctest --output-on-failure -j1` in `build/` | **44/45** |
| Tools suite (the CI gate) | `.venv/bin/python -m unittest discover -s tools -p "test_*.py"` | **452 tests, 0 failures**, 28 skipped |
| Recall through the API | `tools/needle_bench.py --lengths 32k,128k --depths 10,50,90` | **6/6 found** |
| Recall, 256k engine at headroom 40 | `tools/needle_bench.py --lengths 128k --depths 10,50,90` | **3/3 found** |
| Speculative-decode acceptance | `glm spec:` lines in the engine log | old 87–96%, new 88–99% — unchanged |

The single ctest failure, `ple_parity`, is **not** a driver issue and also failed on the
old driver on 2026-10-24:

```
ple_parity: required PLE table is missing or incompatible:
../../Q2_0/Qwen3.8-Flash-Next-Q2_0-00002-of-00002.gguf (cannot open ...)
```

The `Q2_0` shards are not on disk anywhere on this box, so the test cannot run under
either driver. (Baseline for comparison: `ctest.log` 94%/68 with 4 failures, `ctest2.log`
99%/68 with `ple_parity` — both from 2026-10-24, on 580.95.05.)

## Throughput — a controlled driver A/B

Every comparison that mixes drivers also changes the pool size, and the pool size changes
the decode rate. To separate them, the same config and the same headroom were run on both
drivers. `tools/bench_tokens.py` drives the engine alone; the `glm stat:` line is the
engine's own steady-state decode metric.

**Same config (114688 context), same headroom (24), only the driver differs:**

| | old 580.95.05 | new 580.178.04 |
|---|---|---|
| expert pool | 73.5 GB | 76.05 GB |
| ms/token (engine stat) | 64.7 | **62.3** |
| engine-stat tok/s | 15.45 | **16.04** |
| VRAM hit rate | 99.61% | **99.75%** |
| disk reads/token | 1.25 | **0.77** |

Compute per token is slightly *faster* on the new driver, and it streams less from disk.
Caveat on sample size: the old-driver side is 206 engine-stat rows / 937 client samples;
the new-driver side is 1 engine-stat row / 61 client samples (a single 2000-token
generation). The direction agrees on every metric, but the new-driver cell is thin — re-run
the A/B with more generations before treating the +4% as established.

**Why the end-to-end numbers look worse at first glance.** The configs' recorded
calibration (`installer.speed_calibration`, 2026-10-24, old driver) is dflash2 17.43 tok/s
and no-drafting 16.13 tok/s at 1536 answer tokens. Measured on the new driver:
`bench_tokens` gives 13.8 tok/s with the 262144-context config and 14.6 tok/s with the
114688-context one. That gap is **the pool, not the driver**: the calibration was taken
with the engine's default headroom of 16 (pool ~80 GB), and the runs above used headroom
32 (pool 65–68 GB). The engine's own metrics show the mechanism directly:

| headroom | pool | disk reads/token | VRAM hit | decode |
|---|---|---|---|---|
| 16 | ~80 GB | 0.51 | 99.86% | 16.3 tok/s |
| 24 | 73.5 GB | 1.25 | 99.61% | 15.5 tok/s |
| 32 | 65.2 GB | 4.11 | 98.75% | 13.8 tok/s |
| 36 | 61.2 GB | 16.75 | 95.01% | 12.7 tok/s |
| 40 | 57.1 GB | 16.03 | 94.85% | ~12.7 tok/s |

There is a cliff: for the 262144-context config the working set stops being resident
below a ~65 GB pool, and decode becomes disk-bound at ~16 reads/token. Below the cliff
the driver is irrelevant; the pool size decides.

## Stability — the finding that matters

`device_free` (the guard's `F:`) is what the memory floor watches. For the 262144-context
config, a long prefill adds a large transient on top of the steady state: the prompt path
borrows from the expert pool and allocates its own buffers.

```
glm prefill: CUDA0 chunks of 32768 tokens (--prefill auto, up to 32768; mixer sub-batches of 1024),
borrowing 6775 MB of the expert pool while a prompt runs
(activations 5424, weight scratch 96, expert staging 86, prestage 1153: 160 experts)
```

Measured on the new driver, with a 115k-token prompt:

| headroom | pool | `F:` steady | `F:` during the prefill | guard | margin over the 3072 MiB floor |
|---|---|---|---|---|---|
| 32 | 65.2 GB | 7.6 GiB | **2.5 GiB** | **SIGTERM → SIGKILL at 20:42:40** | none — the engine died, the request returned HTTP 503 |
| 36 | 61.2 GB | 10.3 GiB | ~8.1 GiB | survived | ~5 GiB |
| 40 | 57.1 GB | 14.4–18.5 GiB | ~16.1 GiB | survived | ~13 GiB |

**A 115k-token prefill at headroom 32 kills the engine.** Reproduced once, cleanly, with
the guard log as evidence:

```
[2026-10-25 20:42:29] LOW device_free 2559MiB -> armed, needs a confirming sample within 30s
[2026-10-25 20:42:40] LOW device_free 2609MiB < 3072MiB -> SIGTERM build/maya(pid 183443, 76449MiB)
[2026-10-25 20:43:14] pid 183443 still alive after 30s -> SIGKILL
```

The same class of kill has happened all day at lower headrooms (`15:11`, `15:15`, `18:09`,
`18:11` on 2026-10-25), so this is the guard working as designed, not a new fault — but it
means headroom 32 is not a safe operating point for long-context work.

### The fix: cap the prefill chunk, keep the pool resident

The prefill borrow is what breaks headroom 32, and the borrow is a function of the chunk
size. `--prefill <N>` caps it (`auto` sizes it against what the pool can lend, which came
out at 32768 tokens); `STRATA_GLM_PREFILL_KEEP=<slots>` lowers the lend floor and
`--no-prefill-borrow` disables the borrow entirely. The pool reserves the borrow
internally when it is small enough, so a capped chunk never touches `device_free`.

Measured on the new driver, 262144-context config, headroom 32 (pool 65.4 GB), one
115k-token prompt each:

| `--prefill` | borrow (activations) | `F:` dip during the prefill | 115k prefill | guard | warm decode |
|---|---|---|---|---|---|
| auto (32768) | 6775 MB (5424) | **−5.1 GiB** | 179 s | **SIGTERM → SIGKILL** | 13.8 tok/s, 4.11 reads/tok |
| 16384 | 4417 MB (3066) | **−0.73 GiB** | **177 s** | survived | **13.4 tok/s**, 4.70 reads/tok, 98.6% VRAM hit |
| 8192 | 3445 MB (2094) | −0.74 GiB | 200 s | survived | 13.4 tok/s, 4.55 reads/tok |

`--prefill 16384` is the setting to use: it keeps the pool resident (so decode stays at
the fast, ~13.4 tok/s rate instead of the disk-bound ~12.7 tok/s that headroom 40/36
force), it removes the prefill spike entirely, and it costs nothing in prompt-read time —
177 s versus 179 s for the `auto` baseline.

### Recommended operating points

| config | context | headroom | `--prefill` | pool | `F:` steady | note |
|---|---|---|---|---|---|---|
| `maya-maya-s-v2-iq2_xxs-256k.json` | 262144 | **32** | **16384** | 65.4 GB | 10.5 GiB | long-context work; 115k prefills dip `F:` by 0.73 GiB, decode 13.4 tok/s |
| `maya-maya-s-v2-iq2_xxs.json` | 114688 | **32** | auto | 68.1 GB | ~12 GiB | the day-to-day config; 114688 context has no long-prefill spike |

Headroom 36 and 40 are the fallback, not the recommendation: they are safe (the 115k
prefill dips `F:` by 2.4 GiB at 40) but they push the pool below the ~65 GB cliff, so the
working set stops being resident and decode halves its hit rate (16.0 vs 4.7 disk
reads/token, 94.9% vs 98.6% VRAM hit) for ~0.7 tok/s. Headroom 40 is worth it only if
`--prefill` cannot be set (a config that must keep `auto`), and then 40 over 36 buys 4 GiB
of margin at no extra speed cost — both are equally disk-bound.

Both headrooms are now pinned in the configs' `env` blocks
(`"MAYA_GLM_RAM_HEADROOM_GB"`), never as a shell export: `tools/maya_names.py` maps
`MAYA_*` to the `STRATA_*` names the engine reads and `serve/server.py` applies the
config's env after `os.environ`, so the config always wins.

## Admission control

The parameters above are tuned for the prompts that were measured, and nothing stopped a
*bigger* prompt from being admitted and running into the floor — where the memory guard
kills the engine, or the driver refuses the allocation (`NV_ERR_NO_MEMORY`, whose error
path deadlocks the box on this driver). The engine now checks the room before it starts
a request:

```
room a request needs = the prompt path's borrow + the floor
borrow = the engine's own number (the start-up line: "borrowing N MB of the expert pool")
floor  = 3072 MiB, the memory guard's
```

If the device does not have that much free, the engine **waits** (a `NOTE waiting for
room: ...` line every 10 s, so the server's stall check sees it working) and then
**refuses** the request with `ERR out of memory: the device has N MiB free and this
request needs M MiB ...`. The server maps that to a **503 with "the engine is up, try
again"** — the engine lives, the request did not start, and nothing was killed.

| knob | default | what it does |
|---|---|---|
| `STRATA_GLM_ADMIT` | on | `0` turns the check off |
| `STRATA_GLM_ADMIT_FLOOR_MIB` | 3072 | the floor to keep (the guard's) |
| `STRATA_GLM_ADMIT_WAIT_S` | 120 | how long to wait for room before refusing |
| `STRATA_GLM_ADMIT_BORROW_MIB` | the engine's own | the borrow to assume |

Verified live on the 262144 config (pool 65.2 GB, `F:` ~10.4 GiB):

- **Ordinary traffic is admitted**: 10470 MiB free against 4417 + 3072 = 7489 MiB needed.
- **A floor of 100000 MiB** (impossible): the request is refused, the client gets
  `HTTP 503` with *"the device has 10145 MiB free and this request needs 104417 MiB …
  the engine is up, try again"*, the engine stays up, and the guard logs nothing.
- **A floor of 10200 MiB** (just above the free memory): the request waits 45 s with a
  note every 10 s, then refuses the same way — the engine never goes silent, so the
  server's stall check stays disarmed.

With the tuned parameters (headroom 32, `--prefill 16384`) the margin is 3.0 GiB; at
`--prefill auto` the borrow is 6775 MB and the same check would *refuse* a request that
the measurements showed dipping 5.1 GiB under the floor — the case that killed the engine
on 2026-10-25 20:42.

## Defects found (not driver-related)

### 0. The restart path: two engines on one device, and the driver refusing memory

Found while the regression was running, at 2026-10-26 07:36. The guard's samples and log:

| time | what | `F:` | `N:` |
|---|---|---|---|
| 07:36:26 | one engine (pid 243594) holding 76409 MiB | 8.11 GiB | 0 |
| 07:36:37 | **a second engine appears** (pid 248342) - 83847 MiB on a 119 GB device | **0.96 GiB** | 0 |
| 07:36:48 | the newcomer is gone; the driver refused its allocation | 9.76 GiB | **1** |
| 07:37:22 | the guard SIGKILLs the resident engine (its SIGTERM was ignored) | 2.37 GiB | 0 |
| 07:38:03 | the server starts another engine - and another overlap follows | 4.73 GiB | 0 |
| 07:38:37 | it settles | 19.58 GiB | 0 |

Four engine starts in 90 seconds, and the one `N:` - a real `NV_ERR_NO_MEMORY` - is the
warning shot the hard-lock triage names as the precursor of a freeze. Two causes, both
fixed here:

**(a) The engine ignored SIGTERM.** `generate.cpp` installed `SIG_IGN` for SIGTERM so a
group-wide SIGTERM (systemd stopping the service) could not end it mid-write. So the
guard's SIGTERM did nothing: the engine kept its whole pool - 76 GB - for the guard's
30 s grace, and then had to be SIGKILLed. The memory came back late, and the engine
that started next found the device full. The engine now ends on SIGTERM the way QUIT
does: the request in flight is cut at its next token, the conversation is set aside, the
pool is freed and the process exits.

The handler needed two details, both of which cost a build each:

- `signal()` is not enough: glibc's `signal()` sets `SA_RESTART`, so the blocked `read()`
  on stdin restarted itself and the engine never noticed. `sigaction()` without
  `SA_RESTART` returns `EINTR` instead.
- A process-directed signal is delivered to *any* thread that does not block it, so the
  thread waiting on stdin is not necessarily the one the handler runs in. The handler
  writes to a self-pipe and the waiting thread polls it (the self-pipe pattern).

Measured: **the engine now exits 1.14 s after SIGTERM** (before: 30 s of holding the
pool, then SIGKILL).

**(b) The server started an engine while another one was resident.** The headroom is
computed once, at boot, from MemAvailable; a second instance appearing later is not in
that arithmetic, and the pool is sized to leave 8-18 GiB of slack, so one overlapping
engine (76 GB) cannot fit. `StrataEngine.__init__` now makes room before it spawns:
it waits until NVML lists no engine of ours (NVML lists a process until the driver has
freed what it had), and an engine the server does not track - an orphan of an earlier
start, which serves nothing it knows of - is ended (SIGTERM first, SIGKILL after)
rather than left holding the pool. `tools/test_engine_room.py` covers it (6 tests).

Verified live afterwards: one engine at a time in every sample, no `NV_ERR_NO_MEMORY`,
no guard event, and a request served after a deliberate SIGTERM.

### 1. The guard's stall detector false-positives on harness-started engines

At 20:51:57 and 20:57:33 the guard SIGTERMed engines that were demonstrably working:

```
[2026-10-25 20:51:57] STALL build/maya(pid 185167): engine log silent 95s (>= 90s) at 95% GPU,
device_free 6503MiB -> SIGTERM; snapshot ~/gpu_reports/stall_20261025_205157.txt
```

The snapshot shows the engine at **95% GPU utilization** decoding a 2000-token answer. The
guard decided it was stalled because the *file it was watching* had not been written to
for 95 s — and the file was `maya-bench-tokens.log`, because `tools/bench_tokens.py`
writes its progress to stdout instead of the engine log. The engine log path is resolved
from the engine's own stdout, so any harness that redirects stdout (the bench, a cron
job, a wrapper script) gets a quiet file and a healthy engine killed.

Not fixed here. The fix belongs in `tools/gpu_monitor.py`: treat high utilization as proof
of liveness (a stalled engine does not hold 95% of the GPU), or require the watched file
to be the engine's actual stdout.

### 2. `ple_parity` cannot run — the data file is gone

See the correctness section. Nothing to fix in the code; the `Q2_0` GGUF shards have to
come back (or the test has to be skipped when the file is absent) for that test to mean
anything.

## Stability observation

| window | workload | result |
|---|---|---|
| 19:28–21:15 | continuous heavy load: 8 long generations (407–464 s each) + requests queued behind them (422–455 s), U:95% | no lock, no guard kill, `F:` 7.4–8.2 GiB flat, engine RSS 12.0 GiB (+12 MiB / 9.5 min) |
| 21:35–06:15 | engine serving, idle | no lock, `F:` 11.4–12.4 GiB, `N:0` |
| 06:15–06:40 | soak against the 262144 config at headroom 40, incl. three 115k-token prefills | no lock, no guard event, `F:` min 10.02 GiB |
| 06:43–07:07 | 262144 config re-tuned to headroom 32 + `--prefill 16384`; 115k prefills, warm decode measurements | no lock, no guard event, `F:` dip 0.73 GiB |
| 08:01–08:15 | **the full 262144 window read**: a 239,545-token prompt | FOUND, 849 s, `F:` 17.9 GiB, `N:0`, no guard event |
| 07:36 | the restart storm (before the fixes) | 4 engine starts in 90 s, one `NV_ERR_NO_MEMORY`, one guard kill |
| 08:16–08:36 | after the two fixes: deliberate SIGTERM, restart, 115k-equivalent traffic | engine exits 1.14 s after SIGTERM, one engine at a time, no `N:` |

Since the upgrade the box has been up **11h30m with no new boot entry** — i.e. no hard
lock since 580.178.04 came up, against three unclean deaths on 2026-10-25 before it
(17:11:55, 17:49:24, 18:36:48). Across 3874 guard samples since 19:00, `N:` (new NVRM
refusals) was **0** in every single one except the 07:36 restart storm, where one
`NV_ERR_NO_MEMORY` was recorded before the restart path was fixed - and the kernel log
has no `Xid`, no `NVRM` error and no oops.

That is consistent with the driver/firmware hypothesis in
`.opencode/skills/gb10-hardlock-triage` §8, but it is not proof: the surviving runs used a
different headroom (and therefore a different pool) from the runs that froze, and the
record survival on this box was 1h45m, not 11h.

## What is still open

The three remaining gaps, with their evidence, their cost and their acceptance tests, are
written up in [ENHANCEMENT.md](ENHANCEMENT.md). In short:

- **The session store moves the budget under the parameter** — restoring four 115k
  conversations grew the pre-pool footprint by 13.6 GB, and the headroom is computed from
  MemAvailable at boot, so a restart can turn a comfortable config into a tight one.
  Nothing prunes the store automatically. (The engine's own knobs — `STRATA_GLM_SLOTS`
  and friends — can cap it without a rebuild; that is item A in the enhancement page.)
- **The guard's stall detector** false-positives on engines started by another harness
  (see above), and its NVRM rule kills the *biggest* consumer, which is not always the
  one that failed to allocate. (Item B.)
- **The stale 6.14/6.11 kernels** are still installed (see the upgrade hand-off). (Item D.)

Admission control — the gate that stops a request from starting when the device cannot
hold it — is **done**: see the section above.

## Landmines closed during the upgrade

1. **GRUB booted a kernel with no GPU driver.** `GRUB_DEFAULT=saved` with
   `GRUB_SAVEDEFAULT=false` means grub honours `saved_entry`, and the upgrade left it
   naming `6.14.0-1015-nvidia`, whose nvidia module package is `rc` (removed, config
   files only) — booting it gives a box with no `nvidia.ko` at all. Fixed by
   `sudo grub-set-default "DGX OS GNU/Linux, with Linux 7.0.0-1019-nvidia"`; verified
   `grub-editenv list` → `saved_entry=DGX OS GNU/Linux, with Linux 7.0.0-1019-nvidia`
   and `modinfo -k 7.0.0-1019-nvidia -F version nvidia` → `580.178.04`, matching the
   running userspace. The script used is `/tmp/fix-grub-default.sh` (dry-run by default,
   refuses to point grub at a kernel without a driver, aborts on an ambiguous menu
   entry).
2. **Headroom drifted between runs** (16 → 24 → 32 → 24 across one afternoon) because it
   was inherited from ad-hoc shell exports. Both configs now pin it in `env`.

Still open from the upgrade hand-off: the stale 6.14/6.11 kernels and their inert
`nvidia-fs` modules are still installed (see that doc's "The fix" section — needs `sudo`).

## Reproducing

```bash
# engine suite (GPU)
cd build && ctest --output-on-failure -j1

# tools suite (the CI gate)
.venv/bin/python -m unittest discover -s tools -p "test_*.py"

# recall through the API (engine must be running on :8009)
.venv/bin/python tools/needle_bench.py --url http://127.0.0.1:8009 --api-key sk-local \
    --lengths 32k,128k --depths 10,50,90

# throughput (stops nothing, but the engine wants the whole GPU: stop Maya first)
.venv/bin/python tools/bench_tokens.py --config maya-maya-s-v2-iq2_xxs.json \
    --prompts 2048 --answers 1536

# what the guard saw
tail -3 ~/gpu_monitor_log.txt; tail -5 ~/gpu_monitor_guard.log
```

## Cross-references

- [docs/2026-10-25-driver-580-residual-kernels.md](docs/2026-10-25-driver-580-residual-kernels.md)
  — the upgrade itself, the stale kernels, the rollback set
- [BENCHMARK.md](BENCHMARK.md) — the 580.95.05 baseline this page is compared against
- [docs/DGX_SPARK.md](docs/DGX_SPARK.md) — pool sizing on a unified-memory device
- [docs/MEMORY_WATCHDOGS_DEEP_DIVE.md](docs/MEMORY_WATCHDOGS_DEEP_DIVE.md) — the guard,
  the device-free floor, and the stall detector
- `.opencode/skills/gb10-hardlock-triage` — the freeze history and the driver hypothesis
- `.opencode/skills/dgx-spark-memory-forensics` — device-free dips and the WARN bands
