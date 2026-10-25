---
name: watchdog-deep-dive-docs
description: Use when writing or updating the memory-watchdog deep-dive documentation (docs/MEMORY_WATCHDOGS_DEEP_DIVE.md and its pandoc html), comparing the local memwatch.sh against MiaAI Lab's upstream, or refreshing the GPU_MONITORING_GUIDE ops card. Covers the document's structure, the upstream source of truth, local-vs-upstream drift, and the exact pandoc html regeneration command.
---

# The watchdog deep-dive documentation

Canonical copy: `docs/MEMORY_WATCHDOGS_DEEP_DIVE.md` (+ `.html`) in the maya
repo, committed and pushed; working copies live at
`~/MEMORY_WATCHDOGS_DEEP_DIVE.md` / `.html`. Keep all three in sync (repo
first, then copy to `~/`).

## Sources the doc describes (read them fresh before updating)

1. `~/projects/maya/tools/gpu_monitor.py` — the system guard (state files,
   triggers, victim attribution, journal watch, concurrency guards).
2. `~/projects/maya/safe-build.sh` — the build watchdog (pre-flight,
   isolation, freeze/resume/abort, traps).
3. memwatch.sh — **upstream source of truth**:
   https://github.com/MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark/blob/main/files/memwatch.sh
   (fetch the raw URL with webfetch). Local deployment copy:
   `~/qwen38-flash-next-vllm/files/memwatch.sh` — it may LAG upstream and/or
   carry locally tightened floors. Known state (2026-10-25): upstream has the
   leak-trend detector (baseline min over [900 s, +600 s] post-load window,
   flags ≥4 GiB/day), the `WATCHDOG EMERGENCY STOP` marker line and the
   `scripts/alert.sh` hook; the local copy has neither and runs tighter
   floors (3/1 GiB, 15 consecutive vs upstream 6/2, 5) while its header
   prose still says 6/2 — trust code, not headers. Diff before documenting.
4. Live evidence: `~/qwen38-flash-next-vllm/logs/memwatch-vllm-fn-tp1.log`
   (the 2026-10-21 weight-load ramp), `~/gpu_monitor_guard.log` (drills,
   WARN bands, kills), `docs/2026-10-25-guard-concurrency.md` (the
   05:16:28 two-invocation incident).

## Document structure (keep it)

0. Why all three exist — GB10 unified memory, the two truths, the
   defense-in-depth layering table.
1. gpu_monitor — architecture, trigger order, victim selection, kill path,
   concurrency hardening, journal watch, verified behavior + gaps closed.
2. safe-build — contract, four stages, signal handling, floor-ordering
   invariant, env knobs.
3. memwatch — philosophy, debounced triggers, NVRM delta counting,
   leak-trend detector, marker + alert, diagnostic heartbeat, the logged
   weight-load ramp, local-vs-upstream divergence table.
4. Cross-cutting comparison, shared lessons, gaps/future work (strikethrough
   closed gaps with the closing commit sha), companion-sources footer with
   every commit sha.

Update ritual after each guard commit: add the sha to the footer, fold the
change into §1, strike the closed gap in §4.

## Regenerating the html

pandoc with an embedded style header (the committed
`docs/MEMORY_WATCHDOGS_DEEP_DIVE.html` already carries it; reuse this CSS):

```bash
printf '%s\n' '<style>' \
 'body{max-width:900px;margin:2em auto;padding:0 1em;font-family:-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.55;color:#1a1a1a}' \
 'h1,h2,h3{border-bottom:1px solid #ddd;padding-bottom:.2em}' \
 'table{border-collapse:collapse;margin:1em 0;font-size:.92em}' \
 'th,td{border:1px solid #bbb;padding:4px 8px;text-align:left}' 'th{background:#f0f0f0}' \
 'pre{background:#f6f8fa;padding:.7em 1em;border-radius:6px;overflow-x:auto;font-size:.9em}' \
 'code{background:#f0f0f0;padding:1px 4px;border-radius:3px;font-size:.92em}' 'pre code{background:none;padding:0}' \
 'blockquote{border-left:4px solid #ccc;margin:1em 0;padding:.2em 1em;color:#555}' \
 'hr{border:none;border-top:2px solid #eee;margin:2em 0}' '</style>' > /tmp/dive-style.html
pandoc MEMORY_WATCHDOGS_DEEP_DIVE.md -s -H /tmp/dive-style.html \
  --metadata title="Memory Watchdogs on the DGX Spark — Deep Dive" \
  -o MEMORY_WATCHDOGS_DEEP_DIVE.html
```

## Stale-doc hygiene

`~/GPU_MONITORING_GUIDE.md` is the quick-reference ops card — rewritten
2026-10-25 from an ancient cron/ollama/Telegram version. If tooling changes
(floors, sample-line fields, state file names), update the card in the same
session; a monitoring guide that describes a dead architecture is worse
than none during an incident. `docs/DGX_SPARK.md` § "Memory safety" is the
repo-side pointer to all of this.
