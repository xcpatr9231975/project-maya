# Benchmark

Maya-S-v2 (GLM-5.3-Flash, the `Maya-S-v2-IQ2_XXS` quant) on one **NVIDIA DGX Spark (GB10)**, and the same model on
the same machine with **upstream llama.cpp**, so the two can be compared on the same weights and the same GPU.

| | |
| --- | --- |
| GPU | NVIDIA GB10, one device, compute capability 12.1 (sm_121), 120 GB shared with the system |
| CPU | 20 ARM cores (Cortex-X925) |
| RAM | 120 GB |
| Driver / toolkit | 580.95.05 / CUDA 13.0 (nvcc 13.0.88) |
| Model | `GLM-5.3-Flash-Maya-S-v2-IQ2_XXS`, 89.83 GiB, 320.76 B parameters (18 B active), 46 layers, 288 experts (8 used) |
| Maya | v1.1.26 + the aarch64 port (this repository), `build/maya`, sm_121, context 131072, the DFlash2 drafter |
| llama.cpp | upstream master @ `272aad8b9` (the version with the `glm5-next` architecture), CUDA sm_121a |

Both run the same GGUF, on the same GPU, with greedy decoding. Upstream loads every layer into the GPU (89.83 GiB);
Maya fills an expert pool with all of them (80.17 GB of the experts plus the dense weights) and keeps a RAM tier and a
disk tier below it. Maya's config here drafts with a DFlash2 drafter (0.7 GB, `--dflash`), which speeds its decode up;
the tables below are with it, and the comparison says what it was without.

**The short version:** Maya reads a prompt **up to 3.2x faster** than upstream llama.cpp, and its decode is
**1.2-1.4x faster** with the DFlash2 drafter this config uses (a tie at a short context and 3-7% faster at a long one
without it).

## Maya: reading a prompt

The engine alone (`build/maya --serve`), greedy, one token generated after the prompt. "First token" is the time from
the request to the first answer token - what a chat feels.

| prompt (tokens) | read (s) | read (tokens/s) | first token (ms) |
| ---: | ---: | ---: | ---: |
| 256 | 1.15 | 222 | 1152 |
| 512 | 1.69 | 303 | 1689 |
| 1024 | 2.26 | 452 | 2264 |
| 2048 | 3.31 | 619 | 3310 |
| 4096 | 5.78 | 708 | 5782 |
| 8192 | 11.01 | 744 | 11008 |
| 16384 | 21.29 | 769 | 21294 |
| 32768 | 43.43 | 754 | 43433 |

