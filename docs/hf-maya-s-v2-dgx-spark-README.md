---
license: mit
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
- benchmark
base_model:
- peasantsmith/GLM-5.3-Flash-Maya-GGUF
- zai-org/GLM-5.3-Flash
---

# Maya-S v2 IQ2_XXS on a DGX Spark (aarch64) - numbers and build notes

**This repository does not host the weights.** It is the record of one run: the 2.06-bit quant of GLM-5.3-Flash on a
single NVIDIA DGX Spark (GB10, 20 ARM cores, 120 GB of RAM shared with the GPU), with
[Project Maya](https://github.com/mw00/project-maya)'s engine.

## Where the weights are

The GGUF this ran is **already on Hugging Face**:

- repo: **[peasantsmith/GLM-5.3-Flash-Maya-GGUF](https://huggingface.co/peasantsmith/GLM-5.3-Flash-Maya-GGUF)**
  (folder `Maya-S-v2-IQ2_XXS`, revision `main`, MIT)
- 3 shards, 89.83 GiB, 2.0625 bpw:
  `GLM-5.3-Flash-Maya-S-v2-IQ2_XXS-0000{1,2,3}-of-00003.gguf`
- the original weights: **[zai-org/GLM-5.3-Flash](https://huggingface.co/zai-org/GLM-5.3-Flash)** (MIT)

```sh
huggingface-cli download peasantsmith/GLM-5.3-Flash-Maya-GGUF \
    --include "Maya-S-v2-IQ2_XXS/*.gguf" --local-dir ./Maya-S-v2-IQ2_XXS
```

The quant: error-feedback-rounded IQ2_XXS gate/up experts, IQ2_S / IQ3_XXS down projections, Q6_K attention and the
MTP draft block, made from Z.ai's FP8 release by Project Maya. The SHA-256 of each shard is verified by the
project's own setup (`maya.py`).

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

Measured with `./maya.sh --bench` (three 256-token greedy answers, thinking off) and `tools/bench_tokens.py` (the
sweep across prompt and answer sizes).

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
| The aarch64 port | [docs/DGX_SPARK.md](https://github.com/mw00/project-maya/blob/main/docs/DGX_SPARK.md), [docs/DGX_SPARK_BUILD.md](https://github.com/mw00/project-maya/blob/main/docs/DGX_SPARK_BUILD.md) |
| The benchmark | [BENCHMARK.md](https://github.com/mw00/project-maya/blob/main/BENCHMARK.md) |

```sh
git clone https://github.com/mw00/project-maya && cd project-maya
./maya.sh --setup --yes --gpu 0 --context 131072 --no-vision --download-model
./serve-maya.sh start        # http://127.0.0.1:8080/v1 (OpenAI-compatible)
```

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

MIT, like the projects above. This repository holds documentation and measurements only; the weights keep their
own licences (the quant: MIT, from peasantsmith's repo; the originals: zai-org's MIT).
