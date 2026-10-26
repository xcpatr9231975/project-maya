# Why the 256K config dies: the budget, the breach, and the four ways to stop it

Date: 2026-10-26
Status: diagrams for the two memory kills of 2026-10-25/26 (20:42 and 07:36) and the
prevention layers. Companion to `docs/STABILITY-1.md` (the headroom/chunk parameters,
now fully measured, incl. the 115K prompt at 32/36/40) and
`docs/2026-10-25-driver-580-residual-kernels.md` (the unrelated kernel residual).
Affects: `maya-maya-s-v2-iq2_xxs-256k.json`, `src/core/glm_fast_path.cu` (pool sizing),
`src/core/glm_prefill.cu` (chunk borrow), `tools/needle_bench.py` (where the big prompts
come from).

## The problem in one paragraph

A GB10 has no VRAM: the CUDA pool *is* system RAM, and `MemAvailable` cannot see the
driver's pages. The expert pool is sized **once at engine start** from
`MemAvailable − headroom − 1 GB`, and the prompt path's own borrow is printed at boot but
never reserved. What is left after the pool is the only thing that can absorb a large
allocation, and on the 256k config that number has been **≈ 5 GiB usable above the
3,072 MiB kill floor**. A single ≈ 7 GiB allocation during serving is therefore enough to
breach it — which is what happened at 07:36, with a 56-token prompt in flight. The guard
turns that breach into a logged SIGKILL; without the guard the driver's own error path
deadlocks and the box hard-locks.

---

## 1. Where the memory goes (and what is actually left)

```mermaid
flowchart TD
    subgraph BOOT["Sized ONCE at engine start — never re-checked while serving"]
        M["MemAvailable at boot: 98.2 GB"]
        H["headroom 32 GB<br/>(MAYA_GLM_RAM_HEADROOM_GB)"]
        R["reserve 1 GB"]
        P["Expert pool budget = 65.20 GB<br/>(MemAvailable − headroom − reserve)"]
        M --> P
        H --> P
        R --> P
    end

    subgraph DEV["Device: 119.07 GB total — GB10 unified memory, the pool IS system RAM"]
        PRE["Pre-pool footprint: 43.75 GB<br/>dense weights 6.86 · state/KV 0.92 · activations 0.06<br/>context, MTP, scratch, prompt path ≈ 36.9"]
        POOL["Expert pool: 65.20 GB"]
        FREE["Free at boot: 10,796 MiB"]
        PRE --> FREE
        POOL --> FREE
    end

    P -. "the pool takes this" .-> POOL

    FREE -->|"− serving state ≈ 2.8 GiB"| SS["Steady state under load<br/>8.0 GiB (measured, flat for 6 min)"]
    SS -->|"− kill floor 3,072 MiB"| UH["USABLE HEADROOM<br/>≈ 4.9 GiB  ←  the real stability parameter"]
    UH -->|"a sudden ≈ 7 GiB allocation needs more than this"| X["device-free 982 MiB<br/>→ NV_ERR_NO_MEMORY<br/>→ guard SIGTERM → SIGKILL"]

    style UH fill:#ffd9d9,stroke:#c00,stroke-width:2px
    style X fill:#ffb3b3,stroke:#900,stroke-width:2px
```

The two numbers that decide everything: **free-at-boot** (set by headroom) and **the
largest single allocation the engine will attempt** (set by chunk size, KV growth and
session state). Everything else is fixed cost.

---

## 2. What happened at 07:36 (and what the guard prevented)

```mermaid
sequenceDiagram
    participant E as Engine (256k config)
    participant D as CUDA driver
    participant G as gpu_monitor guard
    participant B as The box

    Note over E: 5879-token generation in flight<br/>F: flat 8.0 GiB for 6 min, 95% util, prompts 56–72 tokens
    E->>D: allocate ≈ 7 GiB (no headroom pre-check)
    alt guard in the path — what actually happened
        D-->>E: NV_ERR_NO_MEMORY
        Note over D: device-free 982 MiB (first refusal on 580.178.04)
        G->>G: 07:36:37 LOW 982 MiB → armed
        G->>E: 07:36:48 SIGTERM (NVRM signal)
        E-->>G: still alive after 30 s (mid-generation)
        G->>E: 07:37:22 SIGKILL
        E-->>B: exit −9, 76.4 GB pool released
        Note over B: SURVIVED: logged kill, journal continuous, no freeze<br/>new engine starts with MemAvailable 90.5 GB → smaller pool
    else guard absent or too slow — the 10-24 / 10-25 pattern
        D-->>D: driver error path deadlocks
        Note over B: HARD LOCK: console silent, no shutdown sequence<br/>SBSA watchdog or a manual power-cycle is the only way back
    end
```

The guard is the only thing between a memory breach and a hard lock. It has now caught
every breach on this box — but catching them is not the same as not having them.

---

## 3. The two events, side by side

| | 2026-10-25 20:42 | 2026-10-26 07:36 |
|---|---|---|
| Config | headroom 32, `--prefill auto` (32768) | headroom 32, `--prefill 16384` |
| Prompt borrow | 6,775 MB | 4,417 MB |
| Free at boot | 6.3–6.6 GiB | 10,796 MiB |
| Trigger | 115,201-token prompt prefill | ≈ 7 GiB allocation mid-generation (5879-token request) |
| device-free floor hit | 2.50 GiB | **982 MiB** + `NV_ERR_NO_MEMORY` |
| Guard outcome | SIGTERM → SIGKILL | SIGTERM → SIGKILL |
| Box | survived | survived |

