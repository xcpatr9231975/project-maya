# Maya vs a generic runtime: how the expert route becomes a key

**What this is.** The technical answer to *"why does Maya run this model where a 3-bit GGUF will not?"* — written
at the level of the actual mechanism, not the architecture. The short version, which the rest of this document
expands: **a generic runtime answers "is the layer here?"; Maya answers "where is this one expert right now?"**
— and it can answer that 12,096 different ways, per token, with four different costs and the option to skip.

Every claim is cited to the source that implements it. Numbers marked **[measured]** come from a live 256K run of
the shipped Maya-S pack; the rest is the code.

---

## 1. The key: one integer for "where is this expert"

Everything hangs off a single integer:

```c
const size_t key = (size_t) layer * E + expert;          // src/kernels/cuda/glm_fast.cu:3933
```

For this model that is **42 MoE layers x 288 experts = 12,096 keys**. The same key indexes, with no translation:

| structure | what it holds | where |
|---|---|---|
| `tab[key]` (device) | the expert's **VRAM blob address**, `0` = not resident | `src/kernels/cuda/glm_fast.cu:3933` |
| `tab[n_keys + key]` (device) | the expert's **RAM-tier source address**, `0` = absent | `:3934`, `rtab_key` at `glm_fast_state.hpp:880` |
| `slot_of[key]` (host) | the slot index in the layer's pool, `-1` = absent | `glm_fast_state.hpp:527` |
| `ram_of[key]` (host) | the RAM-tier slot, `-1` = absent | `glm_fast_state.hpp:614` |
| `cnt[key]` | the **LFU route count, aged**, over both tiers | `glm_fast_state.hpp:573` |
| `dcnt[key]` | the same count as the device sees it, for the promotion bar | `glm_fast.cu:4037` |
| `usage[key]` | routes per key **across sessions** (`expert_usage.txt`) | `glm_fast_state.hpp:575` |

So "where is expert *e* of layer *l*" is one array index, and the answer is two device words: a VRAM pointer and a
RAM pointer. That is the whole trick. A generic runtime has no equivalent structure because it has no equivalent
question — its weights are in one place, so there is nothing to look up.

## 2. The route

The router (`src/kernels/cuda/router_sigmoid.cu`) is one block per token:

1. `p[e] = sigmoid(l[e])`, `sel[e] = p[e] + bias[e]` — sigmoid needs no max subtraction, so there is no tree
   reduction at all.
2. `rank(e) = #{f : sel[f] > sel[e]} + #{f < e : sel[f] == sel[e]}` — a stable argsort of `-sel` with ties by
   ascending index, reproduced exactly because **the rank formula *is* the tie rule** (it must match
   `ref/glm.py`'s `top_k_desc`).
3. The gathered weights are the **unbiased** `p` — the bias is applied to `sel` only, which the header calls
   "router_sigmoid.hpp's trap".

Output: *k* expert ids and *k* weights. From here the ids are keys.

## 3. The resolution: four costs, decided inside the kernel

This is the part with no generic-runtime equivalent. For each routed expert the kernel loads **both** table
entries into shared memory at once:

```c
s_tab[tid]  = a.d.tab[key];                    // the VRAM pointer
s_rtab[tid] = a.d.tab[n_keys + key];           // the RAM-tier source
```

The comment above it records why: *"loaded at once (thread 0's plan below read them one after the other, each a
dependent global load)"* — the two lookups were serialised behind one another before.

Then the decision ladder (`glm_fast.cu:4019-4075`), per routed expert:

**a. `ptr != 0` — resident.** The plan entry points straight at the VRAM blob; the GPU's grouped kernel computes
it with an ordinary load. Cost ~0. This is 78% of the pairs on a warm run.

**b. `ptr == 0` and (past `skip_from`, or a "light miss").** A *light miss* is one whose router weight is below a
threshold (`msw`, `:3986`). The expert is **left out**: the plan entry stays null and the expert kernels skip it.
The comment is explicit about why this exists: *"a draft only has to be a good guess"* — it is the speculative
path's escape hatch, and it is also what keeps a low-weighted expert from costing a fetch.

**c. `ptr == 0` and helper-owned (multi-GPU).** Claimed before PCIe quotas or promotion; the host resolves the key
in the helper GPU's own cache.

**d. `ptr == 0`, RAM-tier (`src != 0`).** The CPU-lane split. The kernel counts this route's RAM-tier misses
`f_ram`, reads how many the host should take from a 4-bit field — `to_cpu = (cpu_plan >> (4 * f_ram)) & 15` —
and then **picks the coldest by LFU count** to hand to the host (`rcnt[j] <= rcnt[best]`, `:4005`). The comment
states the intent: *"so the hot ones are still promoted into VRAM"*. The rest are fetched over the link.

**e. `ptr == 0`, `src == 0` (disk only) and ranked low.** `skip |= 1u << i` — *"this token goes without it instead
of waiting on a disk read"* (`:4032`). The threshold is `disk_skip_w` / `disk_skip_from`.

