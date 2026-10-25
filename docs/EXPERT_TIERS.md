# Feeding a 321B model from 119 GB: Maya's expert tiers

**What this is.** A plain-language walk through how Maya runs GLM-5.3-Flash — 321B parameters, about 18B
active per token — out of a single 119 GB pool of unified memory on one DGX Spark, and why that is a different
*infrastructure* from loading a GGUF and hoping it fits. Every claim below is either measured on a live run
(numbers marked **[measured]**, with the run they came from) or cited to the source that implements it.

**Who it is for.** Anyone who has watched a 3-bit GGUF of this model sit "not responding" on a Spark and wants
to know what Maya does instead.

---

## 0. The problem, in one paragraph

The model is 321B parameters; each token routes through about 18B of them. At 3 bits a weight that is roughly
100 GB of expert bytes plus a few GB of dense weights — call it 91 GB, which is what the shipped Maya-S pack
measures on disk. The box has 119 GB of LPDDR5x, and that memory is **the same pool for the CPU and the GPU**:
`docs/DGX_SPARK.md:293` — *"the device allocates from the same RAM the CPU uses"*. There is no discrete VRAM to
add, and no second pool to spill into. So the engineering question is not "does it fit" (it does, barely) but
**"which parts of it can afford the fast access path, and can the rest be fetched before they are wanted?"**

## 1. Why one blob fails

A generic runtime treats a GGUF as one file of equal citizens. Either the weights fit in memory or they do not;
when they do not, whole layers stream per token. At ~91 GB of weights on 119 GB, the model "fits" — and leaves
roughly 19 GB for the KV cache, activations and the MoE working set. A real context is several GB on its own, so
the first long prompt pushes the box into the streaming regime, where every token pays for weights it will never
use again. That is the wall: not capacity, but the absence of any notion that **some experts matter more than
others**.

## 2. One pool, three access paths

The three "tiers" are not three places. They are three **allocation modes of the same physical memory**, with
very different costs for the GPU to reach:

| tier | allocated as | how the GPU reaches it | cost per expert |
|---|---|---|---|
| **VRAM** | `cudaMalloc` — device memory | an ordinary global load from the kernel | ~0 (already resident) |
| **RAM** | `cudaHostAlloc` / `cudaHostRegister` — pinned host | through the host mapping over NVLink-C2C | **0.127 ms** [measured] |
| **disk** | the GGUF shards on the NVMe | an `O_DIRECT` read by a reader thread | **~1.2 ms** [measured, derived] |

The 0.127 ms and the 0.161 ms CPU figure are printed by the engine at startup
(`src/core/glm_fast_path.cu:3317`); the ~1.2 ms disk figure is the disk time per read from a live 256K run
(28.53 ms of disk waits across 23.50 reads per token). The tiers are **exclusive** — an expert is in exactly one
mode at a time, because duplicating it would cost twice the capacity for no gain
(`src/core/glm_fast_state.hpp:585`).

That exclusivity is also why the tier split is a zero-sum negotiation *on this box* and not on a discrete-GPU
machine: growing the RAM tier takes bytes from the VRAM tier, and a VRAM-resident expert is strictly better than
a RAM-resident one. On an H100 with 80 GB of VRAM and 504 GB of system RAM the two pools never compete, which is
why the README's 2x H100 entry runs 1M context at 122 tok/s.

## 3. The pack: addressed, not copied

`./setup.sh` converts the downloaded GGUF into a **pack** (`tools/iq_pack.py`). For the shipped Maya-S pack
that is:

```
pack/index.txt            1257 tensors: the table the engine loads
pack/dense.bin            901 MB — attention, norms, embeddings
pack/native_experts.txt   42 lines, one per expert layer: quant types + BYTE OFFSETS
pack/tokenizer/           the tokenizer and chat template
```

The line that matters is the last one. The **86 GB of experts are never copied**. `native_experts.txt` records,
per layer, the gate/up and down quant types and the absolute offset of each expert's blob *inside the original
GGUF shards*, so the engine reads an expert in place at a known offset. The pack is an index plus the dense
weights — not a second copy of the model.

The pack also records the quant format **per layer** (`gu_type`/`d_type`), because a real quant mixes formats
across layers and a kernel that assumes one format for all of them would silently misread the rest.

## 4. Starting warm: the profile

Residency is not left to chance. `tools/make_profile.py` ranks every `(layer, expert)` pair by routing
frequency, measured over a trace, and `--expert-profile` loads that ranking. For this model that is
**42 layers x 288 experts = 12,096 pairs**, and the engine fills its **9,439 VRAM slots — 78% of them — with the
hottest pairs before the first token** [measured: *"tiers warm: 9,493 experts (67.6 GB) in 8 s (8.69 GB/s)"*].

The alternative the code comments describe and reject is compulsory-miss filling — whatever the first prompt
happened to touch — which measured `h = 0.4864` against the profile's `0.6447`
(`include/strata/core/expert_cache.hpp`).

## 5. The per-token dispatch

Per layer, the router picks its experts and the host **plans** the layer (`GpuPlanSink`,
`src/core/expert_source.hpp:106`). Every routed id is looked up in the residency table and sorted into:

- **hit groups** — resident experts, read straight from the VRAM arena by the GPU's grouped kernel, one device
  pointer per group;
- **PCIe groups** — RAM-tier misses, staged into a staging arena by the copy engine, or read directly from the
  mapped host arena;
