// include/strata/ngram/corpus_store.hpp - the lookup drafter's corpus: a suffix array over a token stream.
//
// The second source of the suffix-lookup drafter's votes. The per-sequence index (SuffixDrafter) answers "has
// this n-gram occurred since this request started"; this answers "has it ever occurred", over a corpus of tens
// of millions of tokens built once by tools/build_corpus.py. That is the difference between a drafter that can
// only quote the prompt back and one that can finish a phrase the model has seen elsewhere - and it costs no
// weights, which is the point: on a step where it fires, the block costs the verify pass and nothing else.
//
// The file is a GGUF (corpus.tokens and corpus.sa as I32 tensors, plus corpus.* metadata), so it opens through
// the same validated reader every model uses and a file that is not a corpus store is refused by its missing
// tensors. Both arrays are mapped read-only, so a 40-million-token store costs address space and only the
// pages a lookup touches.
//
// A lookup is two binary searches over the suffix array, which is sorted by the first `max_order` tokens of
// each suffix - a bounded sort, because the drafter never matches more than max_order tokens. A short n-gram
// occurs tens of thousands of times in thirty million tokens, so a wide range is sampled rather than read
// whole: the samples keep the vote counts an estimate of the distribution instead of of the alphabet.
//
// Declining is a first-class outcome here too: `min_order` is the caller's, and the drafter asks this store
// only for matches long enough to be worth a verify pass (TandemLLM's ngram drafter tunes it to 7).
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace strata::ngram {

class CorpusStore {
public:
    CorpusStore();
    ~CorpusStore();
    CorpusStore(const CorpusStore&) = delete;
    CorpusStore& operator=(const CorpusStore&) = delete;

    /// Open a store built by tools/build_corpus.py. `expected_tokenizer` (non-empty) refuses a store built
    /// for another tokenizer; `vocab_size` (> 0) checks the separator sits above every real token id.
    bool open(const std::string& path, std::string& err, const std::string& expected_tokenizer = "",
              int64_t vocab_size = 0);
    void close();
    bool is_open() const;

    /// The longest suffix of `context`, at most max_order and at least `min_order` long, that occurs in the
    /// corpus. Writes up to `max_samples` continuation START positions to `out` (spread across the suffix
    /// array's range for that match), the count to `n_samples` when it is not null, and returns the match
    /// length, or 0 when there is none.
    int lookup(const int32_t* context, size_t n, int min_order, int32_t* out, int max_samples,
               int* n_samples = nullptr) const;

    /// Up to `k` tokens from `pos`, stopping at a document separator; returns how many.
    int continuation(int32_t pos, int k, int32_t* out) const;

    uint32_t max_order() const { return max_order_; }
    uint32_t doc_sep() const { return doc_sep_; }
    uint64_t n_tokens() const { return n_; }
    const std::string& tokenizer() const { return tokenizer_; }
    const std::string& build() const { return build_; }
    const std::string& path() const { return path_; }

    uint64_t lookups() const { return lookups_; }
    uint64_t hits() const { return hits_; }
    double lookup_us() const { return lookup_us_; }      // mean microseconds per lookup, hits included

private:
    int cmp_suffix(size_t sa_i, const int32_t* pattern, int k) const;
    void range(const int32_t* pattern, int k, size_t& lo, size_t& hi) const;

    std::string path_, tokenizer_, build_;
    uint8_t* map_ = nullptr;               // the whole file, read-only
    size_t map_bytes_ = 0;
    const int32_t* tokens_ = nullptr;
    const int32_t* sa_ = nullptr;
    uint64_t n_ = 0;
    uint32_t max_order_ = 0, doc_sep_ = 0;
    mutable uint64_t lookups_ = 0, hits_ = 0;
    mutable double lookup_us_ = 0.0;
};

}  // namespace strata::ngram
