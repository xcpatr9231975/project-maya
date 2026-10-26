# Enhancements: the gaps the 580.178.04 regression left open

Date: 2026-10-26 · Box: DGX Spark GB10 (`carlosp-nvidia-dgx`) · Repo: `~/projects/maya`
Companion to [REGRESSION.md](REGRESSION.md) (the measurement and the two fixes) and
[docs/STABILITY-1.md](docs/STABILITY-1.md) (the 256k parameters).
Status: **A** and **B** need no rebuild; **C** needs one translation unit; **D** needs
`sudo`. (The fourth gap, admission control, is closed — see
[REGRESSION.md](REGRESSION.md).) Each item carries its own evidence, its own change and its own acceptance test.

## 0. Why the parameters are not enough

The headroom is computed **once, at boot**:

```
pool budget = MemAvailable − headroom − 1 GB reserve        (src/core/glm_fast_path.cu:1533)
```

The footprint the engine holds **after** boot is not static. Measured over the 28 engine
starts in the 2026-10-25/26 log, the memory in use *before* the pool was sized ranged:

| | min | max | spread |
|---|---|---|---|
| in use before the pool (27 healthy starts) | 33.47 GB | 52.52 GB | **19.05 GB** |
| of which `other (context, MTP, scratch, prompt path)` | 25.64 GB | 44.69 GB | 19.05 GB |
| the failing start (2026-10-26 07:36) | — | 113.52 GB | — |

A config that is comfortable at boot can therefore be under water later. The growth is
the conversation slots:

```
glm slots: saved a 239544-token conversation (3.19 GB in 1.2 s); 1 kept, 3.19 GB
glm slots: saved a 115497-token conversation (1.62 GB in 0.6 s); 1 kept, 1.62 GB
...  up to: 4 kept, 5.36 GB
```

The 07:36 start is the extreme: the previous engine was still resident, so
`MemAvailable 26.37 GB` and `pool budget 0.00 GB` — the engine refused to start
("no room for the expert pool: 0 slots a layer, at least 8 needed"). That one is the
restart path, fixed (see [REGRESSION.md](REGRESSION.md) §"The restart path").

What is still open is the **retention** side: the engine keeps conversations it may not
need, and nothing caps that against the headroom it was sized with. Two of today's fixes
already bear on this — the admission gate (a request is not started when the device
cannot hold it) and one engine at a time — but a *long-lived* engine that grows its slots
moves its own floor, and that is what the next four items are about.

## 1. Enhancement A — the session store's budget (config only, no rebuild)

The engine already has the knobs (`src/program/generate.cpp:1417-1449`):

| knob | default | what it does |
|---|---|---|
| `STRATA_GLM_SLOTS` | 4 | how many conversation slots are kept (0 = off) |
| `STRATA_GLM_SLOT_MIN` | 1024 | the shortest conversation worth slotting |
| `STRATA_GLM_SLOT_GB` | 16 | the disk budget for them (never below 8 GB free) |
| `STRATA_GLM_SLOT_RAM_GB` | 0 (off) | keep them in RAM instead, capped at that many GB |
| `STRATA_GLM_SLOT_KEEP` | off | the slots outlive the engine (the folder is not emptied at start) |
| `STRATA_GLM_SLOT_DAYS` | 7 | how long a kept slot file lives |

The config's `env` block carries them — `tools/maya_names.py` maps `MAYA_*` to the
`STRATA_*` names the engine reads, and the config wins over any shell export.

**The change:** `"MAYA_GLM_SLOTS": "1"` (or 2) in the 262144 config's `env`.

**The trade-off:** a conversation that is not slotted re-reads its prompt when it comes
back — slower, not wrong. One chat at a time pays nothing; several interleaved chats pay
it every turn.

**How to measure:** the `glm slots: saved … ; N kept, X GB` line (the number kept and
their bytes), and the `VRAM before the expert pool: … other (context, MTP, scratch,
prompt path) N` line across a long session. The acceptance test is the spread of that
bucket over a session: it must stay inside the headroom's slack (today: 19 GB of spread
against a 3 GiB margin).

