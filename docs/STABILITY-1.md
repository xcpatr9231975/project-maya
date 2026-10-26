# Stability parameters for the 256K context config

Date: 2026-10-26
Status: measured and recommended -- headroom **32** together with `--prefill 16384`,
which is what the 256k config now ships. Headroom **40** is the fallback for a config
that has to keep `--prefill auto`: it is safe, but it puts the pool below the ~65 GB
cliff, so the working set stops being resident and the decode becomes disk-bound.
Every row of the table in §3 is now measured, including the 115K prompt at 32, 36 and
40. [REGRESSION.md](../REGRESSION.md) has the full run behind the numbers.
Affects: `maya-maya-s-v2-iq2_xxs-256k.json` (`env` and `args`), `src/core/glm_fast_path.cu`
(pool sizing), `src/core/glm_prefill.cu` (chunk size / borrow).
Evidence: engine log `maya-maya-s-v2-iq2_xxs-256k.log`, guard log
`~/gpu_monitor_guard.log`, monitor log `~/gpu_monitor_log.txt`, all from the
2026-10-25 20:42 kill and the 2026-10-25/26 monitoring run.

## 1. The parameter

**`MAYA_GLM_RAM_HEADROOM_GB`**, set in the config's `env` block:

```json
"env": { "MAYA_GLM_RAM_HEADROOM_GB": "32" }
```

Why this form and not a shell export:

- `tools/maya_names.py` maps `MAYA_*` -> the `STRATA_*` names the engine reads
  (`MAYA_GLM_RAM_HEADROOM_GB` -> `STRATA_GLM_RAM_HEADROOM_GB`).
- `serve/server.py` applies it on load and `child_env()` passes it through, and
  `child_env` applies `cfg["env"]` *after* `os.environ`, so the config wins over
  any shell export -- including the `STRATA_GLM_RAM_HEADROOM_GB=24` that
  `run-maya-maya-s-v2-iq2_xxs.sh` exports.
- It survives `--calibrate` (`calibrate_glm.py`'s `CAL.apply` only owns
  `STRATA_GLM_PCIE_SHARE` / `CPU_LANE` / `READ_CHUNKS`).
- It prints in the engine's startup line, so every run is self-documenting.

The engine default is 16 (`head_gb = 16.0` in `src/core/glm_fast_path.cu`).

## 2. The sizing math, and why it is the stability lever

```
pool budget = MemAvailable - headroom - 1 GB reserve          (glm_fast_path.cu:1533)
```

The pool budget is computed **without subtracting the prompt path's own borrow**,
which the engine measures and prints at boot but never reserves. On a 256k run
(MemAvailable 98.2 GB, device 119.07 GB):

```
glm prefill: CUDA0 chunks of 32768 tokens ..., borrowing 6775 MB of the expert pool
             while a prompt runs (activations 5424, weight scratch 96,
             expert staging 86, prestage 1153: 160 experts)
glm fast: CUDA0 VRAM before the expert pool: 50.42 of 119.07 GB in use - dense weights
          6.86, state/KV (262144 ctx) 0.92, activations 0.06, other (context, MTP,
          scratch, prompt path) 42.xx
maya serve: CUDA0: 10612 MiB of VRAM free with everything loaded
```

So the stability condition is:

```
free-at-boot  >=  prompt borrow  +  3072 MiB kill floor  +  ~1 GiB slack
```

## 3. Measured table (256k config, MemAvailable ~98.2 GB, driver 580.178.04)

| headroom | `--prefill` | pool budget | free at boot | after the borrow | vs 3072 MiB floor | verdict |
|---|---|---|---|---|---|---|
| 32 | auto (32768) | 65.2 GB | 7.6 GiB | **2.5 GiB** | −0.6 GiB | **kills** (2026-10-25 20:42) |
| 32 | **16384** | 65.4 GB | 10.5 GiB | 9.8 GiB | +6.7 GiB | **safe**, 13.4 tok/s, 4.7 reads/token |
| 32 | 8192 | 65.4 GB | 10.5 GiB | 9.7 GiB | +6.6 GiB | safe, but +12% prefill time |
| 36 | auto | 61.2 GB | 10.3 GiB | ~8.1 GiB | +5.1 GiB | safe, but disk-bound (16.8 reads/token) |
| 40 | auto | 57.1 GB | 14.4–18.5 GiB | ~16.1 GiB | +13 GiB | safe, disk-bound (16.0 reads/token) |

