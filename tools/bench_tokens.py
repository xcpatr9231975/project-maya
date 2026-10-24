#!/usr/bin/env python3
"""tools/bench_tokens.py - Maya's speed at several token sizes.

The standard benchmark (maya.py --bench) answers three questions of 256 tokens and reads two prompts.  This one
sweeps the sizes: a prompt read at N tokens (and the time to its first token) and an answer of N tokens (and its
rate), so a long chat, a long document and a short reply can each be compared on the same machine.

It drives the engine alone (build/maya --serve), the way the dashboard starts it, so what it measures is the engine
and not the HTTP server around it.  Maya must be stopped: the engine wants the whole GPU.

    tools/bench_tokens.py                          the default sweep, on the installed config
    tools/bench_tokens.py --prompts 512,2048 --answers 64,256
    tools/bench_tokens.py --config maya-maya-s-v2-iq2_xxs.json --out BENCHMARK.md

The table it prints is markdown, so it can go straight into a report.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_PROMPTS = (256, 512, 1024, 2048, 4096, 8192, 16384, 32768)
DEFAULT_ANSWERS = (32, 64, 128, 256, 512, 1024)
ANSWER_PROMPT = 2048          # the prompt the answer sweep reads (tokens)


def read_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def configs() -> list[pathlib.Path]:
    return sorted(ROOT.glob("maya-*.json"))


def pick_config(name: str | None) -> pathlib.Path:
    if name:
        path = pathlib.Path(name)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            sys.exit(f"no such config: {path}")
        return path
    found = configs()
    if not found:
        sys.exit("no config in this folder (run ./maya.sh --setup first)")
    # the newest one, so a second setup's config wins
    return max(found, key=lambda p: p.stat().st_mtime)


def main() -> int:
    ap = argparse.ArgumentParser(description="Maya's speed at several token sizes")
    ap.add_argument("--config", help="the config to measure (default: the newest one in this folder)")
    ap.add_argument("--prompts", default=",".join(str(x) for x in DEFAULT_PROMPTS),
                    help="the prompt sizes to read, in tokens (default: %s)" % ",".join(str(x) for x in DEFAULT_PROMPTS))
    ap.add_argument("--answers", default=",".join(str(x) for x in DEFAULT_ANSWERS),
                    help="the answer sizes to write, in tokens (default: %s)" % ",".join(str(x) for x in DEFAULT_ANSWERS))
    ap.add_argument("--answer-prompt", type=int, default=ANSWER_PROMPT,
                    help="the prompt the answer sweep reads (default: %d)" % ANSWER_PROMPT)
    ap.add_argument("--out", help="write the markdown table here (default: stdout)")
    args = ap.parse_args()

    prompts = [int(x) for x in args.prompts.split(",") if x.strip()]
    answers = [int(x) for x in args.answers.split(",") if x.strip()]

    cfg_path = pick_config(args.config)
    cfg = read_json(cfg_path)
    if not cfg.get("exe"):
        sys.exit(f"{cfg_path.name}: no engine in it")
    exe = pathlib.Path(cfg["exe"])
    if not exe.exists():
        sys.exit(f"the engine is not built: {exe}")

    import serve.server as SV
    import strata_tokenizer as ST

    eng_args = cfg.get("args") or []
    pack = pathlib.Path(eng_args[eng_args.index("--glm-pack") + 1]) if "--glm-pack" in eng_args[:-1] else None
    tp = pathlib.Path(cfg.get("tokenizer") or (pack / "tokenizer" if pack else ""))
    if not (tp / "vocab.json").exists():
        sys.exit(f"{cfg_path.name}: the tokenizer is missing ({tp})")
    tok = ST.Tokenizer.from_dir(tp)
    ctx = int(eng_args[eng_args.index("--max-context") + 1]) if "--max-context" in eng_args[:-1] else 32768

    text = "\n\n".join(p.read_text(encoding="utf-8", errors="replace")
                       for p in [ROOT / "README.md"] + sorted((ROOT / "docs").glob("*.md")) +
                       sorted((ROOT / "bench" / "results").glob("*.md")) if p.exists())
    doc = tok.encode(text)
    need = max(prompts + [args.answer_prompt]) + 8
    if len(doc) < need:
        sys.exit(f"the corpus is too short for a {need}-token prompt ({len(doc)} tokens)")

    log_path = ROOT / "maya-bench-tokens.log"
    print(f"benchmark: {cfg_path.name} (context {ctx}, {len(doc)} tokens of corpus)", flush=True)
    print("loading the model ...", flush=True)
    t0 = time.time()
    with open(log_path, "w", encoding="utf-8") as log:
        p = subprocess.Popen([cfg["exe"], "--serve"] + SV.engine_args(cfg), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=log, text=True, env=SV.child_env(cfg),
                             cwd=cfg.get("cwd") or str(ROOT), bufsize=1)

        def gen(ids, n_new):
            p.stdin.write(f"GEN {n_new} temperature=0 " + ",".join(map(str, ids)) + "\n")
            p.stdin.flush()
            while True:
                line = p.stdout.readline()
                if not line:
                    sys.exit(f"the engine stopped (its log: {log_path})")
                if line.startswith("DONE") or line.startswith("ERR"):
                    return line.split()

        while True:
            line = p.stdout.readline()
            if not line:
                sys.exit(f"the engine stopped while loading (its log: {log_path})")
            if line.startswith("READY"):
                break
        print(f"loaded in {time.time() - t0:.0f} s", flush=True)

        rows_p, rows_a = [], []
        # a warm-up: the first answer after a start is slower (the caches are cold)
        gen(tok.encode("[gMASK]<sop><|user|>\nHello<|assistant|>\n</think>"), 8)

        for i, n in enumerate(prompts):
            f = gen(doc[i * 997:i * 997 + n], 1)
            if f[0] != "DONE" or float(f[3]) <= 0:
                print(f"  {n}-token prompt: failed ({' '.join(f[:5])})", flush=True)
                continue
            p_ms = float(f[3])
            d_ms = float(f[4])
            rows_p.append((n, p_ms, n / p_ms * 1000.0, p_ms + d_ms))
            print(f"  prompt {n:6d} tokens: {p_ms / 1000:7.2f} s read ({n / p_ms * 1000:7.1f} tok/s), "
                  f"first token after {p_ms + d_ms:.0f} ms", flush=True)

        base = doc[:args.answer_prompt]
        for m in answers:
            f = gen(base, m)
            if f[0] != "DONE" or float(f[4]) <= 0:
                print(f"  {m}-token answer: failed ({' '.join(f[:5])})", flush=True)
                continue
            produced = int(f[1])
            d_ms = float(f[4])
            rows_a.append((m, produced, d_ms, produced / d_ms * 1000.0))
            print(f"  answer {m:6d} tokens: {produced} written in {d_ms / 1000:7.2f} s "
                  f"({produced / d_ms * 1000:7.1f} tok/s)", flush=True)

        p.stdin.write("QUIT\n")
        p.stdin.flush()
        try:
            p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            p.kill()

    lines = []
    lines.append("| prompt (tokens) | read (s) | read (tokens/s) | first token (ms) |")
    lines.append("| ---: | ---: | ---: | ---: |")
    for n, p_ms, rate, ttft in rows_p:
        lines.append(f"| {n} | {p_ms / 1000:.2f} | {rate:.0f} | {ttft:.0f} |")
    lines.append("")
    lines.append(f"| answer (tokens asked) | written | time (s) | decode (tokens/s) |")
    lines.append("| ---: | ---: | ---: | ---: |")
    for m, produced, d_ms, rate in rows_a:
        lines.append(f"| {m} | {produced} | {d_ms / 1000:.2f} | {rate:.1f} |")
    out = "\n".join(lines)
    print()
    print(out)
    if args.out:
        pathlib.Path(args.out).write_text(out + "\n", encoding="utf-8")
        print(f"\nwritten: {args.out}")
    print(f"engine log: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