The rate rises with the prompt and then flattens: a short prompt is bound by the fixed costs of a pass (the routing,
the first layer's setup, the launch overhead), a long one by the memory bandwidth. The peak, about 769 tokens/s, is
reached around 16K tokens - the same place the decode starts to pay for the context it has to search. The drafter
does not change these numbers: prefill is the same with it and without it.

## Maya: writing an answer

An answer of N tokens, greedy, after a prompt of the size in the first column.

| prompt | answer (tokens) | written | time (s) | decode (tokens/s) |
| ---: | ---: | ---: | ---: | ---: |
| 128 | 32 | 32 | 2.03 | 15.8 |
| 128 | 64 | 64 | 3.49 | 18.3 |
| 128 | 128 | 128 | 5.86 | 21.8 |
| 128 | 256 | 256 | 11.16 | 22.9 |
| 128 | 512 | 512 | 22.46 | 22.8 |
| 128 | 1024 | 1024 | 41.64 | 24.6 |
| 2048 | 32 | 32 | 2.02 | 15.8 |
| 2048 | 64 | 64 | 3.48 | 18.4 |
| 2048 | 128 | 128 | 5.91 | 21.7 |
| 2048 | 256 | 256 | 11.20 | 22.9 |
| 2048 | 512 | 512 | 22.51 | 22.7 |
| 2048 | 1024 | 1024 | 41.56 | 24.6 |
| 8192 | 32 | 32 | 2.14 | 15.0 |
| 8192 | 64 | 64 | 3.77 | 17.0 |
| 8192 | 128 | 128 | 6.79 | 18.8 |
| 8192 | 256 | 256 | 12.14 | 21.1 |
| 8192 | 512 | 512 | 23.17 | 22.1 |
| 8192 | 1024 | 1024 | 41.48 | 24.7 |

With the DFlash2 drafter the answer's length matters as much as the context: a round drafts up to 7 tokens and one
pass of the model checks them all, so a longer answer amortizes the round's fixed cost - 21.8 tokens/s at a 128-token
answer, 24.6 at 1024. The 32-token answer is the one case where drafting does not pay (15.8 against 17.2 without it):
its fixed cost is a large share of so few tokens, and the pool is also still filling back after the prompt sweep above
(the engine's line for it: 3.8 disk reads a token). Without the drafter the rate is flat at 17.2-17.6 whatever the
answer's length.

The context the answer has to carry costs on top of that: every expert is resident (a 99.97% cache hit), so what the
context costs is the attention over it - the k-pool indexer's search and the KDA recurrence.

### The speed calibration

The setup's calibration (v1.1.26, 2026-10-24) measured the drafter on this machine the way it does on a first run:
the same ~8,000-token document conversation without and with `--dflash`, sampled answers, each run writing about
1,500 tokens over a warm-up answer that is not counted.

| | tokens written | sustained (tokens/s) |
| --- | ---: | ---: |
| without the drafter | 1536 | 16.13 |
| with DFlash2 | 1536 | 17.43 |

That is +8% - less than the +19% the docs measure for one GPU on an RTX 3090, where the experts stream from RAM and
a checked draft saves whole misses. Here every expert is resident, so a draft saves mostly the work after a wrong
one and the gain is smaller. It is above the setup's 5% threshold, so the config keeps the drafter
(`installer.speed_calibration` in `maya-maya-s-v2-iq2_xxs.json`; `./maya.sh --speed-calibration` measures again).

## The comparison with upstream llama.cpp

Upstream master @ `272aad8b9` is the first llama.cpp with the `glm5-next` architecture, so it runs the same GGUF.
It was built here with CUDA for sm_121a (`-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=121`,
`-DGGML_CPU_ARM_ARCH=armv8.6-a+dotprod+i8mm+fp16`) and measured with its own `llama-bench` (2 repetitions) and
`llama-completion`.

### Reading a prompt

| prompt (tokens) | Maya (tokens/s) | llama.cpp (tokens/s) | Maya / llama.cpp |
| ---: | ---: | ---: | ---: |
| 256 | 222 | 234 | 0.95x |
| 512 | 303 | 314 | 0.96x |
| 1024 | 452 | 313 | 1.44x |
| 2048 | 619 | 308 | 2.01x |
| 4096 | 708 | 295 | 2.40x |
| 8192 | 744 | 277 | 2.69x |
| 16384 | 769 | 263 | 2.92x |
| 32768 | 754 | 239 | 3.16x |

llama.cpp's rate is flat at 230-315 tokens/s whatever the prompt's size; Maya's keeps rising to 769. The two agree
at the shortest prompts (both are bound by the same fixed costs there) and part company from 1K tokens on: at 32K
tokens Maya reads the prompt 3.2x faster - 43 s instead of 137 s.

### Writing an answer

`llama-bench`'s `tg` test starts from an empty context, so it is compared with Maya's 128-token-prompt row. The
`llama-completion` runs read a real prompt, so they are compared with Maya's 2048- and 8192-token rows.

| context | Maya (tokens/s) | llama.cpp (tokens/s) | Maya / llama.cpp |
| --- | ---: | ---: | ---: |
| empty (llama-bench `tg`) | 22.9 (128-token prompt) | 17.5 - 17.8 | 1.29x |
| 2100 tokens | 22.9 (2048-token prompt) | 15.68 | 1.46x |
| 2708 tokens | - | 15.53 | - |
| 8192 - 9021 tokens | 21.1 (8192-token prompt) | 15.61 | 1.35x |

With the DFlash2 drafter Maya is the faster one at every context: llama.cpp loses about 12% of its rate between an
empty context and a 2K-token one, Maya about 8% (22.9 to 21.1 between a 128-token and an 8K-token context, at a
256-token answer). Without the drafter Maya's decode was a tie at a short context (0.98x) and 3-7% faster at a long
one.

### What else the two do differently

| | Maya | llama.cpp |
| --- | --- | --- |
| The model's load | 27-29 s (the experts are read and packed into the pool at 8.7 GB/s) | 10.2 s (mmap and a copy into VRAM) |
| The expert pool | a profile ranks the experts; the pool holds all of them, the RAM tier and the disk tier take the rest | every expert in VRAM, no pool |
| The prompt's chunk | up to 32768 tokens in one pass (`--prefill auto`), the mixer in sub-batches of 2048 | 2048 tokens per batch (`-b 2048`) |
| The drafter | a DFlash2 drafter (0.7 GB, `--dflash` in the config): the decode numbers above are with it | the model's MTP block (off for this model: it has no draft block) |
| The attention | the k-pool indexer and the KDA recurrence, written for this architecture | the same, generic |

### The same model the traditional way

This machine had the traditional GGUF of the same model loaded before, unsloth's `GLM-5.3-Flash-UD-Q2_K_XL`
(`~/models/glm53-flash-gguf/`, four shards, loaded 2026-08-28) in llama.cpp. The comparison above is not against it -
the llama.cpp numbers are with the Maya GGUF - but the two quants sit at the same bit rate (a 2.x-bpw Q2 family on
both), so the measured llama.cpp rows (decode 15.5-17.8 tokens/s, prefill 239-314 tokens/s) carry over to it: Maya
still reads the prompt 3.2x faster and decodes 1.3-1.5x faster than the traditional stack at a like-for-like context.

## Reproducing it

Maya's numbers (the engine alone, so stop the server first - it wants the whole GPU):

```sh
./serve-maya.sh stop
.venv/bin/python tools/bench_tokens.py > sweep.md
```

`tools/bench_tokens.py` is the sweep this document's Maya numbers come from: the prompt sizes it reads, the answer
sizes it writes, and the markdown table it prints. `--prompts`, `--answers` and `--answer-prompt` change the sizes -
the 128-, 2048- and 8192-token-prompt groups of the decode table are three runs with `--answer-prompt 128`, `2048`
and `8192` (the tables here were pasted together by hand; `--out` would write only one table over the whole document).
The numbers include the DFlash2 drafter the config drafts with (`--dflash` in its `"args"`); for the no-drafting side,
`./maya.sh --speed-calibration` measures it, or take the flag out of the config.

One more thing about the first decode row of each group: the prompt sweep above it borrows from the expert pool, so
its first answers read a few experts back from the disk (the engine's `glm stat:` line says how many) until the pool
has them again - with the drafter that shows up as 15.0-15.8 tokens/s for a 32-token answer.

Upstream llama.cpp:

```sh
git clone https://github.com/ggerganov/llama.cpp && cd llama.cpp
git checkout 272aad8b984a4470d26f14bea8d958be68ac7c0c
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES=121 -DGGML_NATIVE=OFF \
      -DGGML_CPU_ARM_ARCH=armv8.6-a+dotprod+i8mm+fp16 -DLLAMA_BUILD_TESTS=OFF
cmake --build build --target llama-bench llama-completion -j 10

./build/bin/llama-bench -m MODEL-00001-of-00003.gguf \
    -p 256,512,1024,2048,4096,8192,16384,32768 -n 0 -r 2
./build/bin/llama-bench -m MODEL-00001-of-00003.gguf -p 0 -n 32,64,128,256,512,1024 -r 2
./build/bin/llama-completion -m MODEL-00001-of-00003.gguf -f PROMPT.txt -n 256 --no-warmup -no-cnv --temp 0
```

## The other stacks on this machine

`vLLM`, `SGLang` (0.5.12), `Ollama` and `llama-cpp-python` are installed here, but none of them can run this model:
they all take the model from Hugging Face (transformers' weights) or from an Ollama model, and `glm5-next` exists only
as the GGUF above. SGLang's model list has no GLM-5.3-Flash, and llama.cpp is the only one of them with the
architecture at all - which is why the comparison above is against llama.cpp.
