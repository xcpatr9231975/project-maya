// include/strata/spec/draft_policy.hpp - per verify round: the MTP's window, or a lookup (suffix) window?
//
// The suffix drafter (prompt lookup) proposes the tokens that followed an earlier repeat of the context. Taken
// whenever it proposes more than the MTP, it lost 2-8% on ordinary text: a long lookup window costs much more to
// verify than the MTP's usual 3-4 tokens, and it is only worth that when enough of it is accepted. llama.cpp's
// lookup decoding answers the same problem with confidence thresholds on its n-gram statistics; here the policy
// learns both sides online and compares expected committed tokens per millisecond:
//
//   MTP     E = the mean tokens a window of that size has committed (EMA), at the measured cost of that size
//   lookup  E(k) = 1 + q + q^2 + ... + q^k for k <= the proposal, q = the acceptance rate of lookup drafts whose
//           match was about as long, at the measured cost of k + 1
//
// and takes the lookup window only when its best E/cost beats the MTP's by `margin`. Costs are the measured round
// times per window size (EMA; sizes not seen yet are scaled from seen ones by a prior shape), so the policy adapts
// to the machine and the context length. It only chooses which drafts to verify: the output is unchanged.
//
// When the drafter hands over its tree's per-level scores (`choose`'s `levels`), E(k) is their sum instead of the
// geometric series: the tree's levels are this proposal's own acceptance profile - the first level is the
// probability its first token is right, the second the probability the first two are, and so on - where one
// pooled q is the average over every proposal in the bucket. The learned rates remain the fallback (no tree, or
// a window longer than the tree's chain) and the prior a bucket starts from.
//
// A proposal's support - the drafter's `last_support`, how many occurrences of the longest match voted for its
// continuation - moves it up the match ladder for its starting rate: one occurrence starts at its own bucket's
// rate, two at one bucket up, three or more at two. A short match seen once is close to a coincidence and one
// seen three times is a repeat, and on the drafter's own simulation over a 710k-token corpus
// (suffix_drafter_test --simulate, acceptance by match bucket and support) that is what the acceptance says: a
// 3-5-token match with three votes accepts like a 12-23-token one, and a 24-token match accepts the same whatever
// its support. Each (bucket, support) pair keeps its own decayed counts, so a repeat that keeps failing teaches
// the policy about repeats and not about long matches.
#pragma once

#include <algorithm>
#include <array>

namespace strata::spec {

class DraftPolicy {
public:
    static constexpr int kMaxT = 8;
    static constexpr int kBuckets = 4;
    static constexpr int kSup = 3;         // support classes: 1, 2, and 3 or more voting occurrences

    explicit DraftPolicy(int max_t, double margin = 0.03);

    struct Pick {
        bool lookup = false;
        int t = 1;                      // window size (1 + drafts)
    };
    /// `t_mtp`: the MTP's window; `lookup_k`: the lookup proposal's length (0 = none); `match`: its match length;
    /// `support`: how many occurrences voted for it (the drafter's last_support); `levels`: the drafter's
    /// per-level acceptance scores (last_levels, `n_levels` of them), or null for none.
    Pick choose(int t_mtp, int lookup_k, int match, int support, const double* levels = nullptr,
                int n_levels = 0) const;
    /// After the round: the window it used, the drafts accepted, and the round's time (verify + commit + draft).
    /// `support` is the proposal's last_support when the round was a lookup one.
    void observe(bool lookup, int t, int accepted, int match, int support, double round_ms);

    double lookup_rate(int match, int support) const;   // current q for a match length and support
    double cost_ms(int t) const;           // measured or scaled round time of a window of t tokens

private:
    static int bucket(int match);
    /// Support classes: 0 for one occurrence, 1 for two, 2 for three or more.
    static int support_class(int support);
    double mtp_tokens(int t) const;

    int max_t_;
    double margin_;
    std::array<double, kMaxT + 1> cost_{}, cost_n_{};      // round ms by window size
    std::array<double, kMaxT + 1> mtp_tok_{}, mtp_n_{};    // tokens committed by MTP windows of that size
    std::array<double, kBuckets * kSup> ok_{}, bad_{};     // lookup drafts accepted / windows cut short
};

}  // namespace strata::spec
