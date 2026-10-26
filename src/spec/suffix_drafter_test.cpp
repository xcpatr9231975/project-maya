// src/spec/suffix_drafter_test.cpp - plan v0.3 P6: suffix-lookup drafter tests and an offline simulation.
//
//   suffix_drafter_test                      unit tests
//   suffix_drafter_test --simulate [--k K] FILE.ids ...
//
// The simulation replays text as if a model had produced it: each step drafts up to K tokens, accepts the
// prefix that matches the true next tokens, and commits that prefix plus one model token (the verify pass's
// bonus). tokens/step is the lookup drafter's ceiling speedup on that text when a K+1-token verify costs the same
// as a 1-token step. Two regimes per file:
//   continue  history = first half of the prompt; generate its second half (natural continuation)
//   copy      history = the whole prompt; generate its middle third again (output quoting input: edits)
// It also reports acceptance by match-length bucket and support (how many occurrences voted for the proposal),
// which is where DraftPolicy's support classes start from.
// No model runs; this measures the drafter on text, not on the model's own outputs.
#include "strata/spec/suffix_drafter.hpp"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

using strata::spec::SuffixDrafter;

namespace {
int g_fail = 0;
void check(bool ok, const char* what) {
    if (!ok) { std::fprintf(stderr, "FAIL: %s\n", what); ++g_fail; }
}

std::vector<int32_t> read_ids(const char* path) {
    std::ifstream f(path);
    std::string text((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    for (char& c : text) if (c == ',') c = ' ';
    std::istringstream in(text);
    std::vector<int32_t> ids;
    for (int32_t v; in >> v;) ids.push_back(v);
    return ids;
}

struct Sim { long steps = 0, tokens = 0, proposals = 0, accepted = 0; };

// Acceptance by match-length bucket and support, the numbers DraftPolicy's support classes start from: for each
// proposal, the drafts the verify pass would have accepted (a prefix of the true next tokens) against the bucket
// its match falls in and how many occurrences voted for it.
struct SupportSim {
    long n[4][3] = {}, acc[4][3] = {};      // [match bucket][support class: 1, 2, 3+]
    void add(int match, int support, int accepted) {
        const int b = match < 6 ? 0 : match < 12 ? 1 : match < 24 ? 2 : 3;
        const int s = support <= 1 ? 0 : support == 2 ? 1 : 2;
        ++n[b][s];
        acc[b][s] += accepted;
    }
    void report() const {
        const char* bn[4] = {"match<6", "6-11", "12-23", "24+"};
        std::printf("%-10s %-8s %10s %14s\n", "bucket", "support", "proposals", "accepted each");
        for (int b = 0; b < 4; ++b)
            for (int s = 0; s < 3; ++s)
                if (n[b][s] > 0)
                    std::printf("%-10s %-8d %10ld %14.2f\n", bn[b], s + 1, n[b][s], (double) acc[b][s] / n[b][s]);
    }
};

Sim simulate(const std::vector<int32_t>& history, const std::vector<int32_t>& target, int K, SupportSim& sup) {
    SuffixDrafter d(3, 32, history.size() + target.size() + 16);
    d.append(history.data(), history.size());
    Sim s;
    std::vector<int32_t> draft(K);
    size_t i = 0;
    while (i < target.size()) {
        const int n = d.propose(K, draft.data());
        int a = 0;
        while (a < n && i + a < target.size() && draft[a] == target[i + a]) ++a;
        if (n > 0) {
            ++s.proposals;
            s.accepted += a;
            sup.add(d.last_match(), d.last_support(), a);
        }
        const size_t commit = std::min(target.size() - i, (size_t) a + 1);
        d.append(&target[i], commit);
        i += commit;
        s.tokens += (long) commit;
        ++s.steps;
    }
    return s;
}

void simulate_file(const char* path, int K, Sim& cont, Sim& copy, SupportSim& sup) {
    const std::vector<int32_t> ids = read_ids(path);
    if (ids.size() < 64) return;
    const size_t half = ids.size() / 2;
    cont = simulate({ids.begin(), ids.begin() + half}, {ids.begin() + half, ids.end()}, K, sup);
    const size_t a = ids.size() / 3, b = 2 * ids.size() / 3;
    copy = simulate(ids, {ids.begin() + a, ids.begin() + b}, K, sup);
}

void unit_tests() {
    {   // a repeated passage is proposed verbatim
        SuffixDrafter d;
        const std::vector<int32_t> doc = {10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20};
        d.append(doc.data(), doc.size());
        const int32_t again[] = {10, 11, 12};
        d.append(again, 3);
        int32_t out[8];
        const int n = d.propose(5, out);
        check(n == 5 && out[0] == 13 && out[4] == 17, "repeat proposes the continuation");
        check(d.last_match() == 3, "match length is the shared suffix");
    }
    {   // no earlier occurrence: nothing proposed
        SuffixDrafter d;
        const std::vector<int32_t> doc = {1, 2, 3, 4, 5, 6, 7};
        d.append(doc.data(), doc.size());
        int32_t out[4];
        check(d.propose(4, out) == 0, "no match proposes nothing");
    }
    {   // two occurrences of the trigram: the longer match wins even though it is older
        SuffixDrafter d;
        const std::vector<int32_t> doc = {7, 8, 1, 2, 3, 100, 101, 9, 1, 2, 3, 200, 201, 50, 7, 8, 1, 2, 3};
        d.append(doc.data(), doc.size());
        int32_t out[2];
        const int n = d.propose(2, out);
        check(n == 2 && out[0] == 100 && out[1] == 101, "longest match preferred over most recent");
        check(d.last_match() == 5, "longest match length 5");
    }
    {   // a match shorter than min_match is ignored (only a bigram in common)
        SuffixDrafter d(4);
        const std::vector<int32_t> doc = {1, 2, 3, 9, 5, 2, 3};
        d.append(doc.data(), doc.size());
        int32_t out[2];
        check(d.propose(2, out) == 0, "min_match respected");
    }
    {   // periodic text: the continuation may run into the current suffix
        SuffixDrafter d;
        const std::vector<int32_t> doc = {1, 2, 3, 1, 2, 3, 1, 2, 3};
        d.append(doc.data(), doc.size());
        int32_t out[6];
        const int n = d.propose(6, out);
        check(n >= 3 && out[0] == 1 && out[1] == 2 && out[2] == 3, "periodic continuation");
    }
    {   // bounded memory: appending beyond the nominal capacity does not crash and still works
        SuffixDrafter d(3, 32, 1024);
        std::vector<int32_t> doc;
        for (int i = 0; i < 5000; ++i) doc.push_back(i % 997);
        d.append(doc.data(), doc.size());
        int32_t out[4];
        check(d.propose(4, out) > 0, "propose after overflow of nominal capacity");
    }
    {   // votes: among the matches of the longest length, the continuation seen twice beats the recent one
        SuffixDrafter d;
        const std::vector<int32_t> doc = {5, 1, 2, 3, 80, 81, 9, 5, 1, 2, 3, 80, 81, 8, 5, 1, 2, 3, 70, 71};
        d.append(doc.data(), doc.size());
        const int32_t again[] = {5, 1, 2, 3};
        d.append(again, 4);
        int32_t out[2];
        const int n = d.propose(2, out);
        check(n == 2 && out[0] == 80 && out[1] == 81, "most-voted continuation proposed");
        check(d.last_support() == 2, "two occurrences voted for it");
        check(d.last_match() == 4, "match length is the shared suffix");
    }
    {   // a longer match still wins: three shorter matches with one continuation do not outvote it
        SuffixDrafter d;
        const std::vector<int32_t> doc = {6, 5, 1, 2, 3, 90, 91, 9, 1, 2, 3, 80, 81, 8, 1, 2, 3, 80, 81,
                                          7, 1, 2, 3, 80, 81};
        d.append(doc.data(), doc.size());
        const int32_t again[] = {6, 5, 1, 2, 3};
        d.append(again, 5);
        int32_t out[2];
        const int n = d.propose(2, out);
        check(n == 2 && out[0] == 90 && out[1] == 91, "the longest match is still the one proposed");
        check(d.last_support() == 1 && d.last_match() == 5, "support 1, match 5");
    }
    {   // a tie on votes goes to the more recent occurrence
        SuffixDrafter d;
        const std::vector<int32_t> doc = {1, 2, 3, 50, 51, 9, 1, 2, 3, 52, 53, 8, 1, 2, 3, 54, 55, 7, 1, 2, 3, 56, 57};
        d.append(doc.data(), doc.size());
        const int32_t again[] = {1, 2, 3};
        d.append(again, 3);
        int32_t out[2];
        const int n = d.propose(2, out);
        check(n == 2 && out[0] == 56 && out[1] == 57, "tie on votes: the most recent occurrence");
        check(d.last_support() == 1, "a single occurrence voted for it");
    }
    {   // a match below min_match is not a candidate at all: it neither proposes nor votes
        const std::vector<int32_t> doc = {5, 1, 2, 3, 60, 61, 9, 1, 2, 3, 50, 51, 8, 1, 2, 3, 50, 51, 7, 1, 2, 3, 50, 51};
        const int32_t again[] = {5, 1, 2, 3};
        SuffixDrafter d(4);
        d.append(doc.data(), doc.size());
        d.append(again, 4);
        int32_t out[2];
        check(d.propose(2, out) == 2 && out[0] == 60 && out[1] == 61,
              "min_match 4: only the order-4 match survives");
        check(d.last_match() == 4 && d.last_support() == 1, "its match is 4 and it stands alone");
        SuffixDrafter none(5);
        none.append(doc.data(), doc.size());
        none.append(again, 4);
        check(none.propose(2, out) == 0, "min_match 5: nothing reaches it");
    }
    {   // deterministic: the same history proposes the same tokens twice
        SuffixDrafter d;
        const std::vector<int32_t> doc = {5, 1, 2, 3, 80, 81, 9, 5, 1, 2, 3, 80, 81, 8, 5, 1, 2, 3, 70, 71};
        d.append(doc.data(), doc.size());
        const int32_t again[] = {5, 1, 2, 3};
        d.append(again, 4);
        int32_t a[2], b[2];
        const int na = d.propose(2, a);
        const int nb = d.propose(2, b);
        check(na == nb && na > 0 && a[0] == b[0] && a[1] == b[1], "propose is deterministic");
    }
    {   // the tree chooses per level: 5 is the likelier first token (three votes) even though the
        // most-voted whole continuation is a tie, and 6 wins under it
        SuffixDrafter d;
        // each occurrence is preceded by its own token, so no match runs past the trigram
        const std::vector<int32_t> doc = {100, 1, 2, 3, 5, 6, 101, 1, 2, 3, 5, 7, 102, 1, 2, 3, 8, 9,
                                          103, 1, 2, 3, 5, 6, 104, 1, 2, 3, 8, 9};
        d.append(doc.data(), doc.size());
        const int32_t again[] = {105, 1, 2, 3};
        d.append(again, 4);
        int32_t out[2];
        const int n = d.propose(2, out);
        check(n == 2 && out[0] == 5 && out[1] == 6, "the tree's chain is 5, 6 (3 votes for 5 beat 2 for 8)");
        check(d.last_support() == 2 && d.last_match() == 3, "two occurrences voted for the chain");
        // the expected count is the two node scores: 3/(5+0.6) and that times 2/(3+0.6)
        const double e1 = 3.0 / 5.6, e2 = e1 * 2.0 / 3.6;
        check(std::abs(d.last_expected() - (e1 + e2)) < 1e-9, "the expected count is the chain's node scores");
    }
    {   // the smoothing is what a single occurrence's long claim costs
        SuffixDrafter d;
        const std::vector<int32_t> doc = {1, 2, 3, 5, 6, 7, 8};
        d.append(doc.data(), doc.size());
        const int32_t again[] = {1, 2, 3};
        d.append(again, 3);
        int32_t out[4];
        check(d.propose(4, out) == 4 && out[0] == 5 && out[3] == 8, "one occurrence proposes its whole run");
        const double soft = d.last_expected();
        // 1/(1+a) per level, a = 0.6: 0.625 + 0.391 + 0.244 + 0.153
        check(std::abs(soft - (0.625 + 0.390625 + 0.244141 + 0.152588)) < 1e-6, "one occurrence claims ~1.41 tokens");
        d.set_tree_alpha(4.0);
        d.propose(4, out);
        check(d.last_expected() < 0.3, "a heavier smoothing makes the same occurrence claim much less");
    }
    {   // a node keeps three children: the fourth distinct first token does not vote
        SuffixDrafter d;
        const std::vector<int32_t> doc = {1, 2, 3, 5, 50, 1, 2, 3, 6, 60, 1, 2, 3, 7, 70, 1, 2, 3, 8, 80};
        d.append(doc.data(), doc.size());
        const int32_t again[] = {1, 2, 3};
        d.append(again, 3);
        int32_t out[2];
        const int n = d.propose(2, out);
        check(n == 2, "a proposal still comes out with four distinct continuations");
        check(out[0] == 8, "the lightest and oldest first token (5) is past the node budget");
        check(d.last_support() == 1, "its single occurrence voted for it");
    }
    std::printf("suffix_drafter unit tests: %s\n", g_fail ? "FAILED" : "OK");
}
}  // namespace

int main(int argc, char** argv) {
    if (argc > 1 && std::strcmp(argv[1], "--simulate") == 0) {
        int K = 8, first = 2;
        if (argc > 3 && std::strcmp(argv[2], "--k") == 0) { K = std::atoi(argv[3]); first = 4; }
        std::printf("%-28s %8s | %-44s | %-44s\n", "prompt", "tokens", "continue: tok/step  proposals  acc/proposal",
                    "copy: tok/step  proposals  acc/proposal");
        SupportSim sup;
        for (int f = first; f < argc; ++f) {
            Sim c, p;
            simulate_file(argv[f], K, c, p, sup);
            const std::vector<int32_t> ids = read_ids(argv[f]);
            if (ids.size() < 64) continue;
            const char* name = std::strrchr(argv[f], '/') ? std::strrchr(argv[f], '/') + 1 : argv[f];
            std::printf("%-28s %8zu | %8.2f %10.1f%% %10.2f              | %8.2f %10.1f%% %10.2f\n", name, ids.size(),
                        (double) c.tokens / c.steps, 100.0 * c.proposals / c.steps,
                        c.proposals ? (double) c.accepted / c.proposals : 0.0, (double) p.tokens / p.steps,
                        100.0 * p.proposals / p.steps, p.proposals ? (double) p.accepted / p.proposals : 0.0);
        }
        sup.report();      // acceptance by match bucket and support: DraftPolicy's support classes start here
        return 0;
    }
    unit_tests();
    return g_fail ? 1 : 0;
}
