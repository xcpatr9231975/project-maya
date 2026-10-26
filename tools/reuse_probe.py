#!/usr/bin/env python3
"""The reuse test: two prompts that share a prefix and differ only in the needle word.

P1 = haystack + word A at 90%.  P2 = the same haystack + word B at 90% (only the word differs).
If the engine's conversation reuse restores P1's state for P2 without re-reading the changed
tail, the model answers A for P2 - it answers from a conversation it is not in.

  python /tmp/reuse_test.py
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.needle_bench import CHARS_PER_TOKEN, haystack, ask  # noqa: E402

TOKENS = 30 * 1024


CUT = 0.9   # overridden below

def prompt_with(word: str, salt: str, cut_frac: float = 0.9) -> str:
    text = haystack(int(TOKENS * CHARS_PER_TOKEN)) + f"\n\n[salt {salt}]\n"
    cut = int(len(text) * cut_frac)
    cut = text.rfind("\n", 0, cut) + 1 or cut
    needle = f"\nThe secret code word for this text is: {word}. Remember it.\n"
    return text[:cut] + needle + text[cut:] + \
        "\n\nWhat is the secret code word mentioned in the text above? Reply with the code word only."


def run(tag: str, word: str, salt: str, cut: float, url: str, key: str) -> str:
    p = prompt_with(word, salt, cut)
    # the word must appear exactly once (the haystack may mention a previous one)
    n = p.count(word)
    ans, ptoks, secs = ask(url, key, p, 900)
    ok = word in (ans or "")
    print(f"{tag}: word={word} occurrences_in_prompt={n} prompt_tokens={ptoks:,} {secs:.0f}s "
          f"-> answer={ans!r} {'OK' if ok else 'WRONG'}", flush=True)
    return ans or ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8009")
    ap.add_argument("--api-key", default="sk-local")
    ap.add_argument("--cut", type=float, default=0.9, help="where the needle sits, as a fraction of the prompt")
    ap.add_argument("--tokens", type=int, default=30 * 1024)
    ap.add_argument("--prime-tokens", type=int, default=0,
                    help="send a LONGER conversation first (default 0). The bleed needs one: a conversation "
                         "shorter than the previous one inherits its tail. 0 = the engine is clean, the probe "
                         "passes; 100000 reproduces it on an unfixed engine.")
    a = ap.parse_args()
    globals()["TOKENS"] = a.tokens
    url, key, cut = a.url, a.api_key, a.cut
    if a.prime_tokens > a.tokens:
        text = haystack(int(a.prime_tokens * CHARS_PER_TOKEN))
        prime = text + "\n\nReply with the word PRIMED and nothing else."
        t0 = time.time()
        ans, n, secs = ask(url, key, prime, 1800)
        print(f"prime: {n:,} prompt tokens in {secs:.0f} s (the engine's state now holds a longer conversation)",
              flush=True)
        time.sleep(2)
    word_a, word_b = "amber-lantern-311", "saffron-velvet-707"
    print(f"P1: the first conversation (needle at {cut:.0%})")
    r1 = run("P1", word_a, "one", cut, url, key)
    time.sleep(2)
    print("P2: the same prefix, a different word at the same position (a partial reuse expected)")
    r2 = run("P2", word_b, "one", cut, url, key)
    time.sleep(2)
    print("P3: P1 again, verbatim (a full reuse expected; the answer must still be A)")
    r3 = run("P3", word_a, "one", cut, url, key)
    bad = []
    if word_a not in r1:
        bad.append("P1")
    if word_b not in r2:
        bad.append("P2 (the answer came from the reused state, not this prompt)")
    if word_a not in r3:
        bad.append("P3")
    print("\nVERDICT: " + ("all correct" if not bad else "WRONG: " + "; ".join(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())