// src/spec/draft_policy_test.cpp - DraftPolicy: when does a lookup window beat the MTP's?
//
// Simulated rounds with the costs measured on the RTX 5070 (window-cost: ~10 ms more per token) check that
//   1. with no lookup proposal the MTP window is kept;
//   2. lookup drafts that are mostly rejected stop being taken (their bucket's rate falls);
//   3. lookup drafts that are always accepted are taken, and the window grows with them;
//   4. match-length buckets learn separately (short matches failing does not stop long ones);
//   5. the policy never proposes a window beyond its cap;
//   6. a proposal's support moves it up the match ladder, in the rate and in what it teaches.
#include "strata/spec/draft_policy.hpp"

#include <cstdio>

using strata::spec::DraftPolicy;

namespace {
int g_fail = 0;
void check(bool ok, const char* what) {
    std::printf("  %-66s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) ++g_fail;
}
double cost(int t) { return 19.0 + 10.5 * (t - 1); }   // ms per round, measured shape
}  // namespace

int main() {
    std::printf("draft_policy_test\n");
    {
        DraftPolicy p(6);
        for (int i = 0; i < 50; ++i) p.observe(false, 4, 2, 0, 0, cost(4));   // MTP windows of 4: 3 tokens each
        const DraftPolicy::Pick k = p.choose(4, 0, 0, 1);
        check(!k.lookup && k.t == 4, "no proposal: the MTP window");
    }
    {
        DraftPolicy p(6);
        for (int i = 0; i < 50; ++i) p.observe(false, 4, 2, 0, 0, cost(4));
        for (int t = 2; t <= 6; ++t) p.observe(false, t, 0, 0, 0, cost(t));
        for (int i = 0; i < 40; ++i) p.observe(true, 6, 0, 4, 1, cost(6));    // short matches, all rejected
        check(p.lookup_rate(4, 1) < 0.15, "rejected short-match drafts: their rate falls below 0.15");
        check(!p.choose(4, 5, 4, 1).lookup, "rejected short-match drafts: no longer taken");
        for (int i = 0; i < 40; ++i) p.observe(true, 6, 5, 30, 3, cost(6));   // long matches, all accepted
        check(p.lookup_rate(30, 3) > 0.9, "accepted long-match drafts: their rate rises above 0.9");
        const DraftPolicy::Pick k = p.choose(4, 5, 30, 3);
        check(k.lookup && k.t == 6, "accepted long matches: the full lookup window is taken");
        check(!p.choose(4, 5, 4, 1).lookup, "buckets are separate: short matches still not taken");
        check(p.choose(4, 20, 30, 3).t <= 6, "never beyond the window cap");
    }
    {
        DraftPolicy p(8);
        for (int i = 0; i < 50; ++i) p.observe(false, 3, 2, 0, 0, cost(3));   // a very good MTP: 3 of 3 tokens
        for (int t = 2; t <= 8; ++t) p.observe(false, t, t - 1, 0, 0, cost(t));
        for (int i = 0; i < 40; ++i) p.observe(true, 4, 2, 8, 1, cost(4));    // lookup at q ~ 0.67
        check(!p.choose(3, 7, 8, 1).lookup, "a mediocre lookup does not replace a strong MTP window");
    }
    {
        DraftPolicy p(6);
        for (int i = 0; i < 50; ++i) p.observe(false, 4, 3, 0, 0, cost(4));   // a near-perfect MTP, only size 4 seen
        const DraftPolicy::Pick k = p.choose(4, 5, 40, 3);
        check(k.lookup && k.t == 6, "an unmeasured size is probed for a confident lookup");
        for (int i = 0; i < 3; ++i) p.observe(true, 6, 5, 40, 3, 3.0 * cost(6));   // it turns out very expensive
        check(!p.choose(4, 5, 40, 3).lookup, "after the probes, the measured cost decides");
    }
    {   // support moves a proposal up the match ladder: three votes on a short match rate like a 12-23 match
        DraftPolicy p(6);
        check(p.lookup_rate(4, 1) < p.lookup_rate(4, 2) && p.lookup_rate(4, 2) < p.lookup_rate(4, 3),
              "support raises a short match's rate");
        check(p.lookup_rate(4, 3) == p.lookup_rate(20, 1), "three votes rate like a 12-23-token match");
        check(p.lookup_rate(30, 3) == p.lookup_rate(30, 1), "a 24+ match is not raised further");
    }
    {   // and each (bucket, support) pair learns on its own
        DraftPolicy p(6);
        for (int i = 0; i < 40; ++i) p.observe(true, 6, 0, 4, 3, cost(6));    // high-support short matches, rejected
        check(p.lookup_rate(4, 3) < 0.3, "a rejected high-support short match: its own rate falls");
        check(p.lookup_rate(4, 1) > 0.5, "the same match with one vote is untouched");
        check(p.lookup_rate(20, 1) > 0.9, "long matches are untouched by it");
    }
    {   // the drafter's per-level scores replace the geometric series: two proposals with the SAME first level
        // and the same bucket are priced differently by their tails, where one pooled rate cannot tell them apart
        DraftPolicy p(8);
        for (int i = 0; i < 50; ++i) p.observe(false, 4, 1, 0, 0, cost(4));      // the MTP: 2 tokens a window
        const double deep[] = {0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60};
        const double thin[] = {0.90, 0.20, 0.05, 0.01};
        const DraftPolicy::Pick a = p.choose(4, 7, 4, 1, deep, 7);              // match<6, one vote
        const DraftPolicy::Pick b = p.choose(4, 7, 4, 1, thin, 4);
        const DraftPolicy::Pick c = p.choose(4, 7, 4, 1);                       // the pooled rate alone
        check(a.lookup && a.t == 5, "a deep chain is priced for a long window");
        check(b.lookup && b.t == 2, "the same first level with a thin tail is priced for the shortest one");
        check(c.lookup && c.t == 3, "the pooled rate alone asks for the middle window, blind to the tail");
    }
    std::printf(g_fail ? "FAIL\n" : "PASS\n");
    return g_fail ? 1 : 0;
}
