"""tools/build_corpus.py: the lookup drafter's corpus store builder.

Run: python -m unittest tools.test_build_corpus

Checks the token stream's separators, the bounded-prefix suffix array against a brute-force sort, the GGUF
round-trip through the reader the engine uses, and that `verify` refuses a suffix array that is not a
permutation or not ordered. The C++ side of the same contract is src/ngram/corpus_store_test.cpp.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

import numpy as np

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bc = _load("build_corpus")

DOC_SEP = 1 << 30


class BuildStream(unittest.TestCase):
    def test_separator_before_every_document(self):
        out = bc.build_stream([[5, 6], [7], []], DOC_SEP).tolist()
        self.assertEqual(out, [DOC_SEP, 5, 6, DOC_SEP, 7])

    def test_empty_corpus(self):
        self.assertEqual(bc.build_stream([], DOC_SEP).size, 0)


class BoundedSuffixArray(unittest.TestCase):
    def test_matches_a_brute_force_sort_of_the_bounded_keys(self):
        toks = bc.build_stream([[5, 6, 7, 8], [9, 5, 6, 10], [11, 12]], DOC_SEP)
        sa = bc.bounded_suffix_array(toks, 8)
        n = toks.size
        keys = [tuple((toks[i:i + 8].tolist() + [-1] * 8)[:8]) for i in range(n)]
        self.assertEqual(sa.tolist(), sorted(range(n), key=lambda i: keys[i]))

    def test_is_a_permutation(self):
        toks = bc.build_stream([[3, 1, 4, 1, 5], [9, 2, 6]], DOC_SEP)
        sa = bc.bounded_suffix_array(toks, 4)
        self.assertEqual(sorted(sa.tolist()), list(range(toks.size)))

    def test_shorter_than_the_order(self):
        toks = bc.build_stream([[1, 2]], DOC_SEP)
        sa = bc.bounded_suffix_array(toks, 8)
        self.assertEqual(sa.tolist(), [1, 2, 0])       # real tokens order before the separator


class StoreRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "corpus.gguf"

    def _write(self, tokens, sa, **kw):
        bc.write_store(str(self.path), tokens, sa, kw.get("max_order", 8), DOC_SEP, "tk", "test")
        return bc.verify(str(self.path))

    def test_round_trip(self):
        tokens = bc.build_stream([[5, 6, 7, 8], [9, 5, 6, 10], [11, 12]], DOC_SEP)
        info = self._write(tokens, bc.bounded_suffix_array(tokens, 8))
        self.assertEqual(info["n"], tokens.size)
        self.assertEqual(info["max_order"], 8)
        self.assertEqual(info["doc_sep"], DOC_SEP)
        self.assertEqual(info["tokenizer"], "tk")
        self.assertTrue(self.path.stat().st_size > 0)

    def test_verify_refuses_a_suffix_array_that_is_not_a_permutation(self):
        tokens = bc.build_stream([[5, 6, 7, 8], [9, 5, 6, 10]], DOC_SEP)
        sa = bc.bounded_suffix_array(tokens, 8)
        sa[1] = sa[0]                                       # a duplicate: no longer a permutation
        bc.write_store(str(self.path), tokens, sa, 8, DOC_SEP, "tk", "test")
        with self.assertRaisesRegex(ValueError, "not a permutation"):
            bc.verify(str(self.path))

    def test_verify_refuses_an_unordered_suffix_array(self):
        tokens = bc.build_stream([[5, 6, 7, 8], [9, 5, 6, 10], [11, 12]], DOC_SEP)
        sa = np.array(sorted(range(tokens.size), key=lambda i: -i), dtype="<i4")   # reversed
        bc.write_store(str(self.path), tokens, sa, 8, DOC_SEP, "tk", "test")
        with self.assertRaisesRegex(ValueError, "out of order"):
            bc.verify(str(self.path))

    def test_verify_refuses_metadata_that_disagrees_with_the_tensors(self):
        tokens = bc.build_stream([[5, 6, 7]], DOC_SEP)
        w = bc.GGUFWriter()
        w.add("corpus.format", 1, "u32")
        w.add("corpus.max_order", 8, "u32")
        w.add("corpus.doc_sep", DOC_SEP, "u32")
        w.add("corpus.n_tokens", 999, "u64")                # a lie: the tensors say 4
        w.add("corpus.tokenizer", "tk", "string")
        w.add("corpus.build", "test", "string")
        w.add_i32("corpus.tokens", tokens)
        w.add_i32("corpus.sa", bc.bounded_suffix_array(tokens, 8))
        w.write(self.path)
        with self.assertRaisesRegex(ValueError, "metadata says 999"):
            bc.verify(str(self.path))


class Fingerprint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        (self.dir / "vocab.json").write_text(json.dumps({"a": 0, "b": 1}), encoding="utf-8")
        (self.dir / "merges.txt").write_text("a b\n", encoding="utf-8")
        (self.dir / "token_type.json").write_text("[1, 1]", encoding="utf-8")

    def test_stable_and_content_bound(self):
        files = bc.tokenizer_files(self.dir)
        first = bc.fingerprint(files)
        self.assertEqual(first, bc.fingerprint(files))
        (self.dir / "merges.txt").write_text("a b\nb a\n", encoding="utf-8")
        self.assertNotEqual(first, bc.fingerprint(files))


class ReadIds(unittest.TestCase):
    def test_commas_and_whitespace(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "dump.ids"
            p.write_text("1, 2,3\n4\t5", encoding="utf-8")
            self.assertEqual(bc.read_ids(p), [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