**f. Otherwise — land it.** The blob is fetched and **placed**: a spare slot of this layer if one is free and the
aged route count clears `promote_min` (`:4037-4045`), in which case `tab[key] = ptr` — *"it becomes resident: the
table says so from here on"* — else a scratch slot. A verify window has its own scratch discipline, because a
staggered group's slots still hold another group's in-flight experts.

So one route produces a bitmask of outcomes — `promo`, `fetch`, `cpu`, `skip` — and the plan the device executes
is built from exactly those four bits.

## 4. Why a generic runtime cannot do this

The difference is **granularity of placement**, and it follows directly from the key.

A generic runtime's weights are one contiguous buffer per tensor. The only placement decision it can make is
*which tensor, or which layer, is in which memory* — and when a layer does not fit, the unit of failure is the
layer: every token pays to stream it, or the model does not start. There is no per-expert indirection because
there is no per-expert address.

Maya's `key` makes each of the 12,096 experts an **individually placeable, individually missable object**, and
the tables are mutable at runtime — a fetch in case (f) *writes the table*, so the next token's route to the same
expert takes path (a). That is what turns "the model does not fit" into "the model is 78% where it needs to be,
and the rest is four priced options".

Concretely, on the numbers from §6: a generic runtime facing 91 GB of weights on 119 GB has one option — fit or
stream. Maya has, per expert per token: compute it free, fetch it in 0.127 ms, compute it on the CPU in 0.161 ms,
read it from disk in ~1.2 ms, or **skip it**.

## 5. What keeps the table good

The table is only useful if it is right, and four mechanisms keep it so:

**The profile starts it warm.** `tools/make_profile.py` ranks all 12,096 pairs by routing frequency measured on a
trace, and `--expert-profile` fills the slots in that order before the first token — 9,439 of 12,096 [measured].
The rejected alternative is compulsory-miss filling (whatever the first prompt touched), which measured `h =
0.4864` against the profile's `0.6447` (`include/strata/core/expert_cache.hpp`).

**LFU with a long memory.** `cnt[key]` ages and decays; `usage[key]` persists across sessions in
`expert_usage.txt`, and the warm-up fills the tiers in *its* order — this user's experts first
(`glm_fast_state.hpp:575`).

**The boundary swaps.** At a boundary (`fast_helper_adapt`, `glm_fast_path.cu:2142`) the engine promotes a
layer's hottest RAM-only expert into a free slot, and with none free demotes the least-used resident when a
RAM-only expert is used `bg_ratio` (2) times as much (`:5152`). The move is safe because the expert stays live
where it was until its copy lands — the slot states `kResident/kSpare/kDraining/kLent/kLanding`
(`glm_fast_state.hpp:501`) exist to guarantee exactly that.

**Lookahead.** Every route also names the top-k of the *next* layers' routers, computed on this layer's FFN input
(`glm_fast_state.hpp:398`), and the disk-only experts among them are read into the RAM tier while the layers in
between compute. The engine measures how well the prediction works — `pred_overlap` and `pred_miss_hit`
(`glm_fast_path.cu:4365-4377`) — so the prefetch is scored, not assumed.

## 6. What it buys, on the numbers

The shipped pack, 262,144-token context, one request [measured]:

```
decode 82.07 ms/tok (12.19 tok/s) | vram hit 91.25% -> 95.20%
ram fetches 0.67/tok | disk 23.50 reads/tok in 15.33 waits/tok, 28.53 ms/tok
promotions 23.50/tok | vram 9297/9439  ram 16/33
```

Every one of those fields is a §3 outcome counted: `vram hit` is case (a), `ram fetches` is case (d), `disk
reads` is case (f) that did not promote, `promotions` is case (f) that did, and the misses that took cases (b)
and (e) are simply absent from the output — the token went without them. At the documented 128K the same pack
reports a 99.7% hit and 15-17 tok/s; the difference is entirely how many of the 12,096 keys the tier structure
could keep on path (a).

## 7. Sources

| claim | where |
|---|---|
| the key, the two device tables, the shared-memory load | `src/kernels/cuda/glm_fast.cu:3933` |
| the router, the rank tie rule, the bias trap | `src/kernels/cuda/router_sigmoid.cu` |
| the four-way resolution, the light miss, the CPU-lane coldest-first split, the disk skip, the spare-slot promotion and `tab[key] = ptr` | `src/kernels/cuda/glm_fast.cu:3986-4075` |
| the host mirror, the slot states, the LFU counts, the cross-session usage, the lookahead | `src/core/glm_fast_state.hpp:501,:527,:573,:575,:398,:880` |
| the boundary swaps, `bg_ratio`, the prediction scoring, the startup sizing line | `src/core/glm_fast_path.cu:2142,:5152,:4365,:1088` |
| the profile and the compulsory-miss comparison | `include/strata/core/expert_cache.hpp`, `tools/make_profile.py` |
| the pack layout and the in-place expert offsets | `tools/iq_pack.py`, `pack/native_experts.txt` |
| the tier costs, the pack numbers, the live figures | `docs/EXPERT_TIERS.md` |
