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

- **Maya** - [Project Maya](https://github.com/mw00/project-maya) v1.1.26 + the aarch64 port, **with and without the
  DFlash2 drafter** (`--dflash`, 0.7 GB) so the drafter's effect is measurable
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
| `drafter` | `DFlash2` (the config's drafter) or `none` - llama.cpp ran without one; the model has no MTP draft block, so it has no drafter of its own |
| `prompt_tokens` | tokens the prompt held (the context the run carried) |
| `generated_tokens` | tokens generated |
| `seconds` | wall time for that part |
| `tokens_per_second` | the measurement |
| `first_token_ms` | time to the first answer token (Maya's prompt sweep only) |
| `source` | the command that produced the row |

80 rows: Maya with the drafter (32), Maya without it (28), llama.cpp (20).

## Headline numbers

**With the DFlash2 drafter** (the config this machine runs):

| | Maya | llama.cpp (no drafter) |
| --- | ---: | ---: |
| Prefill, 32K-token prompt | 754 tok/s | 239 tok/s (**3.2x**) |
| Prefill, 8K-token prompt | 744 tok/s | 277 tok/s (**2.7x**) |
| Prefill, 512-token prompt | 303 tok/s | 314 tok/s (tie) |
| Decode, 256-token answer, short context | 22.9 tok/s | 17.5-17.8 tok/s (**1.29x**) |
| Decode, 256-token answer, 2K context | 22.9 tok/s | 15.7 tok/s (**1.46x**) |
| Decode, 256-token answer, 8K context | 21.1 tok/s | 15.6 tok/s (**1.35x**) |
| `--bench` (three 256-token answers) | **21.1 tok/s** | - |

**Without the drafter** (the same sweeps, before `--draft dflash2`):

| | Maya | llama.cpp |
| --- | ---: | ---: |
| Prefill, 32K-token prompt | 756 tok/s | 239 tok/s (**3.2x**) |
| Decode, short context | 17.4 tok/s | 17.7 tok/s (tie) |
| Decode, 2K-token context | 16.8 tok/s | 15.7 tok/s (1.07x) |
| `--bench` | **16.8 tok/s** | - |

The drafter does not change the prompt numbers at all, and its decode gain grows with the answer's length - a round
drafts up to 7 tokens and one pass checks them all, so 15.8 tokens/s at a 32-token answer (worse than no drafter: its
fixed cost dominates) but 24.6 at 1024. The setup's speed calibration measured the same document conversation
sampled: 16.13 without, 17.43 with (**+8%**), above its 5% threshold, so the config keeps the drafter. llama.cpp
cannot draft here - the model has no MTP block and this build has no DFlash2.

## How the numbers were taken

- Maya: `./maya.sh --bench` (three greedy 256-token answers, thinking off, and prompts at 2048 / 8192 tokens),
  `./maya.sh --speed-calibration` (the same document conversation sampled, ~1536 tokens, without and with the
  drafter) and `tools/bench_tokens.py`, which drives the engine alone the way the dashboard starts it - the prompt
  read at 256-32768 tokens with the time to its first token, and answers of 32-1024 tokens after a prompt of 128 /
  2048 / 8192 tokens. Both drafter settings were measured the same way.
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
