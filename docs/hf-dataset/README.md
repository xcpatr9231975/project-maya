---
license: mit
task_categories:
- text-generation
language:
- en
- zh
tags:
- benchmark
- inference-speed
- glm
- glm5-next
- mixture-of-experts
- aarch64
- dgx-spark
- gb10
- sm_121
pretty_name: Maya on a DGX Spark - inference benchmark
---

# Maya-S v2 IQ2_XXS on a DGX Spark (aarch64) - benchmark data

Speed measurements for **GLM-5.3-Flash** (the `Maya-S-v2-IQ2_XXS` quant, 320 B parameters / ~18 B active, 89.83 GiB
at 2.0625 bpw) on **one NVIDIA DGX Spark** (GB10, 20 ARM cores, 120 GB of RAM shared with the GPU), taken with two
independent engines on the same weights and the same machine:

- **Maya** - [Project Maya](https://github.com/mw00/project-maya) v1.1.26 + the aarch64 port
- **llama.cpp** - upstream master @ `272aad8b9`, the first release with the `glm5-next` architecture, built for
  `sm_121a`

**No weights are in this dataset.** The GGUF is
[peasantsmith/GLM-5.3-Flash-Maya-GGUF](https://huggingface.co/peasantsmith/GLM-5.3-Flash-Maya-GGUF) (folder
`Maya-S-v2-IQ2_XXS`, MIT), made from [zai-org/GLM-5.3-Flash](https://huggingface.co/zai-org/GLM-5.3-Flash) (MIT).

## Files

| File | What it is |
| --- | --- |
| `bench.csv` | every measurement, one per row (see the fields below) |
| `maya-bench.txt` | Maya's own `--bench` output on this machine, with the engine's log lines |
| `BENCHMARK.md` | the write-up: the tables, the method and the reasoning |

## `bench.csv` fields

| Field | Meaning |
| --- | --- |
| `implementation` | `maya` or `llama.cpp` |
| `metric` | `prefill` (reading the prompt) or `decode` (writing the answer) |
| `prompt_tokens` | tokens the prompt held (the context the run carried) |
| `generated_tokens` | tokens generated |
| `seconds` | wall time for that part |
| `tokens_per_second` | the measurement |
| `first_token_ms` | time to the first answer token (Maya's prompt sweep only) |
| `source` | the command that produced the row |

## Headline numbers

| | Maya | llama.cpp |
| --- | ---: | ---: |
| Prefill, 32K-token prompt | 756 tok/s | 239 tok/s (**3.2x**) |
| Prefill, 8K-token prompt | 746 tok/s | 277 tok/s (**2.7x**) |
| Prefill, 512-token prompt | 302 tok/s | 314 tok/s (tie) |
| Decode, short context | 17.4 tok/s | 17.7 tok/s (tie) |
| Decode, 2K-token context | 16.8 tok/s | 15.7 tok/s (1.07x) |
| Decode, 9K-token context | 16.1 tok/s | 15.6 tok/s (1.03x) |

Maya's standard `--bench` on this machine: **16.8 tokens/s** decoding (three 256-token greedy answers, thinking
off), **597 tokens/s** at a 2048-token prompt and **730 tokens/s** at 8192. Every expert resident
(12 096 of 12 096, 80.17 GB of the 120 GB shared with the GPU), a 99.97% cache hit.

## How the numbers were taken

- Maya: `./maya.sh --bench` (three greedy 256-token answers, thinking off, and prompts at 2048 / 8192 tokens) and
  `tools/bench_tokens.py`, which drives the engine alone the way the dashboard starts it - the prompt read at
  256-32768 tokens with the time to its first token, and answers of 32-1024 tokens after a prompt of 128 / 2048 /
  8192 tokens.
- llama.cpp: its own `llama-bench` (`-p` for prefill, `-n` for decode, 2 repetitions) and `llama-completion` with a
  real prompt, whose log reports the prompt-eval and the decode separately.
- Greedy decoding throughout (temperature 0), one request at a time, nothing else running on the GPU.

## The machine

One NVIDIA DGX Spark: a GB10 GPU (compute capability 12.1, `sm_121`), 20 ARM cores (Cortex-X925), and 120 GB of
LPDDR5X **shared between the CPU and the GPU** - the device has no VRAM of its own. Ubuntu 24.04 aarch64, driver
580.95.05, CUDA 13.0. The expert pool is sized from `/proc/meminfo`'s `MemAvailable` (the runtime's free figure
counts the model's page cache as used), leaving 16 GB of headroom for the system.

## Reproducing

```sh
git clone https://github.com/mw00/project-maya && cd project-maya
./maya.sh --check
./maya.sh --setup --yes --gpu 0 --context 131072 --no-vision --download-model
./serve-maya.sh stop
.venv/bin/python tools/bench_tokens.py          # the prompt and answer sweeps
./serve-maya.sh start && ./serve-maya.sh stop   # --bench wants the GPU to itself
.venv/bin/python maya.py --bench                # writes maya-bench.txt
```

## Credits

- **Project Maya** by Peasant Smith ([@mw00](https://github.com/mw00)) and its contributors - the engine, the
  server, the packer, the expert pool. [@needmorevram](https://github.com/needmorevram) is the co-maintainer and
  contributed the expert tiers across VRAM / RAM / SSD, the CPU expert kernels and the expert profile.
- **carlos patriawan** ([@xcpatr9231975](https://github.com/xcpatr9231975) /
  [xcpatr922JNP](https://huggingface.co/xcpatr922JNP)) - the aarch64 port, the benchmark tooling and these
  measurements.
- **[ggml / llama.cpp](https://github.com/ggml-org/llama.cpp)** (MIT, (c) 2023-2026 the ggml authors) - the
  quantization formats, the CPU dot products, the MMQ kernels, and the `glm5-next` implementation that is both the
  specification and the comparison baseline.
- **[zai-org](https://huggingface.co/zai-org)** for the GLM-5.3-Flash weights (MIT).

## Licence

MIT. This dataset holds measurements and documentation only; the model weights keep their own licences.
