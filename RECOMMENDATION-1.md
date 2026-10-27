# RECOMMENDATION-1: the next run — headroom 32 with `--prefill 16384`

Date: 2026-10-27 · Status: **recommendation, not yet applied** · Evidence: one
9h20m continuous run (engine pid 119402, 256k config, `MAYA_GLM_RAM_HEADROOM_GB=40`,
`--prefill 16384`) from 2026-10-26 19:56 to 2026-10-27 05:16, read from
`~/gpu_monitor_log.txt`, `~/gpu_monitor_guard.log`, `~/maya_health_watch.log`, and
`maya-maya-s-v2-iq2_xxs-256k.log`.

Companion: `docs/STABILITY-1.md` (the measured headroom/chunk table),
`ANALYSIS-1.md` (why decode is a disk-wait function), `REGRESSION.md`.

## 1. The recommendation

Change exactly one knob in `maya-maya-s-v2-iq2_xxs-256k.json`:

```json
"env": {
  "MAYA_GLM_RAM_HEADROOM_GB": "32"
}
```

Leave everything else as it is in this run:

- `--prefill 16384` (do **not** go back to `auto`/32768 in the same attempt)
- `MAYA_MAX_PROMPT_TOKENS=65536`
- `MAYA_GLM_SLOTS=1`
- `MAYA_ADMIT_COOLDOWN_S=4`

Headroom 40 is the documented *fallback* for a config that has to keep
`--prefill auto`. The measured baseline in `docs/STABILITY-1.md` §5 is **32 +
`--prefill 16384`**: 13.4 tok/s, 4.7 disk reads/token, 98.58% VRAM hit, and the
115K-token prompt survived it with a 0.73 GiB dip and no guard event. The current
run is paying the fallback's disk-bound tax instead.

## 2. What the 9-hour run established

- **Stable under heavy load.** One engine, no restart, no guard WARN/kill, no
  `NV_ERR_NO_MEMORY` (`N:0`), no stall, no NUL padding, no swap use; sustained
  ~95% GPU, 9.3-9.5 tok/s, 77-79°C (brief 85-86°C spikes).
- **One clean step, then flat.** `F:` went 21.8 -> 16.2 GiB at 04:34-04:40 and held
  16.1-16.6 GiB for the remaining ~45 min. `M` stayed at 66237 MiB; `MemAvailable`
  moved only 38.9 -> 38.8 GiB while `MemFree` fell 21.5 -> 16.8 GiB. That is the
  step-held / driver-allocator signature, not a leak. It coincided with a
  31,914-token prompt and a 31,913-token session save.
- **It is disk-bound, not GPU-bound.** The same session's `glm stat` lines show
  30-60 disk reads/token in 21-37 waits/token, 45-85 ms/token of disk time, 9.3
  tok/s. `ANALYSIS-1.md`'s law (decode ~= 63 ms compute + ~2.05 ms per disk wait)
  says the pool is too small for the working set.

Boot line from this run:

```
glm fast: CUDA0 unified memory: MemAvailable 96.10 GB, headroom 40.00 GB + 1024 MB reserve, experts need 80.17 GB, pool budget 55.10 GB
maya serve: CUDA0: 23449 MiB of VRAM free with everything loaded
```

## 3. What 32 should give

| Quantity | current (40) | expected (32) | basis |
|---|---|---|---|
| pool budget | 55.1 GB | ~63.1 GB | `MemAvailable - headroom - 1 GB`; +8 GB for -8 GB headroom |
| VRAM free at boot | 23.4 GiB | ~15.4 GiB | measured boot line minus the 8 GB larger pool |
| disk reads/token | 30-60 | ~5 | `STABILITY-1.md` §3/§5 (4.7 measured at 32) |
| decode | ~9.3 tok/s | ~13 tok/s | `STABILITY-1.md` §5 (13.4 measured) |
| late-run `F:` | 16.1 GiB | ~8-9 GiB | boot free minus the ~7 GiB of context/prompt-path growth seen over 8.5 h |

The last row is the honest unknown: this run's `other (context, MTP, scratch,
prompt path)` grew from 32.55 GB at boot and the 04:30 step alone was ~5 GiB. At 32
that growth comes out of a ~15 GiB boot margin, not a 23 GiB one.

## 4. Risk and the safety net

- **Not a crash path.** 8-9 GiB late in the run is above the 3072 MiB guard floor.
  The admission gate already compares free device memory with `borrow 4417 +
  floor 3072 = 7489 MiB` before it starts a request; below that it waits and then
  refuses with a 503 rather than letting the guard kill the engine
  (`STABILITY-1.md` §6 point 2, `REGRESSION.md`).
- **The failure mode is refused requests, not a hard lock.** If the 04:30-style
  step recurs at 32, `F:` can reach ~8 GiB; another similar step would reach the
  gate's ~7.5 GiB refusal line. The guard's 3 GiB floor still has margin.
- **Do not combine changes.** `--prefill auto` at headroom 32 is the exact
  combination that killed the 20:42 run (`F:` 4.10 -> 2.50 GiB in one 11 s
  sample). Keep the chunk at 16384.

## 5. Validation after the restart

Read the four numbers from the engine log (§8 of `STABILITY-1.md`):

```bash
grep -a "unified memory:" <engine log> | tail -1
grep -a "borrowing" <engine log> | tail -1
grep -a "VRAM before the expert pool" <engine log> | tail -1
grep -a "VRAM free with everything loaded" <engine log> | tail -1
```

`VRAM free with everything loaded` must exceed `borrow + 3072 MiB + ~1 GiB`
(i.e. > ~8.2 GiB). Then run one real 32K-token prompt and re-check:

- **Keep 32** if `F:` holds >= ~9 GiB and reads/token is ~5.
- **Fall back to 36/40** if `F:` is < ~8 GiB after the first 32K prompt, or if a
  second 04:30-style step appears. 36 is dominated by 40 (same disk-bound decode,
  less margin), so go straight back to 40 if the fast point does not hold.
- **Alarm** on any `N: > 0`, guard WARN/kill line, or `F:` < 4 GiB — that is the
  existing health-watch contract, unchanged.

## 6. What not to change in this attempt

`--max-context 262144` (not the pressure point this week), `--expert-cache auto`
(let it size the pool), `--kv-streaming on`, the 65536 prompt cap, the 4 s
admission cooldown, and `MAYA_GLM_SLOTS=1`. Raising the prompt cap or the prefill
chunk moves the disk burst and context-store growth in the riskier direction; the
headroom is the one lever with a measured safe point below the current setting.

## 7. Cross-references

- `docs/STABILITY-1.md` — the measured table (§3), the prefill chunk (§4), the
  recommendation (§5), the admission control and session store (§6), re-measure
  commands (§8).
- `ANALYSIS-1.md` — decode as a function of disk waits; the disk burst under
  long prompts.
- `docs/2026-10-26-hardlock-1521-headroom40.md` — why headroom alone is not a
  proven predictor.
- `.opencode/skills/gb10-hardlock-triage` §6 and
  `.opencode/skills/dgx-spark-memory-forensics` — the headroom lever and the
  `F:`/`N:`/`A:` trust order.