- **CPU-lane jobs** — the flat-batch pool in `src/kernels/cpu/pool.hpp`, whose entire header is a concurrency
  protocol (epoch-tagged claims, so a worker that woke late cannot claim a job from the next batch).

The plan is *published to the device*, so the GPU works through a layer without the host in the loop per layer.
On the Spark the CPU lane essentially never wins — the measured split is 0.161 ms on the CPU against 0.127 ms
over the link, which is why a live run reports `cpu lane 0.00/tok`.

## 6. The movers

Three mechanisms keep the tiers from being a static snapshot:

**Adaptive swaps** (`src/core/glm_fast_path.cu:2142`, `:5152`). Per-layer usage counters decay, and at a
boundary the engine promotes a layer's hottest RAM-only expert into a free VRAM slot; with none free it demotes
the least-used resident when a RAM-only expert is used `bg_ratio` (2) times as much. The rule that makes it safe:
**the expert stays live where it was until its copy lands** — the slot state machine
(`kFree/kResident/kSpare/kDraining/kLent/kLanding`, `src/core/glm_fast_state.hpp:501`) tracks the move, so no
route ever falls to disk mid-swap.

**Lookahead** (`src/core/glm_fast_state.hpp:398`). Every route also names the top-k of the *next* layers'
routers on this layer's input, so disk-only experts among them are read into the RAM tier by reader threads
while the layers in between compute.

**KV streaming and lending.** A long context keeps its attention cells in VRAM rows rather than one block per
layer, and the prompt path can *lend* VRAM slots to its scratch and hand them back — with the eviction
bookkeeping to say what that cost.

## 7. What it looks like live

The same pack, two context sizes, one box. Both from real runs; the 128K figures are the ones the bring-up
documents.

| | 128K (documented) | 256K [measured] |
|---|---|---|
| decode | 15-17 tok/s | 82.07 ms/tok = **12.19 tok/s** |
| expert cache hit | 99.7% | 91.25% -> 95.20% (climbing) |
| RAM-tier fetches | — | 0.67/tok |
| disk reads | — | **23.50/tok, 28.53 ms/tok** |
| VRAM tier | — | 9,297 / 9,439 slots (**full**) |
| RAM tier | — | 16 / 33 slots (**nearly empty**) |

Read the last two rows against §2 and the whole story falls out: at 256K the VRAM tier saturated, so promotion
has nowhere to put anything, and the RAM tier is nearly empty, so a miss has nothing to hit and falls through to
disk — a third of every token. The startup line says where the memory went
(`src/core/glm_fast_path.cu:1088`):

```
VRAM before the expert pool: 38.56 of 119.07 GB in use
  dense weights 6.86, state/KV (262144 ctx) 0.92, activations 0.06,
  other (context, MTP, scratch, prompt path) 30.73
```

The KV itself is under a gigabyte. It is the **30.73 GB of "other"** — context structures, the MTP drafter, the
prompt path's scratch — that 256K took, and that is what starved the RAM tier down to 33 slots.

## 8. The knobs, in the order they pay

1. **`STRATA_GLM_RAM_HEADROOM_GB`** (default 24 in `run-maya-maya-s-v2-iq2_xxs.sh`) — what the tier sizing
   leaves for the CPU side. It is the difference between a RAM tier of 33 slots and one of thousands; every
   slot added turns a ~1.2 ms disk read into a 0.127 ms link fetch. It is also the margin against the hard-lock
   in §0, so it is not a free parameter.
2. **Context size.** 128K hands ~15 GB back to the tiers and buys the documented 99.7% hit and 15-17 tok/s.
3. **`--pcie-frac`** — the share of misses sent over the link rather than to the CPU lane.
4. **`STRATA_GLM_AHEAD` / `STRATA_GLM_PROMOTE`** — lookahead depth and the background promotion's aggressiveness.

None of these is a hardware upgrade, and on this box there isn't one available: the NVMe is already a
datacenter-class drive (the 28.5 ms/token is 23.5 random reads happening *at all*, not a slow disk — the
engine's own notes call this regime *"latency and not bandwidth"*), and the 119 GB is the package's unified
LPDDR5x, soldered.

## 9. Sources

| claim | where |
|---|---|
| unified memory, the hard-lock, no OOM killer | `docs/DGX_SPARK.md:293` |
| the VRAM tier, the residency table, no eviction, the profile over compulsory-miss | `include/strata/core/expert_cache.hpp` |
| the RAM tier, exclusive tiers, slot states, lookahead | `src/core/glm_fast_state.hpp:501,:585,:398` |
| the layer plan, hit/PCIe/CPU groups, staging | `src/core/expert_source.hpp:106` |
| the CPU pool's concurrency protocol | `include/strata/kernels/cpu/pool.hpp` |
| the RAM tier's NUMA spread, the startup sizing line, the CPU-vs-link split, adaptive swaps | `src/core/glm_fast_path.cu:294,:1088,:3317,:2142,:5152` |
| the pack format and the in-place expert offsets | `tools/iq_pack.py` |
| the residency profile | `tools/make_profile.py` |
| the pack's own numbers (layers, experts, bytes) | `pack/native_experts.txt`, `pack/index.txt` of the shipped Maya-S pack |
| the 128K figures | `CHANGELOG.md`, the DGX Spark bring-up entry |
| the 256K figures | a live 256K run of `maya-maya-s-v2-iq2_xxs-256k.json`, 2026-10-25 |
