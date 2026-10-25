---
name: maya-hermetic-tests-ci
description: Use when writing or porting tests for maya repo tooling (tools/test_*.py) so they run green in CI, when checking CI status of a push with gh, or when diagnosing a CI failure/flake. Covers the repo's unittest conventions, the hermetic rules for guard tests, the spawned-child cleanup order, flaky-pid pitfalls, and the gh api workflow commands (fork has Actions disabled; the private repo runs CI).
---

# Hermetic tests and CI for the maya repo

CI: `.github/workflows/tests.yml` — runs on every push, ubuntu-latest x86_64,
no GPU. The tools suite is discovered with:

```
python -m unittest discover -s tools -p "test_*.py"
```

Run that exact command locally before pushing. CI reality on the remotes:
`fork` (xcpatr9231975/project-maya) has **Actions disabled (0 runs)**; the
**private repo (xcpatr9231975/maya-dgx-spark) runs CI**. Check status there:

```
gh api 'repos/xcpatr9231975/maya-dgx-spark/actions/runs?per_page=6' \
  --jq '.workflow_runs[] | "\(.head_sha[0:7]) \(.status) \(.conclusion // "-")"'
```

A failed run: get the job id, pull its logs, rerun just the failed job:

```
runid=$(gh api 'repos/.../actions/runs?per_page=1' --jq '.workflow_runs[0].id')
failed=$(gh api "repos/.../actions/runs/$runid/jobs" --jq '.jobs[] | select(.conclusion=="failure") | .id')
gh api "repos/.../actions/jobs/$failed/logs" | grep -aE 'FAILED|AssertionError|Traceback'
gh run rerun -R xcpatr9231975/maya-dgx-spark "$runid" --failed
```

A docs-only commit failing CI = a flake (proven 2026-10-25: `serve/test_mcp.py`
"dying on purpose" assertion failed once, passed on rerun). Confirm by
diffing what the commit touched before investigating anything else.

## Writing tools/test_*.py (repo conventions)

- unittest classes, module docstring naming what is tested and the run
  command: `python -m unittest tools.test_<name>`.
- `tempfile.TemporaryDirectory` + `addCleanup(tmp.cleanup)` for scratch.
- Load script-modules (which are not package-importable and hardcode home
  paths) via importlib, then redirect their path constants in `setUp`:

  ```python
  _spec = importlib.util.spec_from_file_location(
      "gpu_monitor_under_test", Path(__file__).with_name("gpu_monitor.py"))
  gpu = importlib.util.module_from_spec(_spec)
  _spec.loader.exec_module(gpu)
  # setUp: gpu.RSS_BASE_PATH = os.path.join(tmp.name, "rss_base") ...
  ```

- Mock external calls with `unittest.mock.patch` — e.g. fake `journalctl`
  with `patch("subprocess.run", return_value=SimpleNamespace(returncode=0,
  stdout=...))` or `side_effect=subprocess.TimeoutExpired(...)`.

## Hard-won pitfalls

- **addCleanup is LIFO.** For a spawned child, register
  `addCleanup(c.wait)` BEFORE `addCleanup(c.kill)` — the reverse order runs
  `wait` on a live child and hangs the whole suite (silent, buffered, no
  output). A hung suite with no output at all = check cleanups first.
- **Never assert on a synthetic pid you did not spawn.** Code under test
  reads `/proc/<pid>/status` for the victim's name; a made-up pid dies at
  that step and the test fails for the wrong reason. Spawn a real `sleep 60`
  child and monkeypatch identity (`patch.object(gpu, "_starttime", ...)`)
  when you need controlled identities.
- **Flaky-by-luck assertions**: `assertIn(SOME_PID, ...)` passes only if a
  real process happens to own that pid — use a second spawned child for
  multi-entry checks.
- Guard tests must never touch live state: always redirect
  `RSS_BASE_PATH`, `STATE_PATH`, `NVRM_PATH`, and never run `main()`.
- File-handle hygiene: `open(...)` without `with` triggers ResourceWarnings
  that pollute CI logs — close what you open in tests.
- Naming/commit style: lowercase narrative subject, body listing the checks
  by name; reference the sandbox suite the file came from.

## Reference implementations

`tools/test_gpu_monitor_baseline.py` (starttime-bound RSS baseline: delta /
reuse / dead / unlisted / legacy / below-min / round-trip / fresh-after-death
/ exclusions) and `tools/test_gpu_monitor_nvrm.py` (cursor discipline:
first-run init, success advances, timeout/failure hold, window re-coverage,
clock-skew). Both hermetic, green in CI since 2026-10-25 (`67745c3`).
