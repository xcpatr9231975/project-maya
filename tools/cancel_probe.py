#!/usr/bin/env python3
"""tools/cancel_probe.py - the cancel-mid-prefill window: does a request that arrives while
the engine unwinds a cancelled prefill get answered from a poisoned state?

A long prefill is aborted by the client mid-read; the server's drain gives up after
STRATA_DRAIN_S (60 s) - "the engine may still be writing it" - and the next client's
request arrives while the engine is still unwinding the cancelled one.  The question:
does that request get answered from a state that still holds the cancelled conversation?

  python tools/cancel_probe.py --url http://127.0.0.1:8009 --api-key sk-local

VALID ONLY ON AN EXCLUSIVE BOX: the 2026-10-26 14:0x run was invalidated by a server
shutdown and another client's traffic mid-test.  The engine must be serving nobody else.

A: a 90k-token prompt, the client aborts after 25 s (mid-prefill)
B: immediately, a SHORT prompt with its own needle word  -> must answer B
C: a fresh prompt with ANOTHER needle word               -> must answer C
D: A's prompt again, a fresh client                      -> must answer A
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.needle_bench import CHARS_PER_TOKEN, haystack  # noqa: E402

import argparse
ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--url", default="http://127.0.0.1:8009")
ap.add_argument("--api-key", default="sk-local")
_url = ap.parse_args()
URL, KEY = _url.url, _url.api_key
WORDS = {"A": "cobalt-harbor-551", "B": "velvet-saffron-909", "C": "quartz-juniper-142", "D": "ember-glacier-733"}


def prompt(word: str, tokens: int) -> str:
    text = haystack(int(tokens * CHARS_PER_TOKEN))
    cut = int(len(text) * 0.9)
    cut = text.rfind("\n", 0, cut) + 1 or cut
    needle = f"\nThe secret code word for this text is: {word}. Remember it.\n"
    return text[:cut] + needle + text[cut:] + \
        "\n\nWhat is the secret code word mentioned in the text above? Reply with the code word only."


def send(word: str, tokens: int, max_new: int = 24, timeout: float = 900) -> tuple[str, str, float]:
    body = {"model": "glm-5.3-flash", "max_tokens": max_new, "temperature": 0,
            "messages": [{"role": "user", "content": prompt(word, tokens)}]}
    req = urllib.request.Request(URL.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
        ans = out["choices"][0]["message"].get("content") or ""
        kind = "ok"
    except urllib.error.HTTPError as e:
        ans, kind = e.read().decode("utf-8", "replace")[:160], f"http {e.code}"
        out = {}
    except Exception as e:  # noqa: BLE001
        ans, kind, out = f"{type(e).__name__}: {e}", "aborted/failed", {}
    return ans, kind, time.time() - t0


def check(tag: str, word: str, ans: str, secs: float, kind: str) -> None:
    ok = word in (ans or "")
    print(f"{tag}: {kind}, {secs:.0f}s -> {ans!r} {'OK' if ok else '*** WRONG ***'}", flush=True)
    if not ok:
        WRONG.append((tag, ans))


WRONG: list = []


def main() -> int:
    print("A: a 90k-token prompt, the client aborts at 25 s (mid-prefill, the prefill needs ~170 s)")
    try:
        send(WORDS["A"], 90 * 1024, timeout=25)
        print("   A returned before the timeout?! (no abort)")
    except Exception as e:
        print(f"   aborted as intended ({type(e).__name__}) at ~25 s", flush=True)
    print("B: a SHORT prompt with its own needle, right away")
    ans, kind, secs = send(WORDS["B"], 2 * 1024)
    check("B", WORDS["B"], ans, secs, kind)
    print("C: another fresh prompt, another needle")
    ans, kind, secs = send(WORDS["C"], 2 * 1024)
    check("C", WORDS["C"], ans, secs, kind)
    print("D: a 30k prompt, another needle (a fresh client)")
    ans, kind, secs = send(WORDS["D"], 30 * 1024)
    check("D", WORDS["D"], ans, secs, kind)
    print(f"\nverdict: {len(WRONG)} wrong answer(s) of 3 checked")
    for tag, ans in WRONG:
        print(f"  {tag}: {ans!r}")
    return 1 if WRONG else 0


if __name__ == "__main__":
    sys.exit(main())