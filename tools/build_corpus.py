"""Build the lookup drafter's corpus store: a suffix array over a token corpus, in one GGUF.

    python tools/build_corpus.py --gguf MODEL.gguf --text CORPUS_DIR --out corpus.gguf
    python tools/build_corpus.py --tokenizer DIR --text a.txt b.txt --out corpus.gguf
    python tools/build_corpus.py --tokenizer DIR --ids dump.ids --out corpus.gguf   # pretokenized

The store is the second source of the suffix-lookup drafter's votes (`SuffixDrafter::set_corpus`), the way
TandemLLM's ngram drafter reads a corpus suffix array (0xBakeer/TandemLLM, engine/drafters/ngram.py,
AGPL-3.0 - reimplemented from scratch here, no code is taken from it). The drafter asks it "has this n-gram
ever occurred" over tens of millions of tokens, where the per-sequence index answers only "has it occurred
since this request started". A lookup is two binary searches and costs microseconds.

WHAT IS IN THE FILE
    corpus.tokens   I32[n]   the token stream, documents separated by `doc_sep`
    corpus.sa       I32[n]   the stream's suffix array, sorted by the first `max_order` tokens
    corpus.max_order u32     the sort's bound, and the longest match the drafter will ask for
    corpus.doc_sep   u32      the separator: above every real token id, so a match never spans documents
    corpus.n_tokens  u64      n, cross-checked against the tensors
    corpus.tokenizer string   a fingerprint of the tokenizer the corpus was built with
    corpus.build     string   what built this file, and when

The file is a GGUF, so the engine opens it with the reader it already validates every model with; a file that
is not a corpus store is refused by its missing tensors rather than by a magic of its own.

The suffix array is sorted by a BOUNDED prefix rather than the whole suffix. That is not an approximation:
the drafter never matches more than `max_order` tokens, and a bounded sort is three doubling rounds of
`np.lexsort` instead of a full suffix-array construction, which matters at tens of millions of tokens.

A continuation stops at a document separator rather than running from the end of one document into the
beginning of the next, so a store built from unrelated files never proposes a sentence stitched from two.

Run: `python -m unittest tools.test_build_corpus` (the round-trip test).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gguf_writer import GGUFWriter  # noqa: E402

DEFAULT_MAX_ORDER = 8
DEFAULT_DOC_SEP = 1 << 30          # above every real token id, and it fits an int32
FORMAT = 1                        # the store's layout version, recorded in the file


# ------------------------------------------------------------------ the corpus
def read_ids(path) -> list[int]:
    """A pretokenized dump: comma- or whitespace-separated ids."""
    text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    return [int(v) for v in text.replace(",", " ").split()]


def fingerprint(files: list[pathlib.Path]) -> str:
    """A stable name for a tokenizer: the sha256 of its files, in the order given.

    The engine has no tokenizer of its own (the prompt arrives as ids), so it cannot recompute this. It
    records the store's fingerprint at open and reports it; `--corpus-tokenizer` makes a mismatch fatal.
    """
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode("utf-8"))
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:32]


def tokenizer_files(tokenizer_dir) -> list[pathlib.Path]:
    t = pathlib.Path(tokenizer_dir)
    return [t / "vocab.json", t / "merges.txt", t / "token_type.json"]


def build_stream(documents: list[list[int]], doc_sep: int) -> np.ndarray:
    """The documents joined by a separator BEFORE each: a continuation stops at the seam. An empty
    document contributes nothing rather than a separator of its own."""
    docs = [d for d in documents if d]
    n = sum(len(d) + 1 for d in docs)
    out = np.empty(n, dtype="<i4")
    at = 0
    for d in docs:
        out[at] = doc_sep
        at += 1
        out[at:at + len(d)] = d
        at += len(d)
    return out


def bounded_suffix_array(tokens: np.ndarray, max_order: int) -> np.ndarray:
    """The suffix array of `tokens` sorted by the first `max_order` tokens of each suffix.

    The tail's suffixes are padded with -1, which sorts before every real id, so a short suffix at the end of
    the stream orders as the prefix it is. `np.lexsort` takes its keys last-primary, so the keys are the
    padded stream's columns in reverse: column 0 (the first token of the suffix) is the primary key.
    """
    n = tokens.size
    padded = np.empty(n + max_order - 1, dtype="<i4")
    padded[:n] = tokens
    padded[n:] = -1
    keys = [padded[j:j + n] for j in range(max_order - 1, -1, -1)]
    return np.lexsort(keys).astype("<i4")


# ------------------------------------------------------------------ the file
def write_store(path, tokens: np.ndarray, sa: np.ndarray, max_order: int, doc_sep: int,
                fp: str, built_by: str) -> pathlib.Path:
    if tokens.size != sa.size:
        raise ValueError(f"{tokens.size} tokens but {sa.size} suffix-array entries")
    if doc_sep > np.iinfo(np.int32).max:
        raise ValueError(f"doc_sep {doc_sep} does not fit an int32")
    w = GGUFWriter()
    w.add("corpus.format", FORMAT, "u32")
    w.add("corpus.max_order", int(max_order), "u32")
    w.add("corpus.doc_sep", int(doc_sep), "u32")
    w.add("corpus.n_tokens", int(tokens.size), "u64")
    w.add("corpus.tokenizer", fp, "string")
    w.add("corpus.build", built_by, "string")
    w.add_i32("corpus.tokens", tokens)
    w.add_i32("corpus.sa", sa)
    return w.write(path)


def verify(path, sample: int = 100_000) -> dict:
    """Re-open a written store the way the engine will, and check what the lookup relies on.

    The suffix array must be a permutation of the stream's positions and must be ordered by the bounded
    prefix; both are checked directly against the tensor data (the reader parses headers only), the order on
    a sample of adjacent pairs above `sample` positions.
    """
    from gguf_reader import GGUFFile
    f = GGUFFile(pathlib.Path(path))
    md = f.metadata
    for key in ("corpus.format", "corpus.max_order", "corpus.doc_sep", "corpus.n_tokens",
                "corpus.tokenizer", "corpus.build"):
        if key not in md:
            raise ValueError(f"{path}: a corpus store records {key}")
    if int(md["corpus.format"]) != FORMAT:
        raise ValueError(f"{path}: format {md['corpus.format']}, this tool writes {FORMAT}")
    tt, st = f.find("corpus.tokens"), f.find("corpus.sa")
    for t in (tt, st):
        if len(t) != 1:
            raise ValueError(f"{path}: expected one corpus.tokens and one corpus.sa tensor")
        if t[0].type_name != "I32":
            raise ValueError(f"{path}: {t[0].name} is {t[0].type_name}, expected I32")
    tt, st = tt[0], st[0]
    n = tt.elements
    if st.elements != n or int(md["corpus.n_tokens"]) != n:
        raise ValueError(f"{path}: {n} tokens, {st.elements} sa entries, metadata says {md['corpus.n_tokens']}")
    with pathlib.Path(path).open("rb") as fh:
        fh.seek(f.data_start + tt.offset)
        tokens = np.frombuffer(fh.read(4 * n), dtype="<i4").copy()
        fh.seek(f.data_start + st.offset)
        sa = np.frombuffer(fh.read(4 * n), dtype="<i4").copy()

    seen = np.zeros(n, dtype=bool)
    seen[sa] = True                       # a permutation: every position exactly once
    if not seen.all():
        raise ValueError(f"{path}: the suffix array is not a permutation ({int((~seen).sum())} missing)")
    max_order = int(md["corpus.max_order"])
    padded = np.empty(n + max_order - 1, dtype="<i4")
    padded[:n] = tokens
    padded[n:] = -1
    keys = np.stack([padded[sa + j] for j in range(max_order)], axis=1)
    step = max(1, n // sample)
    idx = np.arange(0, n - 1, step)
    # lexicographic <= on the bounded keys, column by column: element-wise comparison is not order. `lt`
    # freezes at the first column where the earlier suffix is smaller; `eq` says they never differed.
    lt = np.zeros(idx.size, dtype=bool)
    eq = np.ones(idx.size, dtype=bool)
    for j in range(max_order):
        a, b = keys[idx, j], keys[idx + 1, j]
        lt |= eq & (a < b)
        eq &= a == b
    if not (lt | eq).all():
        raise ValueError(f"{path}: {int((~(lt | eq)).sum())} of {idx.size} sampled adjacent suffixes are out of order")
    return {"n": n, "max_order": max_order, "doc_sep": int(md["corpus.doc_sep"]),
            "tokenizer": md["corpus.tokenizer"], "build": md["corpus.build"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="the GGUF store to write")
    ap.add_argument("--gguf", help="a model GGUF: its tokenizer is extracted and fingerprinted")
    ap.add_argument("--tokenizer", help="a tokenizer directory (vocab.json, merges.txt, token_type.json)")
    ap.add_argument("--text", nargs="*", default=[], help="text files or directories of .txt/.md")
    ap.add_argument("--ids", nargs="*", default=[], help="pretokenized dumps (comma- or space-separated)")
    ap.add_argument("--max-order", type=int, default=DEFAULT_MAX_ORDER,
                    help="the longest match the drafter asks for, and the sort's bound (default 8)")
    ap.add_argument("--doc-sep", type=int, default=DEFAULT_DOC_SEP, help="the document separator id")
    ap.add_argument("--max-tokens", type=int, default=0, help="stop after this many tokens (0 = no cap)")
    ap.add_argument("--fingerprint", help="record this tokenizer fingerprint instead of computing one")
    args = ap.parse_args()

    if not (args.gguf or args.tokenizer):
        ap.error("one of --gguf or --tokenizer is required (the store records which tokenizer built it)")
    if not (args.text or args.ids):
        ap.error("nothing to build from: give --text files/directories or --ids dumps")

    import strata_tokenizer as ST
    if args.gguf:
        tdir = pathlib.Path(args.out).with_suffix("") .parent / (pathlib.Path(args.out).stem + ".tokenizer")
        cfg = ST.extract(args.gguf, tdir)
        tok = ST.Tokenizer.from_dir(tdir)
        fp_files = tokenizer_files(tdir)
        fp = args.fingerprint or fingerprint(fp_files)
        print(f"tokenizer: extracted from {args.gguf} ({cfg['vocab_size']} tokens), fingerprint {fp[:12]}")
    else:
        tok = ST.Tokenizer.from_dir(args.tokenizer)
        fp_files = tokenizer_files(args.tokenizer)
        fp = args.fingerprint or fingerprint(fp_files)
        print(f"tokenizer: {args.tokenizer}, fingerprint {fp[:12]}")

    documents: list[list[int]] = []
    files: list[pathlib.Path] = []
    for spec in args.text:
        p = pathlib.Path(spec)
        files.extend(sorted(p.rglob("*")) if p.is_dir() else [p])
    for f in files:
        if f.suffix not in (".txt", ".md", ".py", ".json", ".c", ".h", ".cpp", ".hpp"):
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            continue
        documents.append(tok.encode(text))
        print(f"  {f.name}: {len(documents[-1])} tokens")
    for spec in args.ids:
        documents.append(read_ids(spec))
        print(f"  {pathlib.Path(spec).name}: {len(documents[-1])} tokens (pretokenized)")

    if args.max_tokens:
        kept, total = [], 0
        for d in documents:
            if total >= args.max_tokens:
                break
            kept.append(d[:max(0, args.max_tokens - total)])
            total += len(kept[-1])
        documents = [d for d in kept if d]
        print(f"capped at {args.max_tokens} tokens")

    tokens = build_stream(documents, args.doc_sep)
    print(f"corpus: {len(documents)} documents, {tokens.size} tokens")
    print(f"sorting the suffix array by the first {args.max_order} tokens ...")
    sa = bounded_suffix_array(tokens, args.max_order)
    built = f"tools/build_corpus.py {datetime.date.today().isoformat()}"
    write_store(args.out, tokens, sa, args.max_order, args.doc_sep, fp, built)
    info = verify(args.out)
    print(f"wrote {args.out} ({pathlib.Path(args.out).stat().st_size / 1e6:.1f} MB): "
          f"{info['n']} tokens, suffix array ordered by {info['max_order']} tokens, "
          f"separator {info['doc_sep']}, tokenizer {info['tokenizer'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
