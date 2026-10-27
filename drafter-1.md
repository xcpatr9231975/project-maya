# The suffix-lookup drafter ("prompt lookup")

*Plan v0.3 P6. Read out of the tree on 2026-10-27. HEAD `0155067`.*

---

## 1. What it is, in one paragraph

The suffix-lookup drafter proposes the tokens that followed an **earlier occurrence of the sequence's current
suffix**. When the output quotes its input — code edits, refactors, a repeated phrase, boilerplate — the
continuation is usually exact, and one verify pass can accept many tokens at once. It needs **no model, no
weights and no GPU**: it is a hash table plus a few microseconds of arithmetic.

It is the second drafter in the engine. The first is the MTP (a learned multi-token predictor). The two are
never used simultaneously in one window: the **draft policy** (`DraftPolicy`) compares them per round and takes
whichever is expected to commit more tokens per millisecond.

```
        history (prompt + accepted output)
                  |
                  v
        +-------------------+   trigram -> WAYS most recent end positions
        |  per-sequence     |   (open addressing, 48 B/slot, O(1) append)
        |  index            |
        +---------+---------+
                  |  candidates (backward extension => match order 3..64)
                  |  corpus store (suffix array over a built corpus) => second vote source
                  v
        +-------------------+   only the LONGEST matches vote;
        |  vote + tree      |   each voter votes for the continuation it saw
        +---------+---------+   tree node score = parent score * mass/(parent mass + alpha)
                  |  best chain by score descent
                  v
        +-------------------+   expected tokens per proposal: last_expected()
        |  DraftPolicy      |   compares lookup E/cost vs MTP E/cost (both learned online)
        +---------+---------+   -> picks the window size, or keeps the MTP's
                  v
        +-------------------+   T tokens at consecutive positions, one captured graph,
        |  verify window    |   head argmax per token, bit-for-bit greedy decode
        +---------+---------+   -> commits a+1 tokens
                  v
             accepted tokens
```

---

## 2. The files

| File | Lines | What it holds |
|---|---|---|
| `include/strata/spec/suffix_drafter.hpp` | 138 | the design, in prose, at the top of the file; the public API below it |
| `src/spec/suffix_drafter.cpp` | 299 | the implementation: index, candidates, votes, tree, descent |
| `include/strata/spec/draft_policy.hpp` | 75 | the policy: MTP vs lookup, learned online, per (match, support) |
| `src/spec/draft_policy.cpp` | 122 | the policy's arithmetic: costs, rates, the pick rule |
| `include/strata/ngram/corpus_store.hpp` | 79 | the corpus store: a suffix array over a token corpus, mapped read-only |
| `src/ngram/corpus_store.cpp` | 251 | the store: GGUF validation, mmap, the two binary searches |
| `tools/build_corpus.py` | — | builds the store (`--gguf MODEL.gguf --text DIR --out corpus.gguf`) |
| `src/spec/suffix_drafter_test.cpp` | 283 | 14 unit tests + an offline simulation over `.ids` files |
| `src/ngram/corpus_store_test.cpp` | 313 | the store's own tests, plus the drafter against a real store |
| `src/program/generate.cpp` | 8952 | the engine: wiring, the decode loop, the telemetry |

Build (`CMakeLists.txt:880-888`): the four sources form the static library `strata_spec`; `suffix_drafter_test`
links it and is registered with CTest. `strata_spec` exists precisely because these components need no GPU.

---

## 3. The index: a trigram → recent ends table

`SuffixDrafter` keeps two things:

* `hist_` — the full token history (the prompt, then every accepted token), in a `std::vector<int32_t>`.
* `table_` — a fixed-size open-addressing table. Each `Slot` is:

```cpp
struct Slot {
    uint64_t key = 0;          // trigram hash + 1  (0 = empty)
    uint32_t pos[WAYS] = {};   // end positions, most recent first   (WAYS = 8)
    uint8_t  n = 0;            // how many of them are valid
};
```

48 bytes a slot, two slots per nominal token (the table is sized to `2 * capacity_tokens` rounded up to a
power of two, so the load factor stays at or below 0.5). **Appends are O(1)** and memory is bounded — the
drafter cannot be made to grow without limit by a long request.

### Why a trigram, and why not a bigram

The key is built from the last three tokens:

```cpp
uint64_t key_at(size_t end) const {
    const uint64_t a = hist_[end-2], b = hist_[end-1], c = hist_[end];
    return mix(a * 0x9E3779B97F4A7C15ull ^ mix(b + 0x632BE59BD9B4E019ull) ^ (c << 1)) | 1ull;
}
```

`mix()` is a splitmix64-style finaliser (`src/spec/suffix_drafter.cpp:12-19`); the `| 1` makes 0 the empty
sentinel.

The header states the measurement plainly: *"a bigram key puts every occurrence of the same two tokens in one
slot and crowds the longer matches out (measured on a 710k-token corpus: the fire rate fell from 95% to 63%),
so order 2 is not indexed."* The trigram is selective enough to be rare and long enough to seed a match.

### One key, every match order

This is the part worth understanding. The table is keyed on trigrams, but the drafter matches orders 3 to
`max_match` (64 in the engine). A proposal:

1. reads the slot for the trigram **ending at the current token** (`cur = hist_.size() - 1`);
2. for each end position `p` in that slot, extends the match **backwards**:

```cpp
int l = 0;
while (l < max_match_ && l <= (int) p && hist_[p - l] == hist_[cur - l]) ++l;
if (l < min_match_) continue;      // below min_match: not a candidate at all
```

So a match of order *n* is a trigram hit that **agreed on n-3 further tokens**. The key is cheap; the
extension is what buys the order.

### `append`

```cpp
void SuffixDrafter::append(const int32_t* tokens, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        hist_.push_back(tokens[i]);
        const size_t end = hist_.size() - 1;
        if (end < 2) continue;                       // no trigram yet
        Slot* s = find_slot(key_at(end), true);
        if (s == nullptr) continue;                  // table full: the history outgrew its capacity
        for (int w = WAYS - 1; w > 0; --w) s->pos[w] = s->pos[w - 1];
        s->pos[0] = (uint32_t) end;
        if (s->n < WAYS) ++s->n;
    }
}
```

`pos[0]` is always the most recent end. `find_slot` probes linearly and is bounded by the table size, so it
can never spin. A full table drops the append rather than failing — the test *"bounded memory: appending
beyond the nominal capacity does not crash and still works"* covers it.

---

## 4. `propose()` — the whole algorithm

`propose(max_k, out)` returns the number of tokens written (0 = nothing to propose).

### 4.1 Candidates

Only the slot for the current trigram is read, and only candidates that reach `min_match_` survive. The
longest local match length is `blen`; the corpus may contribute one too (below).

### 4.2 The corpus, the second vote source

If a corpus store is attached, it is asked a different question:

* the per-sequence index knows **"has this n-gram occurred since this request started"**;
* the corpus knows **"has this n-gram ever occurred"**, over tens of millions of tokens.

```cpp
n_corpus = corpus_->lookup(hist_.data(), n, min_corpus_order_, corpus_pos_.data(),
                            kMaxCorpusSamples, &n_corpus_samples);
```

`min_corpus_order_` defaults to **7** — a short n-gram occurs too often in a large corpus to be evidence
(TandemLLM tunes its ngram drafter to 7 as well). `kMaxCorpusSamples` is 64: *"the most positions one corpus
lookup samples: a wide suffix-array range is sampled, not read whole."*

### 4.3 Who votes

> **The longest match is the selective one. Only candidates that reached the longest length vote, so a count
> never outvotes a longer match.**

* a local occurrence votes with weight **1.0** — a match inside this request is evidence about *this* text;
* a corpus sample votes with weight **`corpus_weight`** (default **0.5**) — a match in the corpus is evidence
  about text in general;
* **both stores vote when their matches are the same length; the longer match wins outright otherwise.**

This is exactly TandemLLM's counting half: *"its lookup returns one order and every position in the vote is at
that order."*

### 4.4 The continuations

Each voter's continuation is read into one scratch buffer (`vote_cont_`, `(WAYS + kMaxCorpusSamples) * 64`
int32s — 64 tokens a row, and `propose` clips its window at 64):

* local: `hist_[at+1 ...]`, clipped to the current position — *"with periodic text the continuation runs into
  the current suffix, and those tokens are already known"*;
