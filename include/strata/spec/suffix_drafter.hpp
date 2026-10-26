// include/strata/spec/suffix_drafter.hpp - plan v0.3 P6: the suffix-lookup drafter (no weights, no GPU).
//
// Proposes the tokens that followed an earlier occurrence of the sequence's current suffix: when the
// output quotes its input (edits, refactors, repeated code) the continuation is usually exact, and a verify pass
// accepts many tokens at once. It needs no model and costs microseconds.
//
// Index: every trigram of the history (prompt + accepted output) maps to its WAYS most recent end positions in a
// fixed-size open-addressing table, so memory is bounded (two slots per nominal token, 48 bytes a slot) and
// appends are O(1). The trigram key is the selective one on purpose: a bigram key puts every occurrence of the
// same two tokens in one slot and crowds the longer matches out (measured on a 710k-token corpus: the fire rate
// fell from 95% to 63%), so order 2 is not indexed. One trigram key serves every match order from 3 up to
// max_match: a proposal reads the slot for the trigram ending at the current token and extends each candidate
// match backwards token by token, so a match of order n is a trigram hit that agreed on n - 3 further tokens.
// Candidates that do not reach min_match propose nothing.
//
// Votes, not recency. Among the candidates that reached the longest match, each votes for the continuation
// that followed it, and the most-voted continuation is proposed: seen three times is better evidence than seen
// once, and recency only breaks a tie. A count never outvotes a longer match - the candidates at the longest
// length are the only voters, which is how TandemLLM's ngram drafter counts as well (its lookup returns one
// order and every position in the vote is at that order). That is the counting half of its drafter
// (0xBakeer/TandemLLM, engine/drafters/ngram.py, AGPL-3.0), reimplemented from scratch against the hash table
// this drafter already had; no code is taken from it. `last_support` reports how many occurrences voted - the
// difference between a repeat and a coincidence - for a caller that wants to price the evidence and not only
// the match (DraftPolicy prices the match length today).
//
// THE TREE the votes build is a prefix trie over the voted continuations, and each node's score is the
// probability its whole path is accepted: the parent's score times the node's vote mass over the parent's mass
// plus `alpha`. The alpha in the denominator at EVERY level, not only at the root, is what keeps a
// continuation seen once from claiming a sixteen-token draft - its claim decays geometrically at
// count / (count + alpha) per level. `propose` descends the tree by score and returns its best chain, which is
// the interface the verify window speaks; `last_expected` is the sum of the scores along that chain, the
// expected tokens the chain commits, which is a per-level estimate where the policy's geometric series is one
// pooled rate. (Verifying the tree itself rather than its best chain needs a tree mask in the verify window
// and a recurrent state per branch; see docs in src/core/verify.cpp.)
//
// THE CORPUS is the second store (`set_corpus`), and it answers a different question: the per-sequence index
// knows what has occurred since this request started, the corpus knows what has ever occurred, over tens of
// millions of tokens. It is asked only for matches of at least `min_corpus_order` (a short n-gram occurs too
// often in a large corpus to be evidence), and its votes count `corpus_weight` against a local occurrence's
// 1.0: a match inside the current request is evidence about THIS text, a match in the corpus is evidence
// about text in general. Both stores vote when their matches are the same length, and the longer match wins
// outright otherwise.
//
// A continuation is read up to the current position, as it always was: with periodic text the continuation runs
// into the current suffix, and those tokens are already known.
#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace strata::ngram {
class CorpusStore;
}  // namespace strata::ngram

namespace strata::spec {

class SuffixDrafter {
public:
    static constexpr int WAYS = 8;
    /// The most positions one corpus lookup samples: a wide suffix-array range is sampled, not read whole.
    static constexpr int kMaxCorpusSamples = 64;
    /// The vote tree's bounds: TandemLLM's ngram drafter keeps a node budget of 16 and 3 children a node.
    static constexpr int kMaxNodes = 32;
    static constexpr int kTreeChildren = 3;

    explicit SuffixDrafter(int min_match = 3, int max_match = 32, size_t capacity_tokens = 1u << 19);

    void reset();
    /// Add tokens to the history (the prompt, then every accepted token).
    void append(const int32_t* tokens, size_t n);
    void append(int32_t token) { append(&token, 1); }

    /// The corpus store to vote alongside the per-sequence index (null removes it). `min_corpus_order` is the
    /// shortest match the corpus is asked for, `corpus_weight` a corpus vote's weight against a local one.
    void set_corpus(const strata::ngram::CorpusStore* corpus, int min_corpus_order = 7,
                    double corpus_weight = 0.5);
    /// The tree's smoothing: a level's claim is its vote mass over the parent's mass plus this. TandemLLM's
    /// simulator tunes 0.6, and a larger one makes a single occurrence claim less.
    void set_tree_alpha(double alpha) { alpha_ = alpha > 0.0 ? alpha : 0.6; }

    /// Write up to `max_k` proposed next tokens to `out`; returns how many (0 = no match of at least min_match).
    int propose(int max_k, int32_t* out);

    /// Length of the match behind the last proposal (0 if none).
    int last_match() const { return last_match_; }
    /// Occurrences that voted for the last proposal (0 if none): 1 is a coincidence, 3 or more is a repeat.
    int last_support() const { return last_support_; }
    /// Where the last proposal's votes came from: 0 = the per-sequence index, 1 = the corpus, 2 = both.
    int last_source() const { return last_source_; }
    /// The expected tokens the last proposal commits: the sum of the tree's scores along its best chain.
    double last_expected() const { return last_expected_; }
    /// The last proposal's per-level acceptance probabilities, the tree's node scores along its best chain:
    /// `last_levels()[i]` is the probability the chain's first i+1 tokens are all accepted. The count is
    /// `last_levels_n()`; a caller that wants a window of k drafts sums the first k of them.
    const double* last_levels() const { return levels_; }
    int last_levels_n() const { return levels_n_; }
    size_t size() const { return hist_.size(); }

private:
    struct Slot {
        uint64_t key = 0;             // trigram hash + 1 (0 = empty)
        uint32_t pos[WAYS] = {};      // end positions, most recent first
        uint8_t n = 0;
    };
    /// One node of the vote tree: a prefix of the voted continuations, its vote mass, the probability its
    /// whole path is accepted, and the votes that pass through it.
    struct TreeNode {
        int parent = -1;
        int32_t tok = 0;
        double mass = 0.0, score = 0.0;
        int votes = 0, local_votes = 0;
        int child[kTreeChildren] = {};
        int nchild = 0;
    };
    Slot* find_slot(uint64_t key, bool insert);
    uint64_t key_at(size_t end) const;

    int min_match_, max_match_;
    std::vector<int32_t> hist_;
    std::vector<Slot> table_;
    size_t mask_ = 0;
    int last_match_ = 0;
    int last_support_ = 0;
    int last_source_ = 0;
    double last_expected_ = 0.0;
    double levels_[64] = {};             // the best chain's per-level scores, as long as a window can be
    int levels_n_ = 0;
    const strata::ngram::CorpusStore* corpus_ = nullptr;
    int min_corpus_order_ = 7;
    double corpus_weight_ = 0.5;
    double alpha_ = 0.6;
    std::vector<int32_t> corpus_pos_;      // one lookup's sampled continuation starts
    std::vector<int32_t> vote_cont_;       // each voter's continuation, max_k tokens a row
    TreeNode nodes_[kMaxNodes];            // the vote tree, rebuilt by every propose
};

}  // namespace strata::spec
