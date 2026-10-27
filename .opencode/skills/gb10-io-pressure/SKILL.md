---
name: gb10-io-pressure
description: Use when reading, watching, or acting on /proc/pressure/io on the DGX Spark GB10 (carlosp-nvidia-dgx) running the maya engine — deciding whether io pressure is the expert-streaming baseline or a write-path stall, naming who is reading, arming the fsync canary, and the thresholds that turn io pressure into an alarm before a hard lock.
---

# GB10 io-pressure triage

Repo: `~/projects/maya`. The I/O sibling of `dgx-spark-memory-forensics`
(memory class) and the live witness for `gb10-hardlock-triage` (the freeze
class). Full explainer, measured baselines and the anticipate/prevent plan:
`docs/PSI-IO.md` in the repo. This skill is the procedure.

## 1. Read the meter

```bash
cat /proc/pressure/io
# some avg10=6.36 avg60=7.26 avg300=3.41 total=186236697
# full avg10=6.15 avg60=7.09 avg300=3.35 total=185386172
```

- `some` = at least one non-idle task was stalled on I/O that fraction of the
  window; `full` = **all** non-idle tasks were stalled simultaneously.
- `avg10/60/300` are percentages over the last 10/60/300 s; `total` is
  cumulative stall microseconds. Read `avg10` for now, `avg60` for trend.
- PSI-io measures **stall time, not throughput**: 6 GB/s of async reads can
  stall less than 100 MB/s of latency-bound sync reads. Pair it with the
  monitor's `U:` and the engine's `glm stat` disk line before judging.

## 2. This box's measured baselines (2026-10-26, verified live)

| Regime | `some/full avg10` |
|---|---|
| True idle (U:0 %) | **0.00 / 0.00** |
| Decode steady (U:95 %, ~10.8 tok/s) | **5.4-10.4 %** — the baseline under load |
| 1.65 GB/s async burst | 3.6 / 3.6 |
| Prefill copy-lane burst (5-6 GB/s) | not yet sampled — capture it (§6) |
| Memory PSI, any regime | 0.00 — memory is never the pressure here |

`some ≈ full` within ~0.5 in every healthy reading: one purpose, one NVMe, all
tasks stall together. Divergence means a *new* staller — name it (§5).

## 3. Decision table

| Reading | Verdict | Do |
|---|---|---|
| `full avg10 < 1 %` at U:0 % | idle baseline | nothing |
| `full avg10 2-20 %` with U > 50 % | expert streaming — **expected** | nothing; never alarm on this |
| `> 20 %` for 3+ samples, U > 50 % | heavier than measured | WARN: name the reader, check `F:`, keep sampling |
| `> 40 %` for 2+ samples, or `full ≈ some` climbing with **U < 20 %**, or canary > 2 s | write-path-stall class — the pre-freeze signature | ALARM: snapshot (§4) before anything else |
| D-state tasks > 5 for > 60 s | stuck I/O path | ALARM (same snapshot) |
| `> 60-80 %` | near-total stall | capture what can be captured; the box is about to wedge |

## 4. The ALARM snapshot, in order

```bash
cat /proc/pressure/io                                  # the reading itself
ps -eo state,pid,comm,wchan:32 | awk '$1=="D"'         # who is in D state, where
for p in $(ps -eo pid,comm | awk '$2 ~ /^jbd2\// || $2=="maya" {print $1}'); do
  echo "== $p $(cat /proc/$p/comm 2>/dev/null)"; cat /proc/$p/stack 2>/dev/null | head -5; done
# (the journal thread is named jbd2/nvme0n1p2-8 - match by prefix, not equality)
grep -E "^(Dirty|Writeback):" /proc/meminfo            # writeback backed up?
grep nvme0n1 /proc/diskstats                           # in-flight, await ticks
tail -40 ~/gpu_engine_tail.txt                         # what the engine last said
tail -20 ~/gpu_monitor_log.txt                         # the F:/N:/U: context
ls -la ~/gpu_reports/ | tail -5                        # did the newest report write?
```

The snapshot exists to be taken **before** hung_task panics (120 s) or the box
wedges — after either, only the vmcore or a power-cycle answers.

## 5. Name the reader (per-process I/O delta)

```bash
python3 - <<'PY'
import os, time
def snap():
    out={}
    for pid in os.listdir('/proc'):
        if not pid.isdigit(): continue
        try:
            d={}
            for line in open(f'/proc/{pid}/io'):
                k,v=line.split(': '); d[k]=int(v)
            name=open(f'/proc/{pid}/comm').read().strip()
            out[pid]=(name, d['read_bytes'], d['write_bytes'])
        except (OSError, KeyError, ValueError): pass
    return out
a=snap(); time.sleep(3.0); b=snap()
rows=[]
for pid,(name,rb,wb) in b.items():
    dr=rb-a.get(pid,(0,0,0))[1]; dw=wb-a.get(pid,(0,0,0))[2]
    if dr+dw>10_000_000: rows.append((dr+dw,dr,dw,pid,name))
rows.sort(reverse=True)
for t,dr,dw,pid,name in rows[:8]:
    print(f"{t/3/1e6:8.1f} MB/s  (r {dr/3/1e6:7.1f} w {dw/3/1e6:7.1f})  pid {pid:>7}  {name}")
PY
```

Verified live 2026-10-26: an apparent 1.06 GB/s "idle" reader resolved to the
maya engine itself — the user's agent was mid-request; the monitor's `U:0%`
sample had landed between decode phases. Always pair with `U:` and the engine
log before calling a reader phantom.

## 6. What is still unmeasured (capture on the next prefill)

- PSI-io **during the prefill copy-lane burst** (24-28 GB at 5-6 GB/s): the one
  number the thresholds await. Sample every 2 s across a known prefill and
  record it in `docs/PSI-IO.md` §3.
- PSI-io during a **slot save** (the 0.41 GB boundary write).

## 7. Anticipate / prevent (what exists, what is next)

- **Armed 2026-10-26**: prompt cap `MAYA_MAX_PROMPT_TOKENS=49152`, admission
  cooldown `MAYA_ADMIT_COOLDOWN_S=4`, `hung_task_panic=1` + `softlockup_panic=1`,
  kdump chain validated (panic → 1.56 GB vmcore → reboot in 76 s). The SBSA
  watchdog is armed but cannot catch a wedge that leaves PID 1 petting.
- **Next**: the monitor patch (io PSI in the sample line, the fsync canary,
  diskstats, D-count — `docs/PSI-IO.md` §6), the cron watch thresholds (§3), and
  the `hung_task_timeout_secs` 120 → 60 policy call.
- **Never**: restart the engine or kill anything on io pressure alone; the
  decode band is the designed cost of streaming experts.

## 8. Cross-references

- `docs/PSI-IO.md` — the full explainer and the anticipate/prevent plan.
- `gb10-hardlock-triage` — the freeze class this witnesses; its journald-first
  timeline was corrected on 2026-10-26 (writeback artifacts, not a layered stall).
- `dgx-spark-memory-forensics` — the memory sibling (`F:`/`N:` trust order, the
  estimator, step-vs-leak); memory PSI stays 0.00 here even under full load.
- `tools/gpu_monitor.py` `psi_memory()` — the memory-only PSI the monitor patch
  extends.