* corpus: `corpus_->continuation(pos, k, dst)`, which stops at a document separator so a store built from
  unrelated files never proposes a sentence stitched from two documents.

### 4.5 Grouping

Voters whose continuation is the same are **grouped**, so a hundred samples of one continuation cost one
comparison each rather than one each against every other. The group key is an FNV-1a fingerprint of the
continuation (`0xcbf29ce484222325` / `0x100000001b3`, mixed with the golden ratio and the length), in a
256-slot open-addressing table:

```cpp
constexpr int kSlots = 256;
static_assert(kSlots > WAYS + kMaxCorpusSamples, "the grouping table must outnumber its voters");
```

The fingerprint is only a *filter* — on a hash hit the tokens are compared for real (`same()`), so a
fingerprint collision cannot change the outcome. Each group accumulates: total weight, vote count, and the
number of **local** votes (which is what `last_source()` reports).

### 4.6 The vote tree

The groups are sorted **heaviest first** (ties → the more recent occurrence) and inserted into a prefix trie.
Bounds:

```cpp
static constexpr int kMaxNodes = 32;        // TandemLLM keeps 16; this drafter keeps 32
static constexpr int kTreeChildren = 3;       // three children a node, like TandemLLM's
```

The heaviest candidates are inserted first, so the children a node keeps are its heaviest.

**The scoring** is the heart of it:

> *"A node's score is the probability its whole path is accepted: the parent's score times the node's vote
> mass over the parent's mass plus `alpha`. The alpha in the denominator at **every level**, not only at the
> root, is what keeps a continuation seen once from claiming a sixteen-token draft — its claim decays
> geometrically at `count / (count + alpha)` per level."*

```cpp
nodes_[0].score = 1.0;                       // the root
for (int i = 1; i < nnodes; ++i) {
    const TreeNode& p = nodes_[nodes_[i].parent];
    const double denom = p.mass + alpha_;    // alpha_ defaults to 0.6
    nodes_[i].score = p.score * (nodes_[i].mass / (denom > 0.0 ? denom : 1.0));
}
```

Children always have a higher index than their parent, so **one forward pass** scores the whole tree.

`set_tree_alpha(alpha)` is exposed; the unit test shows the effect: one occurrence's four-token run claims
**≈1.41 expected tokens** at α=0.6, and **<0.3** at α=4.0.

### 4.7 The descent

`propose` descends the tree by score and returns the best chain:

```cpp
while (k_out < k && nodes_[node].nchild > 0) {
    int best_child = /* the child with the highest score */;
    node = best_child;
    out[k_out++] = nodes_[node].tok;
    expected += nodes_[node].score;
    levels_[k_out - 1] = nodes_[node].score;
}
```

### 4.8 What the drafter reports

| Accessor | Meaning |
|---|---|
| `last_match()` | the length of the match behind the last proposal (0 if none) |
| `last_support()` | how many occurrences voted for it — *1 is a coincidence, 3 or more is a repeat* |
| `last_source()` | 0 = the per-sequence index, 1 = the corpus, 2 = both |
| `last_expected()` | the expected tokens the chain commits — the **sum of the tree's scores along it** |
| `last_levels()` / `last_levels_n()` | the chain's per-level acceptance probabilities: `levels[i]` is the probability the first *i+1* tokens are all accepted |
| `size()` | the history length |

`last_expected()` is the important one for the policy: it is a **per-level** estimate, where the policy's own
geometric series is one pooled rate.

---

## 5. The corpus store

`strata::ngram::CorpusStore` is a **suffix array over a token corpus**, in one GGUF.

```
corpus.tokens     I32[n]   the token stream, documents separated by doc_sep
corpus.sa         I32[n]   the stream's suffix array, sorted by the first max_order tokens
corpus.max_order  u32      the sort's bound, and the longest match the drafter will ask for
corpus.doc_sep    u32      the separator: above every real token id
corpus.n_tokens   u64      n, cross-checked against the tensors
corpus.tokenizer  string   a fingerprint of the tokenizer the corpus was built with
corpus.build      string   what built this file, and when
corpus.format     u32      the layout version (must be 1)
```

* It opens through the **same validated GGUF reader every model uses**, and a file that is not a corpus store
  is refused by its missing tensors rather than by a magic of its own.
