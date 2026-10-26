// src/ngram/corpus_store.cpp - see include/strata/ngram/corpus_store.hpp.
#include "strata/ngram/corpus_store.hpp"

#include "strata/artifact/gguf_reader.hpp"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <memory>

#if defined(_WIN32)
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace strata::ngram {

namespace {

/// Map `path` read-only. A corpus is a build artifact, not a model: it is mapped rather than read so a store
/// larger than RAM still works and only the pages a lookup touches become resident.
bool map_read_only(const std::string& path, uint8_t*& out, size_t& bytes, std::string& err) {
#if defined(_WIN32)
    HANDLE f = CreateFileA(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL, nullptr);
    if (f == INVALID_HANDLE_VALUE) { err = "CorpusStore: cannot open " + path; return false; }
    LARGE_INTEGER size{};
    if (!GetFileSizeEx(f, &size)) { CloseHandle(f); err = "CorpusStore: cannot size " + path; return false; }
    HANDLE m = CreateFileMappingA(f, nullptr, PAGE_READONLY, 0, 0, nullptr);
    if (m == nullptr) { CloseHandle(f); err = "CorpusStore: cannot map " + path; return false; }
    out = (uint8_t*) MapViewOfFile(m, FILE_MAP_READ, 0, 0, 0);
    CloseHandle(m);
    CloseHandle(f);
    if (out == nullptr) { err = "CorpusStore: cannot view " + path; return false; }
    bytes = (size_t) size.QuadPart;
    return true;
#else
    const int fd = ::open(path.c_str(), O_RDONLY);
    if (fd < 0) { err = "CorpusStore: cannot open " + path; return false; }
    struct stat st {};
    if (::fstat(fd, &st) != 0) { ::close(fd); err = "CorpusStore: cannot stat " + path; return false; }
    bytes = (size_t) st.st_size;
    void* view = ::mmap(nullptr, bytes, PROT_READ, MAP_SHARED, fd, 0);
    ::close(fd);
    if (view == MAP_FAILED) { err = "CorpusStore: cannot map " + path; return false; }
    out = (uint8_t*) view;
    return true;
#endif
}

void unmap(uint8_t* view, size_t bytes) {
    if (view == nullptr) return;
#if defined(_WIN32)
    (void) bytes;
    UnmapViewOfFile(view);
#else
    ::munmap(view, bytes);
#endif
}

int64_t meta_u64(const strata::GgufFile& gf, const char* key, int64_t missing_ok) {
    const strata::MetaValue* v = gf.get(key);
    if (v == nullptr || !v->is_num()) return missing_ok;
    return (int64_t) v->num();
}

std::string meta_str(const strata::GgufFile& gf, const char* key) {
    const strata::MetaValue* v = gf.get(key);
    if (v == nullptr || v->type != strata::MetaType::STRING) return "";
    return v->s;
}

}  // namespace

CorpusStore::CorpusStore() = default;

CorpusStore::~CorpusStore() { close(); }

bool CorpusStore::open(const std::string& path, std::string& err, const std::string& expected_tokenizer,
                       int64_t vocab_size) {
    close();
    std::unique_ptr<strata::GgufFile> gf;
    try {
        gf = std::make_unique<strata::GgufFile>(path);
    } catch (const std::exception& e) {
        err = std::string("CorpusStore: ") + e.what();
        return false;
    }
    const strata::TensorInfo* tt = gf->find("corpus.tokens");
    const strata::TensorInfo* st = gf->find("corpus.sa");
    if (tt == nullptr || st == nullptr) {
        err = "CorpusStore: " + path + " has no corpus.tokens / corpus.sa - not a corpus store "
              "(tools/build_corpus.py writes one)";
        return false;
    }
    // GGML type 26 is I32. Checked by id, not by name: the engine's name table only carries the types its
    // models use, and a corpus store's arrays are not one of them.
    if (tt->type != 26 || st->type != 26) {
        err = "CorpusStore: corpus.tokens / corpus.sa must be I32 (ggml type 26)";
        return false;
    }
    n_ = tt->elements();
    const int64_t meta_n = meta_u64(*gf, "corpus.n_tokens", -1);
    if (st->elements() != n_ || meta_n != (int64_t) n_) {
        char b[256];
        std::snprintf(b, sizeof b, "CorpusStore: %llu tokens, %llu suffix-array entries, metadata says %lld - "
                      "the file is inconsistent", (unsigned long long) n_, (unsigned long long) st->elements(),
                      (long long) meta_n);
        err = b;
        return false;
    }
    const int64_t format = meta_u64(*gf, "corpus.format", 0);
    if (format != 1) {
        err = "CorpusStore: format " + std::to_string(format) + " is not the one this engine reads (1)";
        return false;
    }
    max_order_ = (uint32_t) meta_u64(*gf, "corpus.max_order", 0);
    doc_sep_ = (uint32_t) meta_u64(*gf, "corpus.doc_sep", 0);
    if (max_order_ < 1 || max_order_ > 64) {
        err = "CorpusStore: corpus.max_order " + std::to_string(max_order_) + " is out of range";
        return false;
    }
    if (doc_sep_ == 0 || (vocab_size > 0 && (int64_t) doc_sep_ <= vocab_size)) {
        char b[256];
        std::snprintf(b, sizeof b, "CorpusStore: the separator %u must sit above every real token id (the model "
                      "has %lld)", (unsigned) doc_sep_, (long long) vocab_size);
        err = b;
        return false;
    }
    tokenizer_ = meta_str(*gf, "corpus.tokenizer");
    build_ = meta_str(*gf, "corpus.build");
    if (!expected_tokenizer.empty() && tokenizer_ != expected_tokenizer) {
        err = "CorpusStore: " + path + " was built for tokenizer " + tokenizer_ + ", this run expects " +
              expected_tokenizer;
        return false;
    }
    const uint64_t tokens_at = gf->data_start() + tt->offset;
    const uint64_t sa_at = gf->data_start() + st->offset;
    if (tokens_at + n_ * 4 > gf->file_size() || sa_at + n_ * 4 > gf->file_size()) {
        err = "CorpusStore: the arrays extend past the end of " + path;
        return false;
    }
    uint8_t* view = nullptr;
    size_t bytes = 0;
    if (!map_read_only(path, view, bytes, err)) return false;
    // Both tensors are I32 and 4-byte aligned inside the data section; the reader has checked they fit.
    tokens_ = (const int32_t*) (view + tokens_at);
    sa_ = (const int32_t*) (view + sa_at);
    if (n_ == 0 || sa_[0] < 0 || sa_[n_ - 1] < 0 || (uint64_t) sa_[0] >= n_ || (uint64_t) sa_[n_ - 1] >= n_) {
        err = "CorpusStore: the suffix array's ends are out of range - the file is truncated or corrupt";
        unmap(view, bytes);
        tokens_ = sa_ = nullptr;
        return false;
    }
    map_ = view;
    map_bytes_ = bytes;
    path_ = path;
    lookups_ = hits_ = 0;
    lookup_us_ = 0.0;
    return true;
}

void CorpusStore::close() {
    unmap(map_, map_bytes_);
    map_ = nullptr;
    map_bytes_ = 0;
    tokens_ = sa_ = nullptr;
    n_ = 0;
    max_order_ = doc_sep_ = 0;
    tokenizer_.clear();
    build_.clear();
    path_.clear();
    lookups_ = hits_ = 0;
    lookup_us_ = 0.0;
}

bool CorpusStore::is_open() const { return map_ != nullptr; }

int CorpusStore::cmp_suffix(size_t sa_i, const int32_t* pattern, int k) const {
    const int32_t* at = tokens_ + sa_[sa_i];
    const int64_t avail = (int64_t) n_ - (int64_t) sa_[sa_i];
    for (int j = 0; j < k; ++j) {
        const int32_t a = j < avail ? at[j] : -1;      // a suffix past the end orders as the prefix it is
        if (a != pattern[j]) return a < pattern[j] ? -1 : 1;
    }
    return 0;
}

void CorpusStore::range(const int32_t* pattern, int k, size_t& lo, size_t& hi) const {
    size_t l = 0, r = n_;
    while (l < r) {
        const size_t mid = l + (r - l) / 2;
        if (cmp_suffix(mid, pattern, k) < 0) l = mid + 1;
        else r = mid;
    }
    lo = l;
    r = n_;
    while (l < r) {
        const size_t mid = l + (r - l) / 2;
        if (cmp_suffix(mid, pattern, k) <= 0) l = mid + 1;
        else r = mid;
    }
    hi = l;
}

int CorpusStore::lookup(const int32_t* context, size_t n, int min_order, int32_t* out, int max_samples,
                        int* n_samples) const {
    const auto t0 = std::chrono::steady_clock::now();
    ++lookups_;
    const int top = (int) std::min<uint32_t>(max_order_, (uint32_t) n);
    const int floor_order = std::max(min_order, 1);
    const int cap = std::max(max_samples, 1);
    int found = 0, match = 0;
    for (int k = top; k >= floor_order; --k) {
        const int32_t* pattern = context + n - k;
        size_t lo, hi;
        range(pattern, k, lo, hi);
        const size_t width = hi - lo;
        if (width == 0) continue;
        ++hits_;
        const size_t count = std::min(width, (size_t) cap);
        for (size_t i = 0; i < count; ++i) {
            // a wide range is sampled across its whole width, so the votes estimate the distribution rather
            // than the alphabet: the first `count` entries of a huge range are all the same continuation
            const size_t at = width <= (size_t) cap ? lo + i : lo + (size_t) ((double) i * width / count);
            out[i] = sa_[at] + k;                     // the continuation starts k tokens into the match
        }
        found = (int) count;
        match = k;
        break;
    }
    if (n_samples != nullptr) *n_samples = found;
    lookup_us_ += std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - t0).count();
    return match;
}

int CorpusStore::continuation(int32_t pos, int k, int32_t* out) const {
    int n = 0;
    while (n < k && (uint64_t) (pos + n) < n_) {
        const int32_t t = tokens_[pos + n];
        if (t >= (int32_t) doc_sep_) break;           // a document seam: the continuation stops there
        out[n++] = t;
    }
    return n;
}

}  // namespace strata::ngram
