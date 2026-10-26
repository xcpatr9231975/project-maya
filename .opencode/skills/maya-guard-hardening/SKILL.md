---
name: maya-guard-hardening
description: Use when hardening, reviewing, or deploying the DGX Spark GB10 memory guard (tools/gpu_monitor.py, safe-build.sh) in the maya repo — implementing a guard fix, porting a watchdog technique, running its sandbox tests, deploying to ~/gpu_monitor.py, or pushing to the fork and private remotes. Covers the invariant-preserving workflow, the NOTICE/AGPL credit rule for ported techniques, and the deploy-without-restart pattern.
---

# Hardening the maya memory guard (gpu_monitor.py / safe-build.sh)

Repo: `~/projects/maya` (branch `main`; `origin` = mw00/project-maya upstream,
`fork` = xcpatr9231975/project-maya, `private` = xcpatr9231975/maya-dgx-spark).
Push `main` to **fork and private only**. Never touch `fork/dgx-spark` — it feeds
upstream PR #471 and stays where the owner leaves it.

## Invariants every change must preserve

1. **Two-sample confirmation.** One invocation = one sample (systemd
   `gpu_monitor.timer`, `Restart=always`, ~10 s cadence). Anything spanning
   samples lives in `~/.gpu_monitor_guard_state`
   (`armed:<ts>` → `termed:<pid>:<ts>`). A confirming sample must be ≥
   `MIN_CONFIRM_AGE_S` (8 s) after the armed one and ≤ `GUARD_CONSEC_S` (30 s).
2. **Concurrent-invocation safety.** Whole-run `flock` on
   `~/.gpu_monitor_guard_lock`; a second invocation exits 0 silently.
3. **Freeze-before-kill ordering.** safe-build's floors sit ABOVE the guard's
   kill floors (device-free 4096 vs 3072 MiB; PSI 5.0 % but polled at 5 s vs
   10 s). Never lower a safe-build floor below the guard's matching kill floor.
4. **RSS baseline discipline.** Rebase on healthy samples only (growth must
   accumulate across the armed→confirm window); entries are
   `pid:starttime:rss` (stat field 22) so a reused pid counts as a fresh
   process; skip pids with no `/proc/<pid>/stat`; legacy 2-token baselines
   parse as same-process deltas.
5. **Cursor discipline (journal watch).** `~/.gpu_monitor_guard_nvrm` epoch
   cursor advances only after a successful `journalctl` read; first run
   initialises without counting; boundary double-count is accepted over
   epsilon-drop.
6. **Stall guard is a separate state machine.** `~/.gpu_monitor_stall_state`
   (`stall-termed:<pid>:<starttime>:<ts>`) — never merge it into
   `~/.gpu_monitor_guard_state`, or the memory guard's "unknown guard state"
   branch discards it. It has NO two-sample rule on purpose: the confirmation
   is the engine log's own mtime age (silent ≥ `STALL_QUIET_S`) *and* the GPU
   being at ≥ `STALL_UTIL_PCT`, which is already a sustained condition. The
   utilization gate is what keeps an idle engine (silent by design) safe —
   never arm on silence alone. A stall kill clears a stale memory-guard
   `armed:` state so it cannot frame another process; it leaves an in-flight
   `termed:` alone.

## Workflow (the order that worked)

1. Read the full current source first (`tools/gpu_monitor.py`,
   `safe-build.sh`) plus `docs/2026-10-25-guard-concurrency.md` for context.
   `git log --oneline -8` and `git show <sha>` to review recent commits.
2. Implement the change with `python3 -m py_compile` after each edit.
3. Sandbox-test FIRST in `/tmp` (never on live state): load the module with
   `importlib.util.spec_from_file_location`, redirect `RSS_BASE_PATH` /
   `STATE_PATH` / `NVRM_PATH` into a `tempfile` dir, inject `_rss_map` /
   `_starttime` / fake `subprocess.run`, spawn throwaway `sleep 60` children
   for real-pid cases (victim name lookup reads `/proc/<pid>/status`, so
   synthetic pids die at that step).
4. Port the suite into the repo as `tools/test_gpu_monitor_<topic>.py`
   (hermetic, CI-discovered — see the `maya-hermetic-tests-ci` skill).
5. Deploy: `cp tools/gpu_monitor.py ~/gpu_monitor.py && cmp -s` to verify.
   **No restart ever needed** — the timer spawns a fresh process per sample.
6. Verify live: next lines of `~/gpu_monitor_log.txt` still land, baseline
   file rewritten in the expected format, `N:` field sane.
7. Commit (lowercase narrative subject, body explaining defect → mechanism →
   tests; cite the sandbox checks by name) and push `fork main` + `private main`.
8. Confirm CI green (see `maya-hermetic-tests-ci`).

## Porting a technique from an external watchdog

The NV_ERR_NO_MEMORY journal watch was ported from MiaAI Lab's memwatch.sh
(https://github.com/MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark/blob/main/files/memwatch.sh,
AGPL-3.0-or-later). Rules that applied:

- **Reimplement from scratch, take no code** — the one-shot-process model
  demands a different mechanism anyway (epoch cursor file vs in-process
  datetime, per-sample windows vs fixed cadence).
- **Credit the source in `NOTICE`** with the license and URL, and in the
  code's header comment. Update `NOTICE` in the same commit.
- Adapt the lesson, not the line: memwatch's boundary-second fix
  (`journalctl --since` inclusive at second granularity) and its
  MemFree-gate reasoning translate, its docker-container specifics do not.

## Hard rules (bit us twice)

- Never `pkill -f <pattern>` from a tool shell — it matches the shell's own
  command line. Kill by exact PID / `pgrep -x`.
- `sudo` needs a password here; `systemctl restart gpu_monitor` is never
  needed (and cannot be run unattended anyway).
- The engine (`build/maya --serve`, :8009) is never restarted for guard
  changes — guard and engine are independent processes.
