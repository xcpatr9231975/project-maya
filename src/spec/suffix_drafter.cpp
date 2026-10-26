// src/spec/suffix_drafter.cpp - see include/strata/spec/suffix_drafter.hpp.
#include "strata/spec/suffix_drafter.hpp"

#include "strata/ngram/corpus_store.hpp"

#include <algorithm>
#include <cmath>

namespace strata::spec {

namespace {
uint64_t mix(uint64_t x) {
    x ^= x >> 33;
    x *= 0xff51afd7ed558ccdull;
    x ^= x >> 33;
    x *= 0xc4ceb9fe1a85ec53ull;
    return x ^= x >> 33;
}
}  // namespace

SuffixDrafter::SuffixDrafter(int min_match, int max_match, size_t capacity_tokens)
    : min_match_(std::max(3, min_match)), max_match_(std::max(min_match_, max_match)) {
    size_t cap = 1;
    while (cap < capacity_tokens * 2) cap <<= 1;          // load factor <= 0.5 at the nominal capacity
    table_.assign(cap, Slot{});
    mask_ = cap - 1;
    hist_.reserve(capacity_tokens);
    // The vote scratch: one continuation row per possible voter (the local candidates, then the corpus
    // samples), 64 tokens a row - `propose` clips its window at 64.
    vote_cont_.assign((size_t) (WAYS + kMaxCorpusSamples) * 64, 0);
    corpus_pos_.assign(kMaxCorpusSamples, 0);
}

void SuffixDrafter::reset() {
    hist_.clear();
    std::fill(table_.begin(), table_.end(), Slot{});
    last_match_ = 0;
    last_support_ = 0;
    last_source_ = 0;
    last_expected_ = 0.0;
    levels_n_ = 0;
}

void SuffixDrafter::set_corpus(const strata::ngram::CorpusStore* corpus, int min_corpus_order,
                               double corpus_weight) {
    corpus_ = corpus;
    min_corpus_order_ = std::max(min_corpus_order, min_match_);
    corpus_weight_ = corpus_weight > 0.0 ? corpus_weight : 0.5;
}

uint64_t SuffixDrafter::key_at(size_t end) const {
    const uint64_t a = (uint32_t) hist_[end - 2], b = (uint32_t) hist_[end - 1], c = (uint32_t) hist_[end];
    return mix(a * 0x9E3779B97F4A7C15ull ^ mix(b + 0x632BE59BD9B4E019ull) ^ (c << 1)) | 1ull;   // never 0
}

SuffixDrafter::Slot* SuffixDrafter::find_slot(uint64_t key, bool insert) {
    for (size_t i = key & mask_, probes = 0; probes <= mask_; i = (i + 1) & mask_, ++probes) {
        Slot& s = table_[i];
        if (s.key == key) return &s;
        if (s.key == 0) {
            if (!insert) return nullptr;
            s.key = key;
            return &s;
        }
    }
    return nullptr;                                         // table full: the history outgrew its capacity
}

void SuffixDrafter::append(const int32_t* tokens, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        hist_.push_back(tokens[i]);
        const size_t end = hist_.size() - 1;
        if (end < 2) continue;
        Slot* s = find_slot(key_at(end), true);
        if (s == nullptr) continue;
        for (int w = WAYS - 1; w > 0; --w) s->pos[w] = s->pos[w - 1];
        s->pos[0] = (uint32_t) end;
        if (s->n < WAYS) ++s->n;
    }
}