The dip column is the *measured* device-free during one 115K-token prefill, not an
extrapolation: the guard log's own numbers for the killed case are `F:` 4.10 → 2.50 GiB
in one 11 s sample.

Note the two things the chunk size buys at once: the borrow is smaller (so the dip is
smaller) **and** the pool's own reservation for the prompt path is smaller, so the free
memory at boot is 2.9 GB higher (10.5 GiB at 16384 against 7.6 GiB at 32768). The
"free at boot" column moves with the chunk, not only with the headroom.

Empirical rate from the measured points: **+4 GB of headroom bought +4.3 GiB of free**
(6.3 → 10.6 going 32 → 36), i.e. `free-at-boot ~= headroom - 29.7 GB`. The 32, 36 and
40 rows in the table above are measured; 44 is extrapolated on that slope and should be
re-measured from the engine's `maya serve: CUDA0: N MiB of VRAM free` line after any
change.

## 4. The second parameter: the prefill chunk

`--prefill auto` picks **32768**, and that is where the whole 6.8 GB borrow comes
from. The borrow does **not** scale linearly with the chunk -- it has a floor -- so the
numbers below are measured, not estimated:

| chunk | borrow | of which activations | 115K prefill | at headroom 32 |
|---|---|---|---|---|
| 32768 (`auto`) | 6775 MB | 5424 MB | 179 s | dips `F:` by 5.1 GiB -- kills the engine |
| **16384** | **4417 MB** | 3066 MB | **177 s** | dips `F:` by 0.73 GiB -- safe, no measurable cost |
| 8192 | 3445 MB | 2094 MB | 200 s | dips `F:` by 0.74 GiB -- safe, +12% prefill time |

Set it as `--prefill 16384` in the config's `args` (where it is now), or
`STRATA_GLM_PREFILL_CHUNK=16384` (the older knob; `src/core/glm_prefill.cu:977`).
Prefill throughput at 32768 measured 630-716 tok/s (65536 tokens in 2 chunks at
708 tok/s); at 16384 a 115K prompt read in 177 s against 179 s for `auto`, so halving
the chunk costs nothing measurable at this size -- the 8192 setting is where the cost
appears (+12%).

## 5. Recommendation

- **Headroom 32 with `--prefill 16384`** as the baseline -- and this is what the 256k
  config ships now. The pool stays resident (4.7 disk reads/token, 98.6% VRAM hit,
  13.4 tok/s) *and* a 115K prompt dips `F:` by 0.73 GiB instead of 5.1 GiB. It is
  faster than every safe alternative measured, because the alternatives are
  disk-bound rather than because this one is cheap.
- **Headroom 40** only if `--prefill` cannot be set (a config that has to keep
  `auto`): safe with +13 GiB of margin, but the pool falls below the ~65 GB cliff, so
  the working set stops being resident and decode drops to ~12.7 tok/s at 16.0
  reads/token and 94.9% VRAM hit. 40 over 36 is free -- both are equally disk-bound --
  so take 40, not 36.
- Keep the chunk at **16384**. 8192 buys no more margin (0.74 GiB of dip against
  0.73 GiB) and costs 12% of the prefill time.
- Cost reference: the documented measurement is headroom 16 -> 32 costing ~13% decode
  (~0.8%/GB), so 32 -> 40 is ~6% and 32 -> 44 ~9.5%. That is an extrapolation --
  measure it on the real workload.

## 6. Two non-parameters that also decide 256k stability

0. **The restart path (fixed 2026-10-26).** The engine used to ignore SIGTERM, so the
   guard's kill was 30 s of holding the whole pool followed by a SIGKILL; and the server
   could start an engine while another one was resident, which does not fit on this box
   (two pools of 65 GB in 119 GB) - the driver refused the newcomer's allocation
   (`NV_ERR_NO_MEMORY`, the warning shot before a hard lock) and the guard then killed the
   engine that was working. The engine now ends on SIGTERM the way QUIT does (measured:
   1.14 s, and the pool is freed at once), and a start waits until NVML lists no engine
   of ours. See [REGRESSION.md](../REGRESSION.md).