**Cost:** a config edit and an engine restart (the engine reads its env at start). No
rebuild, no recompile.

## 2. Enhancement B — the guard's two rules (no rebuild, no restart)

`tools/gpu_monitor.py` is a Python script the systemd timer re-runs every ~10 s, so a
change to it is deployed by copying the file — the next sample uses it.

### B1 — the NVRM rule kills the wrong victim

**The evidence** (2026-10-26 07:36:48): a *newcomer* hit `NV_ERR_NO_MEMORY`, and the
guard SIGTERMed the **resident** engine (pid 243594, 76.4 GB) — because the refusal is a
device-wide signal with no process attached, and the guard picks the biggest consumer.

**The rule to change** (`memory_guard`, `tools/gpu_monitor.py`): prefer the process whose
memory **grew** since the last sample — the guard already tracks RSS deltas (it used
them to attribute the 15:11 and 15:15 kills) — and fall back to the biggest consumer. A
refusal during a start is exactly the growth case; a refusal under a steady workload is
not.

### B2 — the stall detector false-positive

**The evidence** (2026-10-25 20:51:57 and 20:57:33): two healthy engines at **95% GPU**
were SIGTERMed because the file the guard watched had been quiet for 95 s. The file was
the wrong one: `tools/bench_tokens.py` writes its progress to stdout, and the guard's
log-path resolution picked up the engine log that the bench was not writing to.

**The rule to change** (two places in `tools/gpu_monitor.py`):

1. resolve the engine's log from the engine's **own stdout** (`/proc/<pid>/fd/1` → the
   file it writes to), not from a name that may belong to another harness;
2. treat "the GPU at ≥ 90% **and** the engine's CPU time still growing" as activity. The
   utilization gate alone is not enough — 95% passed it, and a stalled engine does not
   hold 95% of the GPU *and* keep burning CPU.

## 3. Enhancement C — a device-side cap on the slots (one translation unit)

**The policy (measured 2026-10-26, for the implementation):** the caps must be *tokens AND
bytes*, LRU-evicting, because a count alone ratchets: one 239,544-token conversation is
**3.19 GB** of slot state, so `STRATA_GLM_SLOTS=1` still leaves 3.19 GB held, and two of
them (6.4 GB) take headroom 40's margin to the floor. The measured bytes per token of
slot state: **13.3 B/token** (239,544 → 3.19 GB), 14 B/token (115,201 → 1.61 GB),
19.5 B/token (94,915 → 1.85 GB) — use **20 B/token** as the safe figure.

The binding constraint is the admission gate's demand, which is **fixed** at
`borrow + floor` = 7489 MiB on this config: the slots must keep
`device_free ≥ 7489 + slack`. With headroom 40 the fresh margin is 17.5–25.6 GiB, so:

```
slot_bytes ≤ device_free_fresh − 7489 MiB − 2048 MiB slack ≈ 8–16 GB
```

A conservative default: **`STRATA_GLM_SLOT_BUDGET_GB = 4`** (one full-context
conversation plus change) and **`STRATA_GLM_SLOT_TOKENS = 262144`** (one full-context
conversation) — LRU across both. A conversation bigger than the budget is not kept at
all: it re-reads its prompt next time (slower, not wrong). The same caps bound what is
**restored at start** (the restore path allocates the same state).

Acceptance: after N conversations of any size, `glm slots: … N kept, X GB` has
`X ≤ 4 GB` and `N ≤ 1 for a 239k conversation`, and `F:` never drops below
`7489 MiB + 2048 MiB` while serving.

Only if A does not close the gap: the engine would refuse to **keep** a slot whose bytes
would take the device under `free − floor` — the same rule the admission gate now uses
for *admission*, applied to *retention*.