int SuffixDrafter::propose(int max_k, int32_t* out) {
    last_match_ = 0;
    last_support_ = 0;
    last_source_ = 0;
    last_expected_ = 0.0;
    levels_n_ = 0;
    const size_t n = hist_.size();
    if (n < 4 || max_k <= 0) return 0;
    const size_t cur = n - 1;
    const Slot* s = find_slot(key_at(cur), false);
    // Candidates: every earlier end of the same trigram whose backward extension reaches min_match, most recent
    // first. The extension is what turns one trigram key into every order from min_match up to max_match.
    size_t end[WAYS];
    int len[WAYS];
    int nc = 0;
    if (s != nullptr) {
        for (int w = 0; w < s->n; ++w) {
            const size_t p = s->pos[w];
            if (p >= cur) continue;                         // the current suffix itself
            int l = 0;
            while (l < max_match_ && l <= (int) p && hist_[p - l] == hist_[cur - l]) ++l;
            if (l < min_match_) continue;
            end[nc] = p;
            len[nc] = l;
            ++nc;
        }
    }
    // The corpus, asked only for a match long enough to be evidence. Its own lookup picks the longest suffix
    // of the context that occurs in it, at most the store's max_order.
    int n_corpus = 0, n_corpus_samples = 0;
    if (corpus_ != nullptr && corpus_->is_open()) {
        n_corpus = corpus_->lookup(hist_.data(), n, min_corpus_order_, corpus_pos_.data(), kMaxCorpusSamples,
                                   &n_corpus_samples);
        if (n_corpus > 0 && n_corpus_samples == 0) n_corpus = 0;
    }
    if (nc == 0 && n_corpus == 0) return 0;
    // Votes: the longest match is the selective one, exactly as before - candidates that matched fewer tokens do
    // not vote, so a count never outvotes a longer match (TandemLLM's ngram drafter counts the same way: its
    // lookup returns one order and every position in the vote is at that order). A local occurrence votes 1.0
    // and a corpus sample `corpus_weight`: a match inside this request is evidence about this text. Among the
    // candidates at the longest length the heaviest continuation wins, then the one with more occurrences, then
    // a local one over a corpus one, then the more recent - so the proposal is deterministic.
    int blen = n_corpus;
    for (int i = 0; i < nc; ++i)
        if (len[i] > blen) blen = len[i];
    const int kVoters = WAYS + kMaxCorpusSamples;
    double w[kVoters];
    size_t at[kVoters];
    bool local[kVoters];
    int nv = 0;
    for (int i = 0; i < nc; ++i)
        if (len[i] == blen) {
            w[nv] = 1.0;
            at[nv] = end[i];
            local[nv] = true;
            ++nv;
        }
    if (n_corpus == blen)
        for (int i = 0; i < n_corpus_samples; ++i) {
            w[nv] = corpus_weight_;
            at[nv] = (size_t) corpus_pos_[(size_t) i];
            local[nv] = false;
            ++nv;
        }
    // Every voter's continuation, in one scratch buffer, clipped as the local rule always clipped it.
    const int k = std::min<int>(max_k, 64);
    int rl[kVoters];
    uint64_t fh[kVoters];
    int live = 0;
    for (int v = 0; v < nv; ++v) {
        int32_t* dst = &vote_cont_[(size_t) v * 64];
        if (local[v]) {
            const int r = (int) std::min((size_t) k, cur - at[v]);
            for (int t = 0; t < r; ++t) dst[t] = hist_[at[v] + 1 + t];
            rl[v] = r;
        } else {
            rl[v] = corpus_->continuation((int32_t) at[v], k, dst);
        }
        uint64_t x = 0xcbf29ce484222325ull;
        for (int t = 0; t < rl[v]; ++t) {
            x ^= (uint32_t) dst[t];
            x *= 0x100000001b3ull;
        }
        fh[v] = rl[v] > 0 ? (x ^ (0x9E3779B97F4A7C15ull * (uint64_t) rl[v])) | 1ull : 0;
        if (rl[v] > 0) ++live;
    }
    if (live == 0) return 0;
    auto same = [&](int a, int b) {
        if (rl[a] != rl[b] || fh[a] != fh[b]) return false;
        const int32_t* pa = &vote_cont_[(size_t) a * 64];
        const int32_t* pb = &vote_cont_[(size_t) b * 64];
        for (int t = 0; t < rl[a]; ++t)
            if (pa[t] != pb[t]) return false;
        return true;
    };
    // Group the voters by continuation, so a hundred samples of the same continuation cost one comparison each
    // and not one each against every other: a small open-addressing table keyed by the continuation's hash,
    // each entry the first voter of its group. The tokens are compared once per voter, against that first.
    // A power of two, because the probe masks with its size minus one - and the probe is bounded by it, so a
    // full table can never spin.
    constexpr int kSlots = 256;
    static_assert(kSlots > WAYS + kMaxCorpusSamples, "the grouping table must outnumber its voters");
    int slot_of[kSlots];
    for (int i = 0; i < kSlots; ++i) slot_of[i] = -1;
    int group[kVoters];
    for (int v = 0; v < nv; ++v) {
        if (rl[v] == 0) continue;
        group[v] = v;                           // a full table would leave it here; it cannot fill
        size_t i = (size_t) (fh[v] >> 17) & (size_t) (kSlots - 1);
        for (int probes = 0; probes <= kSlots; ++probes, i = (i + 1) & (size_t) (kSlots - 1)) {
            const int g = slot_of[i];
            if (g < 0) {                       // a continuation no earlier voter had
                slot_of[i] = v;
                group[v] = v;
                break;
            }
            if (same(g, v)) {                  // the same continuation: it joins g's group
                group[v] = g;
                break;
            }
        }
    }
    double gw[kVoters];
    int gs[kVoters], gn[kVoters];
    for (int v = 0; v < nv; ++v)
        if (rl[v] > 0 && group[v] == v) {
            gw[v] = 0.0;
            gs[v] = 0;
            gn[v] = 0;
        }
    for (int v = 0; v < nv; ++v)
        if (rl[v] > 0) {
            gw[group[v]] += w[v];
            ++gs[group[v]];
            if (local[v]) ++gn[group[v]];
        }
    // The tree: one node per distinct prefix of the voted continuations, heaviest candidates inserted first so
    // the children a node keeps are its heaviest. A node's score is its parent's times its mass over the
    // parent's mass plus alpha - the smoothing at EVERY level, which is what stops a continuation seen once
    // from claiming a sixteen-token draft. Children always have a higher index than their parent, so one
    // forward pass scores the whole tree.
    struct Cand {
        const int32_t* tok;
        int len;
        double w;
        int votes, local_votes;
        size_t at;
    };
    Cand cands[kVoters];
    int ncand = 0;
    for (int v = 0; v < nv; ++v)
        if (rl[v] > 0 && group[v] == v)
            cands[ncand++] = Cand{&vote_cont_[(size_t) v * 64], rl[v], gw[v], gs[v], gn[v], at[v]};
    std::sort(cands, cands + ncand, [](const Cand& a, const Cand& b) {
        if (a.w != b.w) return a.w > b.w;               // heaviest first
        return a.at > b.at;                             // then the most recent, so ties keep the newer
    });
    int nnodes = 1;                                      // node 0 is the root
    nodes_[0] = TreeNode{};
    double total = 0.0;
    for (int c = 0; c < ncand; ++c) total += cands[c].w;
    nodes_[0].mass = total;
    nodes_[0].score = 1.0;
    for (int c = 0; c < ncand && nnodes < kMaxNodes; ++c) {
        int node = 0;
        for (int d = 0; d < cands[c].len; ++d) {
            int child = -1;
            for (int i = 0; i < nodes_[node].nchild; ++i)
                if (nodes_[nodes_[node].child[i]].tok == cands[c].tok[d]) {
                    child = nodes_[node].child[i];
                    break;
                }
            if (child < 0) {
                // the node budget: past it the remaining candidates are the lighter ones, and a long
                // candidate stops adding nodes rather than running the array over
                if (nodes_[node].nchild >= kTreeChildren || nnodes >= kMaxNodes) break;
                child = nnodes++;
                nodes_[child] = TreeNode{};
                nodes_[child].parent = node;
                nodes_[child].tok = cands[c].tok[d];
                nodes_[node].child[nodes_[node].nchild++] = child;
            }
            nodes_[child].mass += cands[c].w;
            nodes_[child].votes += cands[c].votes;
            nodes_[child].local_votes += cands[c].local_votes;
            node = child;
        }
    }
    for (int i = 1; i < nnodes; ++i) {
        const TreeNode& p = nodes_[nodes_[i].parent];
        const double denom = p.mass + alpha_;
        nodes_[i].score = p.score * (nodes_[i].mass / (denom > 0.0 ? denom : 1.0));
    }
    // The best chain: descend by score. The scores only shrink down a chain, so every level adds its
    // probability to the expected count, and a leaf is where the chain ends.
    int node = 0, k_out = 0;
    double expected = 0.0;
    while (k_out < k && nodes_[node].nchild > 0) {
        int best_child = nodes_[node].child[0];
        for (int i = 1; i < nodes_[node].nchild; ++i)
            if (nodes_[nodes_[node].child[i]].score > nodes_[best_child].score)
                best_child = nodes_[node].child[i];
        node = best_child;
        out[k_out++] = nodes_[node].tok;
        expected += nodes_[node].score;
        levels_[k_out - 1] = nodes_[node].score;
    }
    levels_n_ = k_out;
    if (k_out == 0) return 0;
    last_match_ = blen;
    last_support_ = nodes_[node].votes;
    last_source_ = nodes_[node].local_votes > 0 && nodes_[node].local_votes < nodes_[node].votes ? 2
                                                                                                 : (nodes_[node].local_votes > 0 ? 0 : 1);
    last_expected_ = expected;
    return k_out;
}

}  // namespace strata::spec