* Both arrays are **mapped read-only** (`mmap`, `PROT_READ`): a 40-million-token store costs address space and
  only the pages a lookup touches.
* The sort is **bounded** — sorted by the first `max_order` tokens, because the drafter never matches more
  than that. `tools/build_corpus.py` says it plainly: *"a bounded sort is three doubling rounds of
  `np.lexsort` instead of a full suffix-array construction, which matters at tens of millions of tokens."*
  The builder's defaults: `DEFAULT_MAX_ORDER = 8`, `DEFAULT_DOC_SEP = 1 << 30`.

### A lookup

Two binary searches over the suffix array, from the longest order down to `min_order`:

```cpp
for (int k = top; k >= floor_order; --k) {
    const int32_t* pattern = context + n - k;   // the last k tokens of the context
    range(pattern, k, lo, hi);                  // two binary searches
    if (hi - lo == 0) continue;                 // no occurrence at this order
    ...
    // a wide range is SAMPLED across its whole width, so the votes estimate the
    // distribution rather than the alphabet: the first `count` entries of a huge
    // range are all the same continuation
    const size_t at = width <= cap ? lo + i : lo + (size_t)((double)i * width / count);
    out[i] = sa_[at] + k;                       // the continuation starts k tokens into the match
    return k;                                   // the longest order that occurs
}
```

A short n-gram occurs tens of thousands of times in thirty million tokens, so a wide range is sampled rather
than read whole.

**Measured**: *"A lookup is two binary searches (0.4 us measured on a 710k-token store)"* (CHANGELOG, Unreleased).

### `continuation`

```cpp
int CorpusStore::continuation(int32_t pos, int k, int32_t* out) const {
    int n = 0;
    while (n < k && (uint64_t)(pos + n) < n_) {
        const int32_t t = tokens_[pos + n];
        if (t >= (int32_t) doc_sep_) break;   // a document seam: the continuation stops there
        out[n++] = t;
    }
    return n;
}
```

### Building one

```bash
python tools/build_corpus.py --gguf MODEL.gguf --text CORPUS_DIR --out corpus.gguf
python tools/build_corpus.py --tokenizer DIR --text a.txt b.txt --out corpus.gguf
python tools/build_corpus.py --tokenizer DIR --ids dump.ids --out corpus.gguf
python -m unittest tools.test_build_corpus      # the round-trip test
```

---

## 6. The draft policy: MTP or lookup?

`DraftPolicy` is the arbiter. It **only chooses which drafts to verify** — the output is unchanged either way.

The reason it exists is in the header: *"Taken whenever it proposes more than the MTP, it lost 2-8% on ordinary
text: a long lookup window costs much more to verify than the MTP's usual 3-4 tokens, and it is only worth
that when enough of it is accepted."*

### The comparison

```
MTP     E = the mean tokens a window of that size has committed (EMA), at the measured cost of that size
lookup  E(k) = 1 + q + q^2 + ... + q^k   for k <= the proposal, q = the acceptance rate of lookup
                                            drafts whose match was about as long, at the measured cost of k+1
```

and the lookup window is taken only when its best **E/cost** beats the MTP's by a margin (default 0.03).