| | |
|---|---|
| the code | `src/program/generate.cpp` (the slot save/keep path, ~1441-1560) |
| the knob | `STRATA_GLM_SLOT_BUDGET_GB` — the bytes the slots may hold; default 4 |
| the cost | one translation unit and a link — the 2026-10-26 rebuild of the same file took **50 s** (not the whole project) |
| the risk | a chat that cannot be slotted re-reads its prompt (slower, not wrong) |
| the test | the rule as a pure function (a unit test in the repo's `unittest` style) plus a live session that would have grown the footprint |

**The gate's own end, closed in `cc7e9ad`:** the gate could not tell "the device is full
of someone else's memory" (waiting helps) from "the device is full of my own held state"
(waiting never helps) — on 2026-10-26 it burned `STRATA_GLM_ADMIT_WAIT_S` = 120 s on
eight refusals in a row at `F: 4.03 GiB`. Now it refuses after
`STRATA_GLM_ADMIT_STALL_S` (30 s) and the message names the cause. The server side
waits for the *memory* to settle and retries the start the gate cannot
(`device_free_mib()`, the settle-wait, the retry on "no room for the expert pool") —
`d4c5f4d`.

## 4. Enhancement D — the stale 6.14/6.11 kernels (sudo)

The driver upgrade removed the old kernels' `nvidia.ko`, so booting one of them gives a
box with **no GPU driver at all** (it looks like a hardware failure). The GRUB menu is
hidden, so it needs a deliberate choice to reach — but it should not be there.

```bash
sudo apt purge -y \
  linux-image-6.14.0-1013-nvidia linux-image-6.14.0-1015-nvidia \
  linux-image-6.11.0-1014-nvidia \
  linux-modules-nvidia-fs-6.14.0-1013-nvidia linux-modules-nvidia-fs-6.14.0-1015-nvidia
sudo update-grub
```

Do **not** touch `linux-image-7.0.0-1019-nvidia` or `linux-modules-nvidia-hwe-24.04`.
The rollback path (the 580.95.05 set) stays in `~/rollback-gb10-580/` — see
[docs/2026-10-25-driver-580-residual-kernels.md](docs/2026-10-25-driver-580-residual-kernels.md).

## 5. The order, and what each one costs

| # | item | rebuild | restart | risk | acceptance |
|---|---|---|---|---|---|
| A | the slot budget (a config knob that already exists) | no | the engine | interleaved chats re-read their prompt | the footprint's spread over a session stays inside the headroom's slack |
| B | the guard's two rules | no | none | the victim choice can now pick a different process | the guard kills the process that grew, not the biggest one; a busy engine is never killed for silence |
| C | the device-side slot cap | one TU (50 s) | the engine | the same trade-off as A, decided at runtime | the slots never take the device under `free − floor` |
| D | the stale kernels | no | a reboot | a wrong purge leaves no driver at all | `dpkg -l | grep -E 'linux-(image|modules-nvidia)'` lists only 7.0.0-1019 |

A and B are independent of each other and of C; D is independent of all three.

## 6. What is deliberately *not* on this list

- **The headroom parameters.** They are a schedule, not a fix: the gate (admission
  control) is the fix, and the schedule only says how much room to leave.
- **The driver upgrade.** It is not a fix for the freeze — no freeze since, but that is
  n=1 at ~1h45m under heavy load, with a different pool from the runs that died.
- **Tuning the stall guard's 90 s window.** The stall that killed two healthy engines was
  the wrong file being watched, not a window that is too short.

## 7. Cross-references

- [REGRESSION.md](REGRESSION.md) — the regression on 580.178.04, the restart-path defect
  and the admission gate, with every number this page leans on
- [docs/STABILITY-1.md](docs/STABILITY-1.md) — the 256k parameters and what a prompt
  costs them
- [docs/MEMORY_WATCHDOGS_DEEP_DIVE.md](docs/MEMORY_WATCHDOGS_DEEP_DIVE.md) — the guard
  (the floors, the victim selection, the journal watch) in full
- [docs/2026-10-25-driver-580-residual-kernels.md](docs/2026-10-25-driver-580-residual-kernels.md)
  — the upgrade, the rollback set, and the stale kernels (item D)
- `.opencode/skills/maya-guard-hardening` — the guard's invariants and the
  deploy-without-restart rule (item B)
- `.opencode/skills/dgx-spark-memory-forensics` — reading `F:` / `N:` / `A:`, and the
  difference between a step, a leak and a transient
