# Evidence ledger — the 2026-10-26 memory and correctness session

Box: DGX Spark GB10 (`carlosp-nvidia-dgx`) · Engine: `Maya-S-v2-IQ2_XXS` (GLM-5.3-Flash) on
driver **580.178.04**, kernel `7.0.0-1019-nvidia` · Server: `maya-maya-s-v2-iq2_xxs-256k.json`,
headroom **40**, `--prefill 16384`, `MAYA_GLM_SLOTS=1`.

This is the chronological record of what was measured, what was concluded, and — just as
important — which runs were **invalidated** and why. Every row names its artifact so a
number can be re-derived. The analysis lives in [REGRESSION.md](REGRESSION.md) (the fixes),
[docs/STABILITY-1.md](docs/STABILITY-1.md) (the parameters),
[ENHANCEMENT.md](ENHANCEMENT.md) (the gaps, incl. §0b the stale-context defect) and
[COORDINATION.md](COORDINATION.md) (the two-agent hand-off).

Artifacts: guard samples `~/gpu_monitor_log.txt` (`F:` device-free is the honest number),
guard decisions `~/gpu_monitor_guard.log`, per-request engine log
`maya-maya-s-v2-iq2_xxs-256k.log`, per-probe JSON in `/tmp/*.{json,log}` (session-scoped).

## The timeline

