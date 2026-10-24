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
| Maya | v1.1.26 + the aarch64 port (this repository), `build/maya`, sm_121, context 131072 |
| llama.cpp | upstream master @ `272aad8b9` (the version with the `glm5-next` architecture), CUDA sm_121a |

Both run the same GGUF, on the same GPU, with greedy decoding. Upstream loads every layer into the GPU (89.83 GiB);
Maya fills an expert pool with all of them (80.17 GB of the experts plus the dense weights) and keeps a RAM tier and a
disk tier below it.

**The short version:** Maya reads a prompt **up to 3.2x faster** than upstream llama.cpp, and its decode is **a tie at
a short context and 3-7% faster at a long one**.

## Maya: reading a prompt

The engine alone (`build/maya --serve`), greedy, one token generated after the prompt. "First token" is the time from
the request to the first answer token - what a chat feels.

| prompt (tokens) | read (s) | read (tokens/s) | first token (ms) |
| ---: | ---: | ---: | ---: |
| 256 | 1.14 | 225 | 1136 |
| 512 | 1.70 | 302 | 1697 |
| 1024 | 2.25 | 456 | 2246 |
| 2048 | 3.28 | 625 | 3278 |
| 4096 | 5.82 | 704 | 5818 |
| 8192 | 10.98 | 746 | 10982 |
| 16384 | 21.41 | 765 | 21410 |
| 32768 | 43.35 | 756 | 43348 |

The rate rises with the prompt and then flattens: a short prompt is bound by the fixed costs of a pass (the routing,
the first layer's setup, the launch overhead), a long one by the memory bandwidth. The peak, about 765 tokens/s, is
reached around 16K tokens - the same place the decode starts to pay for the context it has to search.

## Maya: writing an answer

An answer of N tokens, greedy, after a prompt of the size in the first column.

| prompt | answer (tokens) | written | time (s) | decode (tokens/s) |
| ---: | ---: | ---: | ---: | ---: |
| 128 | 32 | 32 | 1.87 | 17.2 |
| 128 | 64 | 64 | 3.63 | 17.6 |
| 128 | 128 | 128 | 7.36 | 17.4 |
| 128 | 256 | 256 | 14.71 | 17.4 |
| 128 | 512 | 512 | 29.47 | 17.4 |
| 128 | 1024 | 1024 | 59.07 | 17.3 |
| 2048 | 32 | 32 | 2.03 | 15.7 |
| 2048 | 64 | 64 | 3.87 | 16.5 |
| 2048 | 128 | 128 | 7.73 | 16.6 |
| 2048 | 256 | 256 | 15.28 | 16.8 |
| 2048 | 512 | 512 | 30.40 | 16.8 |
| 2048 | 1024 | 1024 | 61.04 | 16.8 |
| 8192 | 128 | 128 | 7.94 | 16.1 |
| 8192 | 256 | 256 | 15.95 | 16.1 |

The decode rate depends on the context it has to carry, not on the length of the answer: 17.4 tokens/s with a
128-token context, 16.8 with 2048, 16.1 with 8192. Every expert is resident (a 99.97% cache hit), so what the
context costs is the attention over it - the k-pool indexer's search and the KDA recurrence.

## The comparison with upstream llama.cpp

Upstream master @ `272aad8b9` is the first llama.cpp with the `glm5-next` architecture, so it runs the same GGUF.
It was built here with CUDA for sm_121a (`-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=121`,
`-DGGML_CPU_ARM_ARCH=armv8.6-a+dotprod+i8mm+fp16`) and measured with its own `llama-bench` (2 repetitions) and
`llama-completion`.

### Reading a prompt

| prompt (tokens) | Maya (tokens/s) | llama.cpp (tokens/s) | Maya / llama.cpp |
| ---: | ---: | ---: | ---: |
| 256 | 225 | 234 | 0.96x |
| 512 | 302 | 314 | 0.96x |
| 1024 | 456 | 313 | 1.46x |
| 2048 | 625 | 308 | 2.03x |
| 4096 | 704 | 295 | 2.39x |
| 8192 | 746 | 277 | 2.69x |
| 16384 | 765 | 263 | 2.91x |
| 32768 | 756 | 239 | 3.16x |

llama.cpp's rate is flat at 230-315 tokens/s whatever the prompt's size; Maya's keeps rising to 765. The two agree
at the shortest prompts (both are bound by the same fixed costs there) and part company from 1K tokens on: at 32K
tokens Maya reads the prompt 3.2x faster - 43 s instead of 137 s.

### Writing an answer

`llama-bench`'s `tg` test starts from an empty context, so it is compared with Maya's 128-token-prompt row. The
`llama-completion` runs read a real prompt, so they are compared with Maya's 2048- and 8192-token rows.

| context | Maya (tokens/s) | llama.cpp (tokens/s) | Maya / llama.cpp |
| ---: | ---: | ---: | ---: |
| empty (llama-bench `tg`) | 17.4 (128-token prompt) | 17.5 - 17.8 | 0.98x |
| 2100 tokens | 16.8 (2048-token prompt) | 15.68 | 1.07x |
| 2708 tokens | - | 15.53 | - |
| 8192 - 9021 tokens | 16.1 (8192-token prompt) | 15.61 | 1.03x |

The decode is a tie at a short context, and Maya is the faster one as the context grows: llama.cpp loses about 12% of
its rate between an empty context and a 2K-token one, Maya about 4%.

### What else the two do differently

| | Maya | llama.cpp |
| --- | --- | --- |
| The model's load | 27-29 s (the experts are read and packed into the pool at 8.7 GB/s) | 10.2 s (mmap and a copy into VRAM) |
| The expert pool | a profile ranks the experts; the pool holds all of them, the RAM tier and the disk tier take the rest | every expert in VRAM, no pool |
| The prompt's chunk | up to 32768 tokens in one pass (`--prefill auto`), the mixer in sub-batches of 2048 | 2048 tokens per batch (`-b 2048`) |
| The drafter | the model's MTP block (off for this model: it has no draft block) | the model's MTP block (off for this model too) |
| The attention | the k-pool indexer and the KDA recurrence, written for this architecture | the same, generic |

## Reproducing it

Maya's numbers (the engine alone, so stop the server first - it wants the whole GPU):

```sh
./serve-maya.sh stop
.venv/bin/python tools/bench_tokens.py --out BENCHMARK.md
```

`tools/bench_tokens.py` is the sweep this document's Maya numbers come from: the prompt sizes it reads, the answer
sizes it writes, and the markdown table it prints. `--prompts`, `--answers` and `--answer-prompt` change the sizes.

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