1. **The session store.** Restoring four 115K conversations grew the pre-pool footprint
   `other (context, MTP, scratch, prompt path)` from 27.9 -> 42 GB (+13.6 GB)
   (`glm slots: saved a 115201-token conversation (1.61 GB); 4 kept, 3.18 GB`). Every
   restart re-enters the same tight budget whatever the headroom says. Cap what it
   restores, or prune old conversations before a restart.
2. **Admission control -- the actual fix.** The engine already calls `cudaMemGetInfo`
   in ~12 places (`src/program/generate.cpp`); checking
   `free >= borrow + floor` *before* admitting a prefill turns a guard kill into a
   queue, after which no headroom value can be wrong. The engine already refuses the
   expert pool at boot for exactly this reason ("no room for the expert pool: 0 slots
   a layer, at least 8 needed") -- the same check at request time is what is missing.

## 7. Validation status (honest)

Every case below was run on 2026-10-26 with the 262144-context config on the new
driver (580.178.04), one 115K-token prompt each, and the guard log read afterwards.

- **headroom 32 + `--prefill auto`**: killed at 2026-10-25 20:42 by a 115,201-token
  prompt prefill. `F:` 4.10 -> 2.50 GiB in one 11 s sample; guard armed 20:42:29,
  SIGTERM 20:42:40, SIGKILL 20:43:14 (the engine was mid-prefill and did not honour
  SIGTERM within 30 s).
- **headroom 32 + `--prefill 16384`**: the 115K prompt **survived** -- `F:` 10.52 ->
  9.79 GiB (a 0.73 GiB dip), no guard event at all, the needle was FOUND, and the
  prompt read in 177 s. Warm decode 13.4 tok/s, 4.70 disk reads/token, 98.58% VRAM
  hit.
- **headroom 32 + `--prefill 8192`**: also survived (0.74 GiB dip), but the prompt
  read took 200 s -- 12% slower than `auto` for no extra margin.
- **headroom 36**: prompts up to ~29K tokens plus one 65536-token prefill ran with
  zero guard events (window 20:58-21:34), and the 115K prompt then ran clean as well
  (pool 61.19 GB, `F:` 10.3 GiB, ~8.1 GiB after the borrow). It is dominated: the
  decode is disk-bound (16.75 reads/token) for no margin that 40 does not give.
- **headroom 40**: the 115K prompt ran clean three times (pool 57.06 GB, `F:` 14.4-18.5
  GiB, ~16.1 GiB after the borrow, 3/3 needles FOUND). Also disk-bound (16.03
  reads/token, 94.85% VRAM hit).
- **headroom 44**: not tested, and not worth testing while 40 is free over 36.

## 8. How to re-measure after any change

```bash
grep -a "unified memory:" <engine log> | tail -1     # MemAvailable, headroom, pool budget
grep -a "borrowing" <engine log> | tail -1           # the prompt borrow in MB
grep -a "VRAM before the expert pool" <engine log> | tail -1
grep -a "VRAM free with everything loaded" <engine log> | tail -1   # the number that matters
```

`VRAM free with everything loaded` must exceed `borrow + 3072 MiB + ~1 GiB`.

## 9. Cross-references

- [REGRESSION.md](../REGRESSION.md) -- the full regression on driver 580.178.04, with
  the measurements behind §3 and §4
- `docs/2026-10-25-driver-580-residual-kernels.md` -- the other open item from the
  same upgrade (stale 6.14/6.11 kernels, purge commands, Monday reminder).
- `.opencode/skills/gb10-hardlock-triage` §6 -- the headroom lever, and why the value
  belongs in the config rather than a one-off export.
- `.opencode/skills/dgx-spark-memory-forensics` -- reading `F:` / `N:` / `A:`, the
  3072 MiB floor, and leak vs step-held vs transient.
- `docs/MEMORY_WATCHDOGS_DEEP_DIVE.md` -- the guard that enforces the floor.