| time (PDT) | what was run | result | verdict |
|---|---|---|---|
| 19:09 (10-25) | the 580.178.04 boot | clean shutdown before it; no freeze since | valid |
| 20:32 | `ctest` on the new driver | 44/45 — `ple_parity` fails identically on the old driver (missing GGUF shard) | valid |
| 20:35 | tools suite | 452 OK | valid |
| 20:37 | needle 32k/128k ×3 depths | 5/6 FOUND; the 128k/50% got HTTP 503 — the guard killed the engine (`device_free 2609 MiB < 3072`) | valid — the finding: headroom 32 is unsafe for long prefills |
| 20:49 | `bench_tokens` 256k cfg | 13.8 tok/s (vs the 17.43 calibration — the pool, not the driver) | valid |
| 20:55 | `bench_tokens` 114k cfg | 14.6 tok/s | valid |
| 20:58–21:34 | headroom 36 probes | the 115k prompt survived; decode disk-bound (16.75 reads/tok) | valid |
| 21:06–21:15 | headroom 40 probes | 115k ×3 FOUND, `F:` dip 2.4 GiB | valid |
| 06:15–06:40 | soak at headroom 40 | no lock, no guard event, `F:` min 10.02 GiB | valid |
| 06:43–07:07 | headroom 32 + `--prefill 16384` | 115k FOUND, `F:` dip 0.73 GiB, warm decode 13.4 tok/s | valid — **the adopted operating point** (then superseded, see 10:41) |
| 07:36 | the restart storm | 4 engine starts in 90 s, one `NV_ERR_NO_MEMORY`, one guard kill | valid — **the restart-path defect** |
| 08:01 | **the full 262144 window read** | 239,545 tokens, FOUND, 849 s, `F:` 17.9 GiB, `N:0` | valid — the 256k goal met at headroom 40 |
| 08:16–08:36 | the restart-path fixes, verified live | SIGKILL → request → served; no overlap in any sample | valid |
| 12:06–12:28 | the needle runs after `MAYA_GLM_SLOTS=1` | "0 of 2 found" with swapped needle words | **valid runs, wrong theory** — not the cache: the haystack carried the bench's own source (fixed), and the operator's notes carried two needle words (fixed by the one-occurrence check) |
| ~12:38 | the reuse probe (3 conversations after 103k/239k ones) | **the answers lagged one conversation behind, and quoted the other agent's prompt and reasoning verbatim** | valid — **the stale-context defect, cross-client** (ENHANCEMENT.md §0b) |
| 12:52 | the engine restart | the probe then passed 3/3 — the bleed needs a primed state | valid |
| 13:0x | the probe primed with an 89k conversation | all correct | valid — a *shorter* probe after a *longer* conversation passes when the priming text carries no distinguishing token |
| 13:16 | the probe primed with a needle word at ~80k | all correct | valid — the single-client case exonerated on the then-current binary |
| 13:31 | the engine on the ckpt-fix binary (`0f1f10b`) | `F:` 25.54 GiB | valid |
| 13:40 | two interleaved clients, 12k conversations | 12/12 correct | valid — but no checkpoint at 16,384 existed at this size, so the bleed path was not exercised |
| 13:49 | two interleaved clients, 27k conversations | **24/24 correct** — the checkpoint path exercised, the fix held | valid |
| 13:59 | **the cancel-window probe** | "3/3 wrong" (empty answers, one-behind) | **INVALIDATED** — the server was shutting down mid-test and another client's tool-calling session was interleaving; recorded in `COORDINATION.md` §8 as a candidate, not a finding |
| 14:06 | (the other agent) restarted the server | the engine now runs the ckpt-fix binary | valid |
| 14:13 | **the cancel-window probe, the redo, exclusive box** | **3/3 wrong — the defect reproduces deterministically**: B answered A's needle, C empty, D C's needle; the engine read two prompts at once and a client got its response before its read finished |
| 14:22 | the engine restarted (the run's state was poisoned) | clean, `F:` 25.12 GiB |
| 14:29 | **the fix live**: the drain-timeout marks the stream `may_be_writing`; the next request skips the stale lines to the skipped request's DONE before it consumes anything of its own | the same cancel probe: **3/3 correct**, and the server logged "the stream is clean" |
| 14:39 | two interleaved clients, 27k conversations, on the fix | 6/6 correct | |

## What the day's evidence supports

1. **The driver upgrade is not contradicted by anything**: no freeze, `N:0` in every
   sample since the 580.178.04 boot, and the one `NV_ERR_NO_MEMORY` of the day is
   attributed to the restart storm (fixed).
2. **The 256k operating point**: headroom 40, `--prefill 16384`. Headroom 32 fails the
   admission gate's own arithmetic once the session state is counted; 36 is dominated by
   40.
3. **The stale-context defect** (ENHANCEMENT.md §0b) is the one open *correctness* risk:
   a conversation shorter than the previous one can inherit its tail. Single-client
   sequential use passes every probe; the multi-client bleed reproduced once (12:38) and
   the controlled redo is the 14:13 row.
4. **The cancel-mid-prefill window** is the last unproven mechanism: the candidate for the
   12:38 bleed, invalidated at 13:59, redone at 14:13 on an exclusive box.

## The cancel-window defect and its fix (the 14:13 → 14:29 pair)

The 14:13 redo reproduced the defect deterministically (3/3 wrong, one conversation
behind, one empty). The mechanism was in the **server**, not the engine: a client that
vanishes mid-request makes the server STOP the engine and drain to the request's DONE —
bounded at `STRATA_DRAIN_S` (60 s). When the drain gave up, the engine's *remaining* lines
(the answer's tokens, then its DONE) were left in the shared, untagged queue — and the
next request consumed them, so it was answered with the previous request's text.

The fix (`serve/server.py`): the drain-timeout marks the stream `may_be_writing`, and the
next request's first act is `_skip_stale_lines()` — consume-and-discard to the skipped
request's DONE (bounded 180 s), then proceed with its own lines. Verified by the 14:29
cancel probe (3/3 correct where 14:13 was 3/3 wrong) and the 14:39 interleaved-clients run
(6/6).

## The redo row (14:13, exclusive box, ckpt-fix binary)

**3/3 wrong — the defect reproduces deterministically.**

```
A: a 82,421-token prompt — the client got its response in <25 s (the engine read for 108 s)
B: a 2,160-token prompt (needle velvet-saffron-909) -> answered cobalt-harbor-551  = A's needle
C: a 2,159-token prompt (needle quartz-juniper-142) -> answered '' (0 tokens, finish=length)
D: a 30k-token prompt   (needle ember-glacier-733)  -> answered quartz-juniper-142 = C's needle
```

The engine log adds two facts that change the shape of the bug:

1. **B's read ran interleaved with A's chunks** — "reading the prompt: 2,161 tokens, 70/80/90/100 s"
   interleaved with A's 16,384-token chunk lines. The engine was reading two prompts at once.
2. **A's client got its response before its read finished** — the request and the response are
   being mismatched under load, not just the conversation state.

So the class is wider than the checkpoint maps: the request→response pairing (and/or the
read pipeline) desynchronises when a long request is followed by short ones. The
checkpoint-maps fix (`0f1f10b`) was necessary but not sufficient.

Consequence: the engine's state was poisoned by this run — **restarted at 14:2x** before
the chat window is used again. The defect's fix is the next surgery; the probe is
`tools/cancel_probe.py`, the reproduction is one command on an exclusive box.