**When the drafter hands over its tree's per-level scores, E(k) is their sum instead** — the tree's levels are
*this proposal's own* acceptance profile, where one pooled q is the average over every proposal in the
bucket. The learned rates remain the fallback (no tree, or a window longer than the tree's chain) and the
prior a bucket starts from.

### The support ladder

A proposal's `last_support` moves it **up the match ladder** for its starting rate:

* one occurrence → its own bucket's rate;
* two → one bucket up;
* three or more → two buckets up.

The buckets are by match length: `<6`, `6-11`, `12-23`, `24+`. The support classes are 1, 2, 3+.

The rationale is in the header, and it is measured: *"a short match seen once is close to a coincidence and
one seen three times is a repeat, and on the drafter's own simulation over a 710k-token corpus that is what the
acceptance says: a 3-5-token match with three votes accepts like a 12-23-token one, and a 24-token match
accepts the same whatever its support."*

Each `(bucket, support)` pair keeps **its own decayed counts** (decay 0.97), so a repeat that keeps failing
teaches the policy about repeats and not about long matches.

### The learned state

```cpp
static constexpr int kMaxT = 8;      // the verify window's maximum
static constexpr int kBuckets = 4;    // match length
static constexpr int kSup = 3;        // support classes
constexpr double kCostAlpha = 0.1;    // EMA weight of a new round time
constexpr double kTokAlpha  = 0.05;   // EMA weight of a new MTP window outcome
constexpr double kDecay     = 0.97;   // lookup counts: older windows fade
constexpr double kPriorQ[4] = {0.75, 0.88, 0.93, 0.96};   // the rate a bucket starts from
constexpr double kPriorN = 4.0;      // worth 4 observations
constexpr int    kProbes  = 3;       // a lookup window size is tried this often before its
                                     // guessed cost can veto it
```

The cost of a window size is **measured**, not assumed; a size never seen is scaled from the seen ones by a
prior shape (`kShape[]`, a per-token cost curve). The `kProbes` rule exists so that a size can ever be
measured: *"verification keeps the output; only the one round's speed is at stake."*

---

## 7. The verify window

`strata::core::Verifier` (include/strata/core/verify.hpp) is what actually checks the drafts.

* **T tokens at consecutive positions** go through all 48 layers in **one captured graph**, and the head's
  argmax is produced for every one of them.
* *"Token t's argmax is what plain greedy decode would have produced after token t, **bit for bit**."* Every
  kernel is either the single-token kernel applied per token, or a multi-token kernel whose per-token
  arithmetic is the single-token kernel's.
* The cost: the dense weights are read **once** for T tokens, and the union of the T tokens' missed experts
  goes to the CPU. Measured on decode traces: **1.75x** one token's misses for T=2, **2.4x** for 3, **3.05x**
  for 4.
* `commit(n_keep)` then makes the first `n_keep` tokens permanent: the GDN conv history and recurrent state
  are advanced by replaying those tokens, the indexer's key tail is restored from a snapshot and the accepted
  keys re-appended, and the PLE history is set to its snapshot.

### Why the tree is not verified

The drafter builds a **tree**; the window verifies a **chain**. `verify.hpp:19-31` states the three things a
tree would need and the window does not have:

1. **a per-row attention set** — each node attends to its ancestors' rows and not to its siblings', where
   today the set is "every position at or before mine", which a DFS ordering would silently widen to include
   earlier branches' keys and change what the model picks;
2. **a recurrent state per branch** — the GDN state is one per layer, advanced along the accepted prefix at
   commit, where a tree needs it forked at every branch point (TandemLLM verifies trees through recurrent
   layers, so it is done, but it is the work);
3. **a commit that keeps a path and rolls back its siblings' cells**, where `commit(n_keep)` keeps a prefix.

Until then the drafter's tree chooses the chain the window verifies, and `last_expected()` prices it.

---

## 8. The engine wiring

### 8.1 Construction (once per process)

In the **serve** path (`generate.cpp:7260-7279`):

```cpp
const int S = o.spec;
const int S_mtp = o.mtp_max_t > 0 ? std::min(o.mtp_max_t, S) : S;   // the MTP's windows; suffixes go up to S
if (S_mtp < S) mtp.set_max_drafts(S_mtp - 1);
strata::spec::SuffixDrafter sfx(std::max(1, o.suffix_draft), 64, (size_t) o.max_context + 4096);
strata::spec::DraftPolicy policy(S);   // MTP or lookup window, learned over the whole process
strata::ngram::CorpusStore corpus;
if (!o.corpus_store.empty()) {
    if (!corpus.open(o.corpus_store, cerr, "", 0)) {
        std::fprintf(stderr, "maya serve: --corpus: %s\n", cerr.c_str());
        return 1;
    }
    if (o.suffix_draft > 0) sfx.set_corpus(&corpus);
    std::printf("  corpus store: %s (%llu tokens, matches up to %u, tokenizer %s)\n", ...);
}
```

The one-shot **generate** path has the same block at `generate.cpp:8598-8613` (with `maya generate:` in the
error message). Both build the drafter with:

* `min_match = max(1, o.suffix_draft)` — clamped to 3 by the constructor;
* `max_match = 64`;
* `capacity = o.max_context + 4096` tokens.

The corpus is opened **once per process** and shared by every request, as the PLE table is.

### 8.2 Per request: the history

The serve path (`generate.cpp:7819-7822`):

```cpp
if (o.suffix_draft > 0) {
    sfx.reset();
    for (int64_t t : ids) sfx.append((int32_t) t);
}
```

The history is **reset per request** and seeded with the whole prompt. The one-shot path seeds it with
`o.tokens` and `produced` (`generate.cpp:8616-8619`).

### 8.3 Per round: the lookup

Serve path (`generate.cpp:7850-7870`):

```cpp
int T = S_mtp;
if (req_spec_min_p > 0.0) {              // the MTP's own confidence gate
    T = 1;
    while (T < S_mtp && dprob[(size_t)T - 1] >= (float) req_spec_min_p) ++T;
}
if (first_window) T = 1;

bool from_sfx = false;
int sfx_match = 0, sfx_support = 0;
if (o.suffix_draft > 0 && !first_window) {
    const int k = sfx.propose(S - 1, sbuf.data());
    sfx_match   = sfx.last_match();
    sfx_support = sfx.last_support();
    if (k > 0 && sbuf[0] == drafts[0]) {   // the MTP's own first guess agrees
        const strata::spec::DraftPolicy::Pick pk =
            policy.choose(T, k, sfx_match, sfx_support, sfx.last_levels(), sfx.last_levels_n());
        if (pk.lookup) { T = pk.t; from_sfx = true; }
    }
}
```

Three things to notice:

* the first window never uses the lookup (`first_window` — the first window produces the first generated
  token and the MTP's first cell);
* the lookup's **first proposed token must agree with the MTP's first draft** before the policy is even
  consulted;
* the window is then filled from the winner:

```cpp
window[0] = x;
for (int i = 1; i < T; ++i)
    window[(size_t) i] = from_sfx ? sbuf[(size_t) i - 1] : drafts[(size_t) i - 1];
```

The one-shot path repeats this at `generate.cpp:8622-8655`, with the extra `oracle` fallback and the
`--spec-corrupt` fault injection.

### 8.4 Acceptance

After `ver.run(T, window.data(), p, ...)`:

```cpp
int a = 0;
while (a < T - 1 && window[(size_t)a + 1] == outv[(size_t)a]) ++a;   // the accepted prefix
if (!ver.commit(a + 1, err)) { ... }
if (from_sfx) { ++sfx_windows; sfx_drafts += T - 1; sfx_ok += a; }
for (int i = 0; i <= a && produced_n < max_new && !eos; ++i) {
    produced.push_back(outv[(size_t)i]);
    if (o.suffix_draft > 0) sfx.append(outv[(size_t)i]);   // the accepted tokens feed the next lookup
    eos = ...;
}
```

So the accepted tokens go straight back into the index: the drafter's history is the prompt **plus everything
the model has actually produced**, which is what makes the second and later rounds work at all.

The policy is then told how the round went (`generate.cpp:8715` in the one-shot path, the equivalent in the
serve path):

```cpp
if (timed_round) policy.observe(from_sfx, T, a, sfx_match, sfx_support, round_ms);
```

### 8.5 The window size is widened for lookups

`generate.cpp:4953-4959`:

```cpp
// Prompt lookup (the suffix drafter, on by default): the MTP keeps its --spec windows and a lookup window may be
// up to 2 tokens longer; the draft policy (strata/spec/draft_policy.hpp) takes one only where it pays. Code
// edits +6-11%, ordinary text unchanged (bench/results/2026-09-27-spec). --suffix-draft 0 turns it off.
if (o.suffix_draft > 0 && o.spec >= 2 && o.mtp_max_t == 0) {
    o.mtp_max_t = o.spec;
    o.spec = std::min(o.spec + 2, 8);   // kVerifyMaxT
}
```

So: the MTP keeps its window, the **verify** window may be up to two tokens longer (to hold a longer lookup
chain), and the whole thing is capped at `kVerifyMaxT = 8`. The condition `o.mtp_max_t == 0` means an explicit
`--mtp-max-t` disables the widening.

### 8.6 The flags

| Flag | Default | Meaning |
|---|---|---|
| `--suffix-draft N` | 3 | prompt lookup: draft from an earlier repeat of the last N+ tokens of context, when it pays. **0 = MTP only.** |
| `--corpus PATH` | empty | the lookup drafter's corpus store (`tools/build_corpus.py`): a second vote source over tens of millions of tokens. Default: the request's own text only. |
| `--mtp-max-t M` | 0 | cap the MTP's windows at M tokens (0 = `--spec`; longer ones come from suffixes) |

Parsing: `generate.cpp:4706` (`--suffix-draft`), `generate.cpp:4616` (`--corpus`), `generate.cpp:4707`
(`--mtp-max-t`). The help text is at `generate.cpp:451-455`. The `INFO` line the server prints at startup
includes `lookup=%d` — the `--suffix-draft` value (`generate.cpp:7250-7251`).

---

## 9. Telemetry

### The serve path

```
maya serve: suffix drafts: %lld windows, %lld of %lld drafts accepted
maya serve: corpus store: %llu lookups, %llu matched (%.1f%%), %.2f us each
```

(`generate.cpp:8114-8121`, the first gated on `sfx_windows > 0`, the second on `corpus.is_open() &&
corpus.lookups() > 0`).

### The one-shot generate path

```
suffix drafts  %lld windows, drafts accepted %lld of %lld
corpus store   %llu lookups, %llu matched (%.1f%%), %.2f us each
```

(`generate.cpp:8729-8736`).

Both report **windows** (rounds the lookup won), **drafts offered** and **drafts accepted** — so the accept
rate per lookup window is directly readable.

### The offline simulation

```bash
suffix_drafter_test --simulate [--k K] FILE.ids ...
```

* `--k K` sets the draft window (default 8, first file at index 4 instead of 2).
* Two regimes per file:
  * **continue** — history = the first half of the prompt, generate its second half (natural continuation);
  * **copy** — history = the whole prompt, generate its middle third again (output quoting input: edits).
* It reports **tokens per step** (the lookup drafter's ceiling speedup on that text), the **proposal rate**,
  and the **acceptance per proposal**.
* Then it reports **acceptance by match bucket and support** — the table
  `DraftPolicy`'s support classes start from:

```
bucket     support   proposals   accepted each
match<6    1               1234            0.41
6-11       1                456            1.02
...
```

Files with fewer than 64 ids are skipped. **No model runs** — this measures the drafter on text, not on the
model's own outputs.

---

## 10. The unit tests

`src/spec/suffix_drafter_test.cpp` — 14 cases, in the order they appear:

1. a repeated passage is proposed verbatim; `last_match()` is the shared suffix;
2. no earlier occurrence → nothing proposed;
3. two occurrences of the trigram → **the longer match wins even though it is older**;
4. a match shorter than `min_match` is ignored;
5. periodic text: the continuation may run into the current suffix;
6. bounded memory: appending beyond the nominal capacity does not crash and still works;
7. **votes**: among the matches of the longest length, the continuation seen twice beats the recent one seen
   once; `last_support() == 2`;
8. **a longer match still wins**: three shorter matches with one continuation do not outvote it;
9. a tie on votes goes to the more recent occurrence;
10. `min_match` 4 keeps only the order-4 match; `min_match` 5 proposes nothing;
11. **deterministic**: the same history proposes the same tokens twice;
12. **the tree chooses per level**: three votes for a first token beat two for a whole continuation, and
    `last_expected()` is exactly the sum of the chain's node scores;
13. **the smoothing**: one occurrence claims ≈1.41 expected tokens (0.625 + 0.391 + 0.244 + 0.153), and
    `set_tree_alpha(4.0)` makes the same occurrence claim <0.3;
14. **the node budget**: a node keeps three children; the fourth distinct first token is dropped.

`src/ngram/corpus_store_test.cpp` adds the drafter against a **real store**:

* the corpus proposes a continuation the history lacks; `last_source() == 1`; without the corpus the same
  history proposes nothing;
* **both stores vote when their matches are the same length** — three corpus votes (3 × 0.5) outweigh one
  local vote (1.0), and one local vote outweighs one corpus vote;
* **the longer match wins outright**: the corpus's four-token match beats the local's shorter one;
* `min_corpus_order`: a three-token corpus match is not asked for at `min_corpus_order = 7`.

---

## 11. Measured results

From the CHANGELOG's Unreleased section, all on the drafter's own 710k-token simulation.

| Change | Result |
|---|---|
| **Counting** (votes instead of recency) | fire rate and accepted tokens per window unchanged: 95% of steps propose, **1.72 vs 1.73** tokens accepted per proposal |
| **Support ladder** | a 3-5-token match with three votes accepts like a 12-23-token one; a 24-token match accepts the same whatever its support. The policy takes **41% more** lookup windows on fresh text and **34% more** on copies, at equal or better tokens/ms |
| **Corpus store** | lookup **0.4 us** on a 710k-token store; asked only for matches ≥7 tokens; a corpus vote counts half a local one. On text the corpus has seen: **2.48 → 6.04** tokens/step (copy 3.48 → 5.63). On a disjoint corpus: 2.48 → 2.67 and 3.48 → 4.02 |
| **Vote tree** (with no corpus and no policy) | **2.63 → 2.71** tokens/step on fresh text, **3.48 → 3.63** on copies; fire rate unchanged. The gain is where the per-level choice matters: a short match seen once, **1.22 → 1.31** accepted per proposal |
| **Per-level pricing** in the policy | the policy takes **2.7x** more lookup windows on fresh text and **2.5x** on copies, at **60.1 → 60.4** and **60.4 → 60.6** tokens/ms |
| **In the engine** | code edits **+6-11%**, ordinary text unchanged (`bench/results/2026-09-27-spec` — *this results directory is not in the tree at HEAD*) |

The corpus result deserves the emphasis it gets: the *disjoint* corpus still helps, because the longer
matches it finds are the more reliable ones.

---

## 12. Provenance and licence

The drafter and its corpus store port the **counting half** of the ngram drafter in
[TandemLLM](https://github.com/0xBakeer/TandemLLM) (`engine/drafters/ngram.py`, **AGPL-3.0**) — *"where every
occurrence of a match votes for the continuation that followed it, over both a per-sequence index and a suffix
array of a token corpus."*

Both are **reimplemented from scratch** against the hash table the drafter already had; **no code is taken
from it**. The credit is recorded in `NOTICE:38-48`:

> *"- the vote-counting suffix-lookup drafter (include/strata/spec/suffix_drafter.hpp,
> src/spec/suffix_drafter.cpp) and its corpus store (include/strata/ngram/corpus_store.hpp,
> src/ngram/corpus_store.cpp, tools/build_corpus.py): they port the counting half of the ngram drafter in
> TandemLLM (0xBakeer/TandemLLM, engine/drafters/ngram.py, AGPL-3.0,
> https://github.com/0xBakeer/TandemLLM), where every occurrence of a match votes for the continuation that
> followed it, over both a per-sequence index and a suffix array of a token corpus. They are reimplemented from
> scratch against the hash table this drafter already had, and no code is taken from it"*

Git history: the components arrive with `a813352` (the GLM-5.3-Flash engine import), and are touched by
`2395ee9` and `0101919`.

---

## 13. Notes and open edges

* **The tree is priced, not verified.** The drafter's tree is real, but the window verifies a chain
  (`verify.hpp:19-31`). Verifying the tree needs a per-row attention set, a recurrent state per branch and a
  path-keeping commit — three named pieces of work.
* **The lookup must agree with the MTP on the first token** before the policy sees it
  (`sbuf[0] == drafts[0]`). That is a cheap sanity filter, but it means the lookup never runs alone.
* **`last_source()` is a coarse signal**: 0/1/2, and it is derived from the *leaf* node of the best chain, not
  the whole proposal.
* **The corpus's `max_order` (8 by default) bounds its matches**, so a corpus vote can only tie or win
  against a local match of ≤8 tokens. A longer local match always wins outright.
* **The corpus store's `doc_sep` is not checked against the vocabulary at runtime** — the engine does not know
  the vocabulary size at that point. The builder refuses a separator that does not fit an int32, and its
  default (`1 << 30`) is far above any vocabulary.
* **`--suffix-draft 0` turns the whole thing off** and the engine is MTP-only.
* The `bench/results/2026-09-27-spec` directory referenced in the engine comment (the "+6-11% on code edits"
  measurement) **is not in the tree at HEAD**.
