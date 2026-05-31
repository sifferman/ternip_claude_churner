# Ternary weight compression: BitNet.cpp vs. llama.cpp

How the two in-tree inference stacks pack 1.58-bit ternary weights ({-1, 0, +1}).

- **BitNet.cpp** — [microsoft/BitNet @ `01eb415`](https://github.com/microsoft/BitNet/tree/01eb415772c342d9f20dc42772f1583ae1e5b102) (`extra/BitNet/`)
- **llama.cpp** — [ggml-org/llama.cpp @ `2084434`](https://github.com/ggml-org/llama.cpp/tree/2084434e666c5b08cd5e2a2f256e583a0f85a44c) (`extra/llama.cpp/`)

The headline: **only upstream llama.cpp uses the optimal "5 weights per byte" packing (`TQ1_0`).** BitNet.cpp uses 2-bit (`I2_S`) or LUT-oriented (`TL1`/`TL2`) layouts that trade density for kernel speed.

---

## Quick comparison

| Type | Stack | Packing | bits/weight | Optimized for |
|---|---|---|---:|---|
| **TQ1_0** | llama.cpp | 5 trits/byte (3⁵=243≤256) + 4 trits/byte tail + scale | **1.6875** | max density |
| **TQ2_0** | llama.cpp | 2 bits/element + scale | 2.0625 | simple/fast dequant |
| **I2_S** | BitNet.cpp | 2 bits/element, 4/byte + per-row scale | ~2.0 | native ggml GEMM/GEMV |
| **TL1** | BitNet.cpp | 2-bit index, LUT-based | ~2.0 | ARM LUT kernels |
| **TL2** | BitNet.cpp | 3 trits → 4-bit magnitude (6/byte) + sign bit (24/byte) | ~1.67 | x86 LUT kernels |

---

## llama.cpp types

These are first-class ggml quantization types, defined and documented in `ggml-common.h`.

### `TQ1_0` — the true 5-trits-per-byte format
- **Defined / described:** [`ggml/src/ggml-common.h#L266-L271`](https://github.com/ggml-org/llama.cpp/blob/2084434e666c5b08cd5e2a2f256e583a0f85a44c/ggml/src/ggml-common.h#L266-L271). The comment says it directly:
  ```c
  // 1.6875 bpw
  typedef struct {
      uint8_t qs[(QK_K - 4*QK_K/64) / 5]; // 5 elements per byte (3^5 = 243 < 256)
      uint8_t qh[QK_K/64];                // 4 elements per byte
      ggml_half d;                        // scale
  } block_tq1_0;
  ```
  Most weights are packed 5-per-byte in base-3 (`qs`); a small tail is packed 4-per-byte (`qh`); one fp16 scale `d` per `QK_K`-element block.
- **Enum id:** `GGML_TYPE_TQ1_0 = 34` in [`ggml/include/ggml.h#L424`](https://github.com/ggml-org/llama.cpp/blob/2084434e666c5b08cd5e2a2f256e583a0f85a44c/ggml/include/ggml.h#L424).
- **Pack / unpack:** `quantize_row_tq1_0_ref` and `dequantize_row_tq1_0` in [`ggml/src/ggml-quants.c#L2244`](https://github.com/ggml-org/llama.cpp/blob/2084434e666c5b08cd5e2a2f256e583a0f85a44c/ggml/src/ggml-quants.c#L2244).
- **Used by:** the CPU dot-product kernels (`ggml_vec_dot_tq1_0_q8_K`) under `ggml/src/ggml-cpu/` (generic + per-arch `arch/{arm,x86,riscv}/quants.c`), wired into `mul_mat` through ggml's type traits table.

### `TQ2_0` — 2-bit ternary
- **Defined:** [`ggml/src/ggml-common.h#L274-L278`](https://github.com/ggml-org/llama.cpp/blob/2084434e666c5b08cd5e2a2f256e583a0f85a44c/ggml/src/ggml-common.h#L274-L278) — `// 2.0625 bpw`, `uint8_t qs[QK_K/4]; // 2 bits per element` plus the fp16 scale.
- **Enum id:** `GGML_TYPE_TQ2_0 = 35`, [`ggml/include/ggml.h#L425`](https://github.com/ggml-org/llama.cpp/blob/2084434e666c5b08cd5e2a2f256e583a0f85a44c/ggml/include/ggml.h#L425).
- **Used by:** same path as `TQ1_0` — `quantize_row_tq2_0` / `dequantize_row_tq2_0` in `ggml-quants.c`, dot-products in `ggml-cpu/`. Looser than `TQ1_0` but cheaper to unpack.

---

## BitNet.cpp types

BitNet.cpp is a llama.cpp fork (`extra/BitNet/3rdparty/llama.cpp`) plus a custom kernel layer in `extra/BitNet/src/`. It adds its own ggml types (`GGML_TYPE_I2_S`, `GGML_TYPE_TL1`, `GGML_TYPE_TL2`) and routes only ternary weight matmuls through them.

**Dispatch gate** (what decides "use a BitNet kernel for this matmul"): [`ggml_bitnet_can_mul_mat()`](https://github.com/microsoft/BitNet/blob/01eb415772c342d9f20dc42772f1583ae1e5b102/src/ggml-bitnet-lut.cpp#L58) and [`ggml_bitnet_get_type_bits()`](https://github.com/microsoft/BitNet/blob/01eb415772c342d9f20dc42772f1583ae1e5b102/src/ggml-bitnet-lut.cpp#L85) in `src/ggml-bitnet-lut.cpp` (both return 2 bits for `TL1`/`TL2`).

### `I2_S` — 2 bits/weight, 4 per byte
- **Pack code (the description):** [`quantize_i2_s()` in `src/ggml-bitnet-mad.cpp#L51`](https://github.com/microsoft/BitNet/blob/01eb415772c342d9f20dc42772f1583ae1e5b102/src/ggml-bitnet-mad.cpp#L51). Each weight → 2-bit code `{0,1,2}`, four codes OR'd into one byte (`<< (6 - 2*group_idx)`), with a per-row float scale appended.
- **Described in prose:** [`src/README.md`](https://github.com/microsoft/BitNet/blob/01eb415772c342d9f20dc42772f1583ae1e5b102/src/README.md) — "Native I2_S GEMM & GEMV Support … integrated into ggml."
- **Used by:** the MAD (multiply-add) kernels in `src/ggml-bitnet-mad.cpp`; this is the default format for the BitNet-b1.58-2B-4T model and integrates with stock llama.cpp `mul_mat`.

### `TL1` — table-lookup, ARM
- **Defined / dispatched:** `src/ggml-bitnet-lut.cpp` (the `#if defined(GGML_BITNET_ARM_TL1)` block, [`get_type_bits` L85](https://github.com/microsoft/BitNet/blob/01eb415772c342d9f20dc42772f1583ae1e5b102/src/ggml-bitnet-lut.cpp#L85)).
- **Pack code:** [`process_tl1()` in `utils/convert-hf-to-gguf-bitnet.py#L465`](https://github.com/microsoft/BitNet/blob/01eb415772c342d9f20dc42772f1583ae1e5b102/utils/convert-hf-to-gguf-bitnet.py#L465).
- **Used by:** generated ARM kernels in [`preset_kernels/*/bitnet-lut-kernels-tl1.h`](https://github.com/microsoft/BitNet/tree/01eb415772c342d9f20dc42772f1583ae1e5b102/preset_kernels) (emitted by `utils/codegen_tl1.py`). Weights are reshaped into tiles whose layout feeds a per-tile lookup table rather than a base-3 unpack.

### `TL2` — table-lookup, x86 (closest BitNet gets to dense)
- **Defined / dispatched:** the `#if defined(GGML_BITNET_X86_TL2)` block of `src/ggml-bitnet-lut.cpp` ([`get_type_bits` L157+](https://github.com/microsoft/BitNet/blob/01eb415772c342d9f20dc42772f1583ae1e5b102/src/ggml-bitnet-lut.cpp#L157)).
- **Pack code (the description):** [`preprocess_three_weights_tl2()` in `utils/convert-hf-to-gguf-bitnet.py#L549`](https://github.com/microsoft/BitNet/blob/01eb415772c342d9f20dc42772f1583ae1e5b102/utils/convert-hf-to-gguf-bitnet.py#L549) (plus the `_two_` variant at [L523](https://github.com/microsoft/BitNet/blob/01eb415772c342d9f20dc42772f1583ae1e5b102/utils/convert-hf-to-gguf-bitnet.py#L523)). It groups **3 trits** as `9·w₀ + 3·w₁ + w₂` (27 values), then splits that into a **magnitude** (`abs` ≤ 13 → 4 bits, two packed per byte = 6 weights/byte) and a **sign bit** stored separately (24 weights/byte). Net ≈ 1.67 bits/weight — reached by sign-factoring, not base-3 packing.
- **Used by:** generated x86 LUT kernels in [`preset_kernels/*/bitnet-lut-kernels-tl2.h`](https://github.com/microsoft/BitNet/tree/01eb415772c342d9f20dc42772f1583ae1e5b102/preset_kernels) (emitted by `utils/codegen_tl2.py`; design notes in [`docs/codegen.md`](https://github.com/microsoft/BitNet/blob/01eb415772c342d9f20dc42772f1583ae1e5b102/docs/codegen.md)). The runtime kernel splits K into a `three_k` part (TL2) and a `two_k` remainder (TL1-style).

---

## Why the difference

- **llama.cpp `TQ1_0`** optimizes **storage**: base-3 packing hits the information-theoretic floor for ternary (1.6 bits; 1.6875 with the block scale).
- **BitNet.cpp** optimizes **compute**: its kernels are SIMD lookup-table / multiply-add GEMMs, so the on-disk layout is chosen to feed those kernels with minimal unpacking. `I2_S`/`TL1` accept ~2 bits/weight; `TL2` claws density back to ~1.67 via the magnitude+sign split. Decode throughput beats maximum compression.
