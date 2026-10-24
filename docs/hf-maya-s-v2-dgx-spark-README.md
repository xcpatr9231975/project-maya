---
license: mit
license_name: mit
library_name: transformers
pipeline_tag: text-generation
language:
- en
- zh
tags:
- glm
- glm5-next
- mixture-of-experts
- aarch64
- dgx-spark
- gb10
- inference
base_model: zai-org/GLM-5.3-Flash
---

# Maya-S v2 IQ2_XXS - a DGX Spark (aarch64) run of Project Maya

A 2.06-bit quant of **GLM-5.3-Flash** (320 B parameters, ~18 B active per token), run on **one NVIDIA DGX Spark
(GB10, 20 ARM cores, 120 GB of RAM shared with the GPU)** with
[Project Maya](https://github.com/mw00/project-maya)'s engine.

This card is the record of that run: the numbers, what the machine is, and where every part of the build comes from.
The engine's own write-up is [BENCHMARK.md](https://github.com/mw00/project-maya/blob/main/BENCHMARK.md) and
[docs/DGX_SPARK_BUILD.md](https://github.com/mw00/project-maya/blob/main/docs/DGX_SPARK_BUILD.md).

## Numbers (one DGX Spark, greedy, thinking off)

| | |
| --- | --- |
| Decode (256-token answers) | **16.8 tokens/s** |
| Prefill (8K-token prompt) | **730 tokens/s** |
| Prompt read at 16K tokens | 765 tokens/s |
| Expert cache hit | 99.97% |
| Experts resident | 12 096 of 12 096 (80.17 GB of the 120 GB shared with the GPU) |
| Context | 131 072 tokens (the model supports 1 M) |
| Model size | 89.83 GiB, 2.0625 bpw |

Measured with `./maya.sh --bench` (three 256-token greedy answers, thinking off) and
`tools/bench_tokens.py` (the sweep across prompt and answer sizes).

### Against upstream llama.cpp, on the same GPU and the same weights

The same GGUF on the same machine with **upstream llama.cpp** (`272aad8b9`, the first release with the `glm5-next`
architecture, built for `sm_121a`):

| | Maya | llama.cpp |
| --- | ---: | ---: |
| Prefill, 32K-token prompt | 756 tok/s | 239 tok/s (**3.2x**) |
| Prefill, 8K-token prompt | 746 tok/s | 277 tok/s (**2.7x**) |
| Prefill, 512-token prompt | 302 tok/s | 314 tok/s (tie) |
| Decode, short context | 17.4 tok/s | 17.7 tok/s (tie) |
| Decode, 2K-token context | 16.8 tok/s | 15.7 tok/s (1.07x) |
| Decode, 9K-token context | 16.1 tok/s | 15.6 tok/s (1.03x) |

## The machine

One NVIDIA DGX Spark: a GB10 GPU (compute capability 12.1, `sm_121`), 20 ARM cores (Cortex-X925), and 120 GB of
LPDDR5X **shared between the CPU and the GPU** - the device has no VRAM of its own. Ubuntu 24.04 aarch64, driver
580.95.05, CUDA 13.0.

That is the whole point of this run: a machine whose GPU memory is the system's RAM. The engine sizes its expert
pool from `/proc/meminfo`'s `MemAvailable` rather than the runtime's free figure (which counts the model's
reclaimable page cache as used), leaves 16 GB of headroom for the system and a 1024 MiB reserve for the driver, and
keeps a RAM tier and a disk tier below the pool.

## What runs it

| | |
| --- | --- |
| Engine | [Project Maya](https://github.com/mw00/project-maya) v1.1.26 + the aarch64 port |
| Tensor library | [ggml / llama.cpp](https://github.com/ggml-org/llama.cpp) (MIT), pinned at `3cf03257` - the quant formats, ggml-cpu's NEON dot products (the expert kernels on aarch64) and the prompt path's MMQ kernels |
| Model weights | [GLM-5.3-Flash](https://huggingface.co/zai-org/GLM-5.3-Flash) by zai-org (MIT) |
| Quant | `Maya-S-v2-IQ2_XXS`, 89.83 GiB in three GGUF shards, packed with the project's own `tools/iq_pack.py` |

## Credits

- **Project Maya** by Peasant Smith ([@mw00](https://github.com/mw00)) and its contributors - the engine, the
  server, the packer, the expert pool. [@needmorevram](https://github.com/needmorevram) is the co-maintainer and
  contributed the expert tiers across VRAM / RAM / SSD, the CPU expert kernels and the expert profile.
- **carlos patriawan** ([@xcpatr9231975](https://github.com/xcpatr9231975) /
  [xcpatr922JNP](https://huggingface.co/xcpatr922JNP)) - the aarch64 port and this run's numbers.
- **[ggml / llama.cpp](https://github.com/ggml-org/llama.cpp)** (MIT, (c) 2023-2026 the ggml authors) - the
  quantization formats, the CPU dot products and the MMQ kernels, and the `glm5-next` architecture implementation
  that is the specification and the comparison baseline here.
- **[zai-org](https://huggingface.co/zai-org)** for the GLM-5.3-Flash weights (MIT).

## Licence

MIT, like the projects above. The notices of ggml / llama.cpp and of Project Maya stay with every copy; the model
weights keep zai-org's MIT licence.