Reducing the chunk size moved the failure from "prompt-driven" to "allocation-driven" and
made the breach *deeper* (982 MiB vs 2.5 GiB), because the baseline it breached from was
higher. The chunk knob buys margin; it does not remove the failure mode.

---

## 4. The four prevention layers

```mermaid
flowchart LR
    REQ["Request arrives<br/>(prompt + session)"] --> L0

    subgraph L0["Layer 0 — do not create the demand  (cheapest, ~5 min, no engine risk)"]
        B1["needle_bench: cap --lengths<br/>to what the budget can take"]
        B2["keep bench traffic out of the<br/>session store — no 115K replay"]
    end

    L0 --> L1

    subgraph L1["Layer 1 — admission gate  (the real fix, ~20 lines)"]
        G1{"cudaMemGetInfo free<br/>≥ demand + 3,072 MiB + slack?"}
        G2["no → clean refusal in milliseconds<br/>instead of a SIGKILL 30 s later"]
        G1 -->|"yes"| OK
        G1 -->|"no"| G2
    end

    OK --> L2

    subgraph L2["Layer 2 — make the demand smaller"]
        C1["--prefill 16384 → borrow 4.4 GB<br/>8192 → ≈ 2.2 GB"]
        C2["--kv int8 · smaller --max-context"]
    end

    L2 --> L3

    subgraph L3["Layer 3 — give it room"]
        H1["headroom 40 → free 14.4–18.5 GiB<br/>safe, but disk-bound: 16 reads/token<br/>the fallback when --prefill cannot shrink"]
    end

    L3 --> SVC["Serving — decode ≈ 13 tok/s"]

    style G1 fill:#d9ead3,stroke:#060,stroke-width:2px
    style G2 fill:#fff2cc,stroke:#960,stroke-width:2px
```

| Layer | Stops 20:42? | Stops 07:36? | Cost |
|---|---|---|---|
| 0 — bench cap + no session replay | yes (removes the source) | partially (removes the replayed 115K state) | none; loses the long-context bench |
| 1 — admission gate on **every** large allocation | yes | **yes** | ~20 lines in `generate.cpp`, following the refusal idiom already at `glm_fast_path.cu:1178` |
| 2 — smaller prefill chunks | yes | no (this was not a prefill) | some prefill throughput |
| 3 — headroom 40 | yes | yes, with margin | **disk-bound decode** (16 reads/token vs 4.7) — the fallback when the chunk cannot shrink, not a cheap option |

Layer 1 is the only one that is source-agnostic: it does not care whether the demand comes
from a needle bench, a pasted document, a replayed session or an in-generation allocation
the engine makes on its own behalf. Layers 0 and 2 reduce how often it has to say no;
Layer 3 raises how much it can say yes to.

## Where the 115K prompt stands now

The shipped config (**headroom 32 + `--prefill 16384`**) has been measured against a real
115K-token prefill, and it holds:

| config | pool budget | free at boot | measured dip during the 115K prefill | vs 3,072 MiB floor |
|---|---|---|---|---|
| 32 + `auto` (32768) | 65.2 GB | 7.6 GiB | **2.5 GiB** | −0.6 GiB → **killed at 20:42** |
| **32 + `16384` (shipped)** | 65.4 GB | 10.5 GiB | **9.8 GiB** | **+6.7 GiB → safe**, 13.4 tok/s, 4.7 reads/token |
| 32 + `8192` | 65.4 GB | 10.5 GiB | 9.7 GiB | +6.6 GiB → safe, +12 % prefill time |
| 36 + `auto` | 61.2 GB | 10.3 GiB | ~8.1 GiB | +5.1 GiB → safe, disk-bound (16.8 reads/token) |
| 40 + `auto` | 57.1 GB | 14.4–18.5 GiB | ~16.1 GiB | +13 GiB → safe, disk-bound (16.0 reads/token) |

So the 115K prompt is **no longer the failure mode** — Layer 2 already closed it, and the
full run is in `REGRESSION.md`. What remains open is the 07:36 class: a ≈ 7 GiB allocation
the engine makes on its own behalf during serving, against a steady-state `F:` of ~8 GiB.
No chunk size or headroom value is measured against that case, which is why Layer 1 is the
one that still has to be built.

**The rule the gate enforces**, and the number to watch:

```
cudaMemGetInfo free  ≥  next allocation estimate  +  3,072 MiB  +  ~1 GiB slack
```

`maya serve: CUDA0: N MiB of VRAM free with everything loaded` is the boot-time value;
the steady-state value is what the monitor's `F:` field reports under load.

---

## 5. Cross-references

- `docs/STABILITY-1.md` — the headroom and prefill-chunk parameters, the fully measured
  table (incl. the 115K prompt at 32/36/40), and how to re-measure after any change.
- `REGRESSION.md` — the run behind those measurements.
- `.opencode/skills/dgx-spark-memory-forensics` — reading `F:` / `N:` / `A:`, the
  3,072 MiB floor, and why `NV_ERR_NO_MEMORY` is a warning shot rather than a recoverable
  error.
- `.opencode/skills/gb10-hardlock-triage` §5 — the expert-pool footprint hypothesis these
  two events refine: it is not the pool *size*, it is the **unusable remainder** above the
  floor.
- `docs/MEMORY_WATCHDOGS_DEEP_DIVE.md` — the guard that converts a breach into a logged
  kill instead of a power-cycle.
