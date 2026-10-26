// src/ngram/corpus_store_test.cpp - CorpusStore: the lookup drafter's corpus suffix array, and the drafter's
// use of it as a second vote source.
//
//   corpus_store_test
//
// The store is written here by a minimal GGUF emitter (the engine has no GGUF WRITER, and the Python one that
// tools/build_corpus.py uses is not available to a C++ test), so the test is hermetic: it builds the file,
// opens it through the same validated reader the engine uses, and checks the lookup against what it wrote.
// tools/test_build_corpus.py covers the Python side of the same contract.
#include "strata/ngram/corpus_store.hpp"

#include "strata/spec/suffix_drafter.hpp"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

using strata::ngram::CorpusStore;
using strata::spec::SuffixDrafter;

namespace {

int g_fail = 0;
void check(bool ok, const char* what) {
    std::printf("  %-64s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) ++g_fail;
}

// ------------------------------------------------------------------ a minimal GGUF writer (test only)
void put_u32(std::string& b, uint32_t v) { b.append((const char*) &v, 4); }
void put_u64(std::string& b, uint64_t v) { b.append((const char*) &v, 8); }
void put_i32s(std::string& b, const std::vector<int32_t>& v) { b.append((const char*) v.data(), v.size() * 4); }
void put_str(std::string& b, const std::string& s) {
    put_u64(b, s.size());
    b.append(s);
}
void put_kv_u32(std::string& b, const char* key, uint32_t v) {
    put_str(b, key);
    put_u32(b, 4);      // MetaType::U32
    put_u32(b, v);
}
void put_kv_u64(std::string& b, const char* key, uint64_t v) {
    put_str(b, key);
    put_u32(b, 10);     // MetaType::U64
    put_u64(b, v);
}
void put_kv_str(std::string& b, const char* key, const std::string& v) {
    put_str(b, key);
    put_u32(b, 8);      // MetaType::STRING
    put_str(b, v);
}

/// A GGUF v3 with two I32 tensors and the corpus metadata the store validates. Tensor data is aligned to 32
/// bytes, as the format requires, and the offsets in the tensor directory say so.
void write_corpus_gguf(const std::string& path, const std::vector<int32_t>& tokens,
                       const std::vector<int32_t>& sa, uint32_t max_order, uint32_t doc_sep,
                       const std::string& tokenizer, uint64_t n_meta = UINT64_MAX) {
    const uint64_t tok_bytes = tokens.size() * 4;
    const uint64_t tok_pad = (32 - tok_bytes % 32) % 32;
    const uint64_t sa_off = tok_bytes + tok_pad;
    std::string b;
    put_u32(b, 0x46554747);                       // "GGUF"
    put_u32(b, 3);
    put_u64(b, 2);                                // tensors
    put_u64(b, 6);                                 // metadata
    put_kv_u32(b, "corpus.format", 1);
    put_kv_u32(b, "corpus.max_order", max_order);
    put_kv_u32(b, "corpus.doc_sep", doc_sep);
    put_kv_u64(b, "corpus.n_tokens", n_meta == UINT64_MAX ? tokens.size() : n_meta);
    put_kv_str(b, "corpus.tokenizer", tokenizer);
    put_kv_str(b, "corpus.build", "corpus_store_test");
    put_str(b, "corpus.tokens");
    put_u32(b, 1);
    put_u64(b, tokens.size());
    put_u32(b, 26);                               // GGML I32
    put_u64(b, 0);
    put_str(b, "corpus.sa");
    put_u32(b, 1);
    put_u64(b, sa.size());
    put_u32(b, 26);
    put_u64(b, sa_off);
    while (b.size() % 32) b.push_back('\0');
    put_i32s(b, tokens);
    for (uint64_t i = 0; i < tok_pad; ++i) b.push_back('\0');
    put_i32s(b, sa);
    std::ofstream f(path, std::ios::binary);
    f.write(b.data(), (std::streamsize) b.size());
}

/// The bounded-prefix suffix array of `tokens`: what tools/build_corpus.py writes. O(n^2 log n) is fine at
/// test sizes, and it is written here rather than called so the test does not depend on Python.
std::vector<int32_t> bounded_sa(const std::vector<int32_t>& t, int max_order) {
    const int n = (int) t.size();
    std::vector<int32_t> sa(n);
    for (int i = 0; i < n; ++i) sa[i] = i;
    auto key = [&](int i, int j) {
        const int at = i + j;
        return at < n ? t[at] : -1;
    };
    std::sort(sa.begin(), sa.end(), [&](int a, int b) {
        for (int j = 0; j < max_order; ++j) {
            const int x = key(a, j), y = key(b, j);
            if (x != y) return x < y;
        }
        return false;
    });
    return sa;
}

const uint32_t SEP = 1u << 30;

void unit_tests() {
    const std::string path = "/tmp/opencode/corpus_store_test.gguf";
    {   // a small corpus with repeats across documents
        // documents: [10 11 12 13] [20 10 11 12 99] [30 31 10 11 12 13 40]
        const std::vector<int32_t> tokens = {SEP, 10, 11, 12, 13, SEP, 20, 10, 11, 12, 99, SEP, 30, 31, 10, 11, 12, 13, 40};
        const std::vector<int32_t> sa = bounded_sa(tokens, 8);
        write_corpus_gguf(path, tokens, sa, 8, SEP, "test-tokenizer");

        CorpusStore s;
        std::string err;
        check(s.open(path, err), "open a store the test wrote");
        check(s.is_open() && s.n_tokens() == tokens.size() && s.max_order() == 8, "metadata read back");
        check(s.tokenizer() == "test-tokenizer" && s.doc_sep() == SEP, "tokenizer and separator");

        int32_t out[64];
        int ns = 0;
        const int32_t ctx1[] = {10, 11, 12};                 // occurs three times
        const int n1 = s.lookup(ctx1, 3, 3, out, 64, &ns);
        check(n1 == 3 && ns == 3, "a three-token match is found, three occurrences sampled");
        int distinct = 0;
        for (int i = 0; i < ns; ++i) {
            int32_t c[8];
            const int k = s.continuation(out[i], 4, c);
            distinct += (k > 0);
            check(k >= 1 && k <= 4, "a continuation is 1..4 tokens");
        }
        check(distinct == n1, "every sampled position continues");
        // the separator: the match at the end of a document must not run into the next one
        int seam = 0;
        for (int i = 0; i < ns; ++i) {
            int32_t c[8];
            const int k = s.continuation(out[i], 8, c);
            for (int j = 0; j < k; ++j)
                if (c[j] >= (int32_t) SEP) ++seam;
        }
        check(seam == 0, "no continuation crosses a document separator");

        const int32_t ctx2[] = {10, 11, 12, 13};              // the longest repeat in the corpus
        const int n2 = s.lookup(ctx2, 4, 3, out, 64, &ns);
        check(n2 == 4 && ns == 2, "a four-token match is found, two occurrences sampled");
        int32_t c[8];
        const int k = s.continuation(out[0], 4, c);
        check(k == 1 && c[0] == 40, "the continuation after the longest repeat is 40");
        check(s.continuation(out[1], 4, c) == 0, "the other occurrence's continuation stops at the separator");

        check(s.lookup(ctx1, 3, 5, out, 64) == 0, "min_order is respected: no five-token match, nothing");
        check(s.lookup(ctx2, 4, 5, out, 64) == 0, "min_order 5 with a four-token match: nothing");

        const int32_t ctx3[] = {30, 31};                       // occurs once
        check(s.lookup(ctx3, 2, 2, out, 64) == 2, "a two-token match is found when min_order allows it");
        const int32_t ctx4[] = {77, 78};
        check(s.lookup(ctx4, 2, 2, out, 64) == 0, "an absent bigram: nothing");
        s.close();
        check(!s.is_open(), "close unmaps");
    }
    {   // max_order bounds the match: a longer repeat is not seen past it
        const std::vector<int32_t> tokens = {SEP, 5, 6, 7, 8, 9, 1, SEP, 4, 5, 6, 7, 8, 9, 1, 2};
        const std::vector<int32_t> sa = bounded_sa(tokens, 4);
        write_corpus_gguf(path, tokens, sa, 4, SEP, "tk");
        CorpusStore s;
        std::string err;
        check(s.open(path, err), "open a max_order-4 store");
        int32_t out[64];
        const int32_t ctx[] = {5, 6, 7, 8, 9, 1};
        int ns = 0;
        const int n = s.lookup(ctx, 6, 3, out, 64, &ns);
        check(n == 4 && ns == 2, "the match stops at max_order");
        // Two occurrences of the 4-token match share its whole bounded key, so their order in the range is
        // the sort's choice: one continues with 2, the other stops at the separator.
        int32_t c0[8], c1[8];
        const int k0 = s.continuation(out[0], 4, c0);
        const int k1 = s.continuation(out[1], 4, c1);
        check((k0 == 1 && c0[0] == 2 && k1 == 0) || (k1 == 1 && c1[0] == 2 && k0 == 0),
              "one occurrence continues with 2, the other stops at the separator");
        s.close();
    }
    {   // a wide range is sampled, not read whole
        std::vector<int32_t> tokens = {SEP};
        for (int i = 0; i < 500; ++i) { tokens.push_back(3); tokens.push_back(4); tokens.push_back(i); }
        const std::vector<int32_t> sa = bounded_sa(tokens, 8);
        write_corpus_gguf(path, tokens, sa, 8, SEP, "tk");
        CorpusStore s;
        std::string err;
        check(s.open(path, err), "open a store with a wide range");
        int32_t out[64];
        const int32_t ctx[] = {3, 4};
        int ns = 0;
        const int n = s.lookup(ctx, 2, 2, out, 8, &ns);
        check(n == 2 && ns == 8, "the wide-range match is found, eight samples taken");
        bool spread = false;
        for (int i = 1; i < n; ++i)
            if (out[i] != out[0]) spread = true;
        check(spread, "the samples are spread across the range, not its first entries");
        check(s.lookups() == 1 && s.hits() == 1, "lookup stats count the calls and the hits");
        s.close();
    }
    {   // refusals: a missing file, a plain GGUF, a lying n_tokens, another tokenizer
        CorpusStore s;
        std::string err;
        check(!s.open("/tmp/opencode/corpus_store_test_missing.gguf", err) && !err.empty(),
              "a missing file is refused");
        const std::vector<int32_t> tokens = {1, 2, 3};
        write_corpus_gguf(path, tokens, bounded_sa(tokens, 8), 8, SEP, "tk", 999);
        check(!s.open(path, err), "metadata that disagrees with the tensors is refused");
        write_corpus_gguf(path, tokens, bounded_sa(tokens, 8), 8, SEP, "tk");
        check(!s.open(path, err, "another-tokenizer"), "a store built for another tokenizer is refused");
        check(s.open(path, err, "tk"), "the right tokenizer is accepted");
        check(s.open(path, err, "", 3), "a separator above the vocabulary is accepted");
        check(!s.open(path, err, "", SEP), "a separator at or below the vocabulary is refused");
        s.close();
    }
    {   // the corpus is the drafter's second vote source: it proposes where the per-sequence index cannot
        const std::vector<int32_t> tokens = {SEP, 10, 11, 12, 55, 56, 57, SEP, 20, 21};
        write_corpus_gguf(path, tokens, bounded_sa(tokens, 8), 8, SEP, "tk");
        CorpusStore s;
        std::string err;
        check(s.open(path, err), "open a store for the drafter's corpus vote");
        SuffixDrafter d;
        d.set_corpus(&s, 3, 0.5);
        const std::vector<int32_t> hist = {30, 31, 10, 11, 12};      // 10,11,12 is new to this history
        d.append(hist.data(), hist.size());
        int32_t out[8];
        const int n = d.propose(3, out);
        check(n == 3 && out[0] == 55 && out[2] == 57, "the corpus proposes the continuation the history lacks");
        check(d.last_match() == 3 && d.last_support() == 1, "its match is 3, one corpus occurrence");
        check(d.last_source() == 1, "the vote came from the corpus");
        // without the corpus the same history proposes nothing
        SuffixDrafter plain;
        plain.append(hist.data(), hist.size());
        check(plain.propose(3, out) == 0, "without the corpus: nothing");
    }
    {   // both stores vote when their matches are the same length; the heavier continuation wins
        const std::vector<int32_t> tokens = {SEP, 10, 11, 12, 88, SEP, 40, 10, 11, 12, 88, SEP, 41, 10, 11, 12, 88};
        write_corpus_gguf(path, tokens, bounded_sa(tokens, 8), 8, SEP, "tk");
        CorpusStore s;
        std::string err;
        check(s.open(path, err), "open a store with three corpus occurrences");
        // one local occurrence (weight 1.0) against three corpus ones (weight 1.5): the corpus wins
        SuffixDrafter d;
        d.set_corpus(&s, 3, 0.5);
        const std::vector<int32_t> hist = {10, 11, 12, 77, 30, 31, 10, 11, 12};
        d.append(hist.data(), hist.size());
        int32_t out[8];
        const int n = d.propose(2, out);
        check(n == 1 && out[0] == 88, "three corpus votes outweigh one local vote");
        check(d.last_support() == 3, "three corpus occurrences voted for the winner");
        check(d.last_source() == 1, "the winning continuation's votes came from the corpus");
        // one local occurrence against one corpus one: the local vote is the heavier, and it wins
        const std::vector<int32_t> one = {SEP, 10, 11, 12, 88};
        write_corpus_gguf(path, one, bounded_sa(one, 8), 8, SEP, "tk");
        CorpusStore s1;
        check(s1.open(path, err), "open a store with one corpus occurrence");
        SuffixDrafter d1;
        d1.set_corpus(&s1, 3, 0.5);
        d1.append(hist.data(), hist.size());
        const int n1 = d1.propose(2, out);
        check(n1 == 2 && out[0] == 77 && out[1] == 30, "one local vote outweighs one corpus vote");
        check(d1.last_source() == 0, "the winning continuation's vote came from the history");
    }
    {   // the longer match wins outright: the corpus's longer match beats the local's shorter one
        const std::vector<int32_t> tokens = {SEP, 70, 10, 11, 12, 13, 99};
        write_corpus_gguf(path, tokens, bounded_sa(tokens, 8), 8, SEP, "tk");
        CorpusStore s;
        std::string err;
        check(s.open(path, err), "open a store with a longer match than the history");
        SuffixDrafter d;
        d.set_corpus(&s, 3, 0.5);
        const std::vector<int32_t> hist = {10, 11, 12, 77, 30, 31, 10, 11, 12, 13};   // no local match
        d.append(hist.data(), hist.size());
        int32_t out[8];
        const int n = d.propose(2, out);
        check(d.last_match() == 4 && n == 1 && out[0] == 99, "the corpus's four-token match wins outright");
        check(d.last_source() == 1, "only the corpus voted at that length");
    }
    {   // min_corpus_order: a short corpus match is not asked for
        const std::vector<int32_t> tokens = {SEP, 10, 11, 12, 55, 56, 57};
        write_corpus_gguf(path, tokens, bounded_sa(tokens, 8), 8, SEP, "tk");
        CorpusStore s;
        std::string err;
        check(s.open(path, err), "open a store with a three-token match only");
        SuffixDrafter d;
        d.set_corpus(&s, 7, 0.5);
        const std::vector<int32_t> hist = {30, 31, 10, 11, 12};
        d.append(hist.data(), hist.size());
        int32_t out[8];
        check(d.propose(3, out) == 0, "a three-token corpus match is below min_corpus_order");
        d.set_corpus(&s, 3, 0.5);
        check(d.propose(3, out) == 3 && out[0] == 55, "the same match is taken when min_corpus_order allows it");
    }
    std::printf("corpus_store tests: %s\n", g_fail ? "FAILED" : "OK");
}

}  // namespace

int main() {
    unit_tests();
    return g_fail ? 1 : 0;
}
