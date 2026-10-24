# Maya on an NVIDIA DGX Spark (GB10) and Grace

A single DGX Spark runs Maya-S (GLM-5.3-Flash) at its full context, with every expert in the GPU's own pool. The
catch is that a DGX Spark has no VRAM: the GB10 draws on the system's RAM, so the expert pool, the prompt buffers and
the desktop all share the same 120 GB. This page is what a bring-up needs to know about that, what the aarch64 port
had to change, and what is still missing.

Measured on one DGX Spark (see [Measured](#measured)): **16.8 tokens/s** decoding, **730 tokens/s** reading an
8K-token prompt, every expert resident, a 99.97% cache hit. [BENCHMARK.md](../BENCHMARK.md) has the numbers at every
token size, and the same model on the same GPU with upstream llama.cpp.

## The machine

| | |
| --- | --- |
| GPU | NVIDIA GB10, one device, compute capability 12.1 (sm_121) |
| Memory | 120 GB, shared with the system (no VRAM of its own) |
| CPU | 20 ARM cores (Cortex-X925, CPU part 0xd87) |
| Driver | 580 or newer (CUDA 13 needs 580) |
| Toolkit | CUDA 13.0 or newer, `nvcc` on the path |
| OS | Ubuntu 24.04, aarch64 |

Grace (NVIDIA's aarch64 CPU, as in a Grace Superchip or a GH200) has the same shape: an aarch64 host whose GPU draws
on memory the CPU shares with it. A discrete NVIDIA card on an aarch64 host (a card in a Grace server, for example)
takes the ordinary discrete path - the unified-memory rules below apply only where the GPU's memory is the system's.

## Bringing it up

The installer is the same one every platform uses; nothing about the setup is DGX-specific.

```sh
./maya.sh --check                 # the PC checks only
./maya.sh --setup --yes --gpu 0 --context 131072 --no-vision --download-model
```

`--check` prints what the engine will do with the hardware:

```
  NVIDIA GPUs:
    GPU 0 (NVIDIA GB10, 120 GB shared with the system) - can be used
  [ok] using GPU 0 (NVIDIA GB10, 120 GB shared with the system)
  the GPU's memory is the system's RAM: the engine fills it with the most-used experts and leaves the rest to the RAM
  tier and the SSD, so the free RAM at the start is what the pool can take
  [ok] CUDA toolkit 13.0: /usr/local/cuda/bin/nvcc
  [ok] NVIDIA driver 580.95.05
  [ok] RAM: 120 GB, 16 GB available now
  [ok] CPU: ARM (CPU part 0xd87) (ARM64, 20 threads; the expert kernels use ggml-cpu's NEON paths)
```

The setup downloads about 96 GB of GGUF shards (Maya-S-v2-IQ2_XXS) and writes the pack next to them, so the models
folder wants a fast NVMe SSD with ~100 GB free. `--no-vision` skips the image encoder (see
[Still missing](#still-missing)); without it the setup also fetches 1.1 GB of vision files and builds the encoder.

The context costs VRAM that the experts want, so 131072 leaves a pool of about 80 GB - enough for every expert of
Maya-S. A smaller context (32768, the default) leaves more room for the pool, but at 131072 every expert already
fits, so there is nothing to gain from it here: the context only costs state and KV room.

## Running it

The engine answers on an OpenAI-compatible API and serves a dashboard:

```sh
./serve-maya.sh            # start (waits until the API answers)
./serve-maya.sh status     # is it up, and where
./serve-maya.sh logs       # the server's and the engine's logs
./serve-maya.sh stop
```

- API: `http://127.0.0.1:8080/v1` (`/v1/chat/completions`, `/v1/responses`, `/v1/messages` for Anthropic's shape)
- Dashboard: `http://127.0.0.1:8080/`

`serve-maya.sh` finds a server it did not start (one a supervisor or a hand started), so `status` and `stop` work
whatever started it. The first start reads ~86 GB of experts: about 40 s to load them and warm the tiers, and the
first answers wait for that.

```sh
curl http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"glm-5.3-flash","messages":[{"role":"user","content":"Hello"}],"max_tokens":200}'
```

## What the aarch64 port needed

The engine's fast expert kernels are x86 (AVX-512 and AVX2). On aarch64 the port leaves them out and routes the
work through ggml-cpu's own NEON paths:

| | x86-64 | aarch64 |
| --- | --- | --- |
| The Q2_0 pack's expert kernel | AVX-512 VNNI/VBMI | not built: the native packs use ggml-cpu instead |
| The EXL3 expert kernels | AVX-512 / AVX2 / scalar | not built: the EXL3 quants do not run (the setup refuses them) |
| The native packs (IQ2, IQ3, Q4, ...) | this repository's kernels | ggml-cpu's NEON kernels |
| The spin wait | `_mm_pause` | `yield` |
| The store fence | `_mm_sfence` | `dmb oshst` |
| The CPU feature checks | AVX-512 present or not | always "no AVX-512", so no illegal instruction |

`src/kernels/cpu/portable.cpp` replaces the x86 kernels in the build. Its stubs do not silently compute something
else: they print what is missing and stop, because a wrong answer is worse than no answer. The canonical Q2_0 pack
therefore refuses to start on an ARM CPU with a clear message (`cpu_require_expert_support()`), and the IQ2_XXS pack
this setup uses is a native pack - it goes through ggml-cpu.

ggml-cpu is built with the CPU's own features, taken from `/proc/cpuinfo`:

```
-march=armv8.6-a+dotprod+i8mm+fp16
```

The GB10 has `asimddp` and `i8mm`, so the dot-product and the 8-bit integer matrix-multiply paths are on (its `bf16`
is not in the string: ggml's ARM backend does not use it). `setup.py` picks the string from the kernel's feature list,
so the same setup on an older ARM CPU picks a smaller one.

## The expert pool on a unified-memory device

A discrete card reports its own free VRAM, and the pool takes it. A unified-memory device cannot: the runtime's free
figure counts the model's file-backed page cache as used (on the test machine it read about 2 GB free with the 90 GB
pack cached), so the pool would come out far too small - the start fails with

```
maya generate: glm fast: CUDA0 has no room for the expert pool: 5 slots a layer, at least 8 needed (about 604 MiB
more). To make room: a smaller --max-context, --kv int8, or close other programs that use the GPU
```

The pool is therefore sized from `/proc/meminfo`'s `MemAvailable`, which leaves the reclaimable cache out - the kernel
evicts it as the experts are read back in:

```
glm fast: CUDA0 unified memory: MemAvailable 99.17 GB, headroom 16.00 GB + 1024 MB reserve, experts need 80.17 GB,
           pool budget 80.17 GB
glm fast: CUDA0 layers [0,45) expert pool 80.17 GB, 288-288 slots/layer (12096 total)
glm fast: CUDA0 all experts fit in the GPU pool; RAM tier is staging only
```

- `headroom 16.00 GB` is `MAYA_GLM_RAM_HEADROOM_GB` - the RAM left free for the system (the desktop, the file
  cache, everything else that is not Maya). Raise it on a busy machine; lower it when the pool is short.
- `1024 MB reserve` is the VRAM kept free beside the pool for the driver, the sampler and what is allocated later (a
  unified device uses 1024 MiB, a discrete card 700).
- Nothing is checked after the pool is written: the free figure moves with the page cache, so a second check would
  give the memory back and then take it away. The start fails only if the pool cannot hold one route's experts in
  every layer, and it then says how many MiB more it needs.

Because the pool is RAM, **other programs on the machine take from it**: the pool is sized from what is free at the
start, so a desktop with 40 GB of its own applications running leaves the engine that much less. The RAM tier (the
pinned tier below the pool) is staging only when every expert fits, as it does here.

The CPU lane - the threads that compute the experts that do not fit in the pool - measures itself against the copy
over the link at the start and takes fewer experts when the link is faster:

```
glm fast: CUDA0 CPU lane: 13 threads, an expert 0.171 ms on the CPU vs 0.117 ms over PCIe -> of 0..8 RAM-tier experts
          the CPU takes 0 0 1 1 1 2 2 3 3
```

On this machine the link is the faster way, so the CPU lane takes little, and when every expert fits in the pool it
takes none at all (`glm fast: CUDA0 CPU lane off: the RAM tier holds no expert`).

## Measured

`./maya.sh --bench` with Maya stopped (the standard measurement: three greedy answers of 256 tokens with the
thinking off, and a prompt read at 2048 and 8192 tokens):

```
Project Maya v1.1.26 benchmark, 2026-10-24 11:36
GPUs: NVIDIA GB10 120 GB; RAM 120 GB; CPU ARM (CPU part 0xd87)
setup: maya-maya-s-v2-iq2_xxs.json, context 131072, GPUs [0]
decode (writing the answer): mean 16.8 tokens/s over 3 answers of 256 tokens (greedy, thinking off)
prefill (reading the prompt): 597 tokens/s, 2048 tokens
prefill (reading the prompt): 730 tokens/s, 8192 tokens
```

The engine's own lines for the same run:

```
glm stat: decode 59.34 ms/tok (16.85 tok/s) | vram hit 99.97% | ram fetches 0.00/tok | disk 0.09 reads/tok
```

Every expert sits in the pool (12 096 of 12 096), so a token needs no disk and no RAM: the experts are read into the
pool at 8.7 GB/s at the start (86 GB in 10 s), and the decode is bound by the kernels.

These numbers match the DGX Spark row in the README's table (16.5 tokens/s, ~730 tokens/s on a 6K prompt, reported
by @ericoh88 on v1.1.15).

## Still missing

- **The canonical Q2_0 pack** is x86-only. Maya-S-v2-IQ2_XXS (a native pack) works; a Q2_0 pack does not, and the
  engine says so at startup instead of failing later.
- **The EXL3 CPU lane** is x86-only, so the DeepSeek EXL3 quants have no CPU lane here: their expert kernels are
  AVX2/AVX-512. The setup refuses them on an aarch64 host and points at the GGUF quant instead:

  ```
  [X]  DeepSeek-V4.1-Flash-1017's EXL3 quant needs the x86 expert kernels (AVX2/AVX-512), which this ARM CPU does not
       have
       set up its GGUF instead: ./maya.sh --setup --model DeepSeek-V4.1-Flash-1017 --ds4-quant gguf-q8
  ```
- **The image encoder** builds for aarch64 (`tools/vision`, CUDA sm_121), but its files (1.1 GB) are not part of the
  setup used here - `--no-vision` skips them. The engine reads text only in this configuration.
- **No allocation-failure recovery.** The prefill sizing estimates the memory, but an allocation can still fail at
  runtime; the recovery (a smaller chunk, or a request-level error instead of a dead process) is not implemented yet.
  See [docs/SETTINGS.md](SETTINGS.md#the-expert-pools-size) for the settings that move the pool's bounds.
- **`ple_parity`** (a C++ test) needs the maintainer's fixtures - the Qwen3.8-Flash-Next GGUF and
  `bench/micro/ple_*.bin` - and fails without them on any machine, not only here.

## Troubleshooting

**"has no room for the expert pool"** - the pool came out too small for one route's experts in every layer. On a
unified device that means the RAM was not free: close what holds it, or lower `MAYA_GLM_RAM_HEADROOM_GB` (16 GB by
default here) or `--max-context`.

**The pool is smaller than expected** - it is sized from the free RAM at the start, so anything else on the machine
is subtracted. The start log says what it took (`pool budget ... GB`); compare it with `MemAvailable` in
`/proc/meminfo`.

**The decode is slower than the table says** - check the engine's `glm stat:` line. `vram hit` below 99% means the
experts are not all resident (the pool was too small), and `cpu lane` above zero means the CPU is computing experts
it should not.

**The build fails in ggml** - check the `-march=` line in the configure output. It comes from `/proc/cpuinfo`'s
feature list; a CPU without `i8mm` gets a smaller one.
