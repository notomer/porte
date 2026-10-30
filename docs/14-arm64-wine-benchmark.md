# 14 — Native ARM64 Wine/FEX benchmark report (reproducible)

This is the measurement behind `docs/13` §3–§5 and the numbers `docs/12` decides
on. It is written so that someone with the same hardware can reproduce it end to
end, including the parts that make the result look less impressive.

Nothing here is a claim about games, GPUs, or "native parity". One CPU-bound
workload, two execution architectures, one native reference, five interleaved
repeats each, and output hashes.

---

## 1. Environment

| | |
| --- | --- |
| machine | Apple M2 Max, 12 cores, 32 GB |
| OS | macOS 27.2 (build 26B123) |
| Rosetta runtime | Wine Staging 11.20, x86-64 (`Wine Staging.app/Contents/Resources/wine/bin/wine`) |
| native ARM64 runtime | MetalSharp Wine 4.0 — Wine 11.20 + FEX-2610, `metalsharp-wine-4.0-arm64.tar.xz`, SHA-256 `88ab68b5…`, verified against the release's `SHA256SUMS` |
| native reference | 7-Zip 26.05 for macOS arm64 (`7zz`, Homebrew bottle) |
| Windows program | 7-Zip 26.05 x64, installed from the official MSI into a Porte bottle |
| date | 2026-10-29 |

The native ARM64 runtime is ad-hoc signed, so it runs x86-64 and ARM64 guests
with 16K pages. Its i386 support needs the signed app and was not used.

---

## 2. Reproduce it

```bash
# 1. the corpus — deterministic, 30 MiB, prints its own SHA-256
python3 fixtures/workload/make_input.py /tmp/porte-workload.bin --mb 30
# -> sha256 3b9fb80531626dbf894d45cff97e8472edae5e92e346e1d0f55c0a136f453deb

# 2. the native ARM64 runtime (955 MB), then a prefix and the program's files
curl -LO https://github.com/metalsharp/MetalSharp-Wine/releases/download/v4.0/metalsharp-wine-4.0-arm64.tar.xz
tar -xJf metalsharp-wine-4.0-arm64.tar.xz -C "$HOME/Library/Application Support/Porte Research"
WINE="$HOME/Library/Application Support/Porte Research/metalsharp-wine-4.0-arm64/bin/wine"
WINEPREFIX="$HOME/Library/Application Support/Porte Research/metalsharp-prefix-7zip" "$WINE" wineboot -i
# copy 7z.exe and 7z.dll from an existing 7-Zip installation into the prefix's
# C:\Program Files\7-Zip (a full MSI install under this runtime also works)

# 3. point the job at everything
export PORTE_WORKLOAD_INPUT=/tmp/porte-workload.bin
export PORTE_ROSETTA_WINE="$HOME/Library/Application Support/Porte/Runtimes/wine-staging-11.20/Wine Staging.app/Contents/Resources/wine/bin/wine"
export PORTE_ROSETTA_PREFIX="$HOME/Library/Application Support/Porte/Bottles/7-zip-26-05-x64-edition-96360287"
export PORTE_ARM64_WINE="$HOME/Library/Application Support/Porte Research/metalsharp-wine-4.0-arm64/bin/wine"
export PORTE_ARM64_PREFIX="$HOME/Library/Application Support/Porte Research/metalsharp-prefix-7zip"
export PORTE_NATIVE_7ZZ=/opt/homebrew/bin/7zz

# 4. baselines: Rosetta Wine, native ARM64 Wine, the specialization, native arm64
.venv/bin/python -m porte.cli runtime workload \
  --job fixtures/workload/7zip-lzma.json --out results/arm64_wine_bench.json

# 5. the specialization, measured and proven on its own
.venv/bin/python -m porte.cli runtime specialize \
  --job fixtures/workload/7zip-lzma.json --baseline arm64-wine-fex --repeats 5 \
  --out results/arm64_specialization.json

# 6. guest attribution (needs FEX's JIT naming, which the tool sets)
.venv/bin/python -m porte.cli runtime fex-profile \
  --program 'C:\Program Files\7-Zip\7z.exe' \
  --wine "$PORTE_ARM64_WINE" --prefix "$PORTE_ARM64_PREFIX" \
  --arg=a --arg=-t7z --arg=-mx=9 --arg=-mmt=1 --arg=-y \
  --arg='C:\prof-final-1.7z' --arg='C:\porte-workload.bin' \
  --seconds 3 --out results/arm64_wine_profile.json
```

Machines differ: the numbers below are this host's. What transfers is the
method, the hashes, and the ratios.

---

## 3. Workload

`7z a -t7z -mx=9 -mmt=off -y out.7z input.bin`, single-threaded LZMA, on a 30 MiB
corpus that is 45% text-like bytes, 35% fixed-width records and 20%
incompressible — the mix is deliberate, because LZMA's match finder behaves very
differently on each. The corpus is generated from a seed and its SHA-256 is
checked by the harness before the first run, so a wrong or stale input fails
instead of silently changing the result.

Every backend runs in a fresh temporary directory with the input copied in as
`input.bin`, so the archive records one name and one size everywhere.

---

## 4. Method, and three decisions that matter

1. **Interleaved repeats.** The harness runs repeat 1 across every backend, then
   repeat 2, and so on. A machine that heats up or slows down mid-measurement
   affects all backends instead of the last one. (It was not interleaved in the
   first version of this benchmark, and that version overstated the
   specialization by turning drift into a win.)
2. **Kernel-measured CPU and memory.** Wine starts the guest as a *child* process
   and reparents it, so the launcher's own `rusage` reports ~0.06 s for a 17 s
   job. The harness samples every process in the run's family (`ps` every 100 ms:
   CPU time, RSS, thread count) and reports the sum of peaks. `GETRUSAGE`-based
   numbers from the launcher are simply wrong on this stack and are not used.
3. **Correctness as bytes and as content.** The archive's SHA-256 is compared
   across backends; the native macOS build legitimately produces a different
   header (POSIX attributes instead of Windows ones), so every output is also
   verified with `7zz t`. "Identical" means identical within a family, "valid"
   means valid everywhere.

Repeats: 5. Reported: median, min, max, stdev, CPU median, peak RSS median,
threads.

---

## 5. Results

### 5.1 Baselines (`results/arm64_wine_bench.json`)

| backend | wall median | min | max | stdev | CPU | RSS | threads |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Wine 11.20 / Rosetta | 20,484 ms | 19,465 ms | 21,139 ms | 677 ms | 15,450 ms | 351 MB | 5 |
| MetalSharp 4.0 / FEX | 21,190 ms | 20,370 ms | 23,071 ms | 1,160 ms | 16,670 ms | 420 MB | 4 |
| MetalSharp 4.0 / FEX, no-TSO | 19,430 ms | 18,287 ms | 19,962 ms | 651 ms | 14,900 ms | 417 MB | 5 |
| native arm64 7-Zip 26.05 | 13,505 ms | 12,757 ms | 13,851 ms | 430 ms | 13,030 ms | 336 MB | 1 |

Ratios: FEX **0.97×** Rosetta; FEX+specialization **1.09×** FEX, **1.05×**
Rosetta; native **1.57×** FEX, **1.52×** Rosetta.

Reading it honestly:

- The native ARM64 runtime costs **20% more memory** than Rosetta here
  (translation caches and JIT regions) and is **not faster**.
- The memory-model specialization is the only line that beats Rosetta, and it
  beats it by 5%.
- Native ARM64 is 1.57× faster than the same program under FEX. That is the
  headroom any nativeization layer is aiming at, and it is a long way from
  "near-native".

### 5.2 The specialization, proven separately (`results/arm64_specialization_record.json`)

| | baseline | specialized | change |
| --- | ---: | ---: | ---: |
| wall median (5 repeats) | 16,279 ms | 14,951 ms | **1.089×** |
| wall min | 15,708 ms | 14,553 ms | 1.079× |
| CPU median | 15,960 ms | 14,650 ms | **1.089×** |
| peak RSS | 418.3 MB | 417.8 MB | — |
| exit codes | 0 | 0 | — |
| archive sha256 | `e85b8f18…` | `e85b8f18…` | identical |
| integrity check | pass | pass | — |

Four evaluation runs of the same job produced 1.034×, 1.084×, 1.089× and
1.114× wall clock. The CPU ratio stayed in 1.06–1.11× across all of them, which
is the number to trust: removing emulated barriers removes instructions.

### 5.3 The guard (`results/arm64_specialization_guard.json`)

| 7-Zip mode | CPU | wall | parallelism | decision |
| --- | ---: | ---: | ---: | --- |
| `-mmt=1` | 15.5 s | 17.3 s | 0.90 | candidate proposed |
| `-mmt=4` | 13.4 s | 9.7 s | 1.39 | **refused** |

Same binary, same input, opposite verdicts. Without this the specialization would
be a switch that happens to work on one workload and corrupts another.

### 5.4 Guest attribution (`results/arm64_wine_profile.json`)

| class | samples | share |
| --- | ---: | ---: |
| waiting in the kernel (4 guest threads, one working) | 7,761 | 77.5% |
| executing in FEX's JIT, address in no named block | 2,254 | 22.5% |
| executing in a named guest block | 0 | 0% |
| host | 0 | 0% |

FEX wrote 6,277 block entries with guest origins (`7z.dll` 3,631, `7z.exe`
3,399). None of the 57 distinct sampled top-of-stack addresses fell inside one.
Measured, then reported as measured.

One caveat on this table, because the number is easy to get wrong: FEX's map has
overlapping ranges (region markers spanning 1 MB, several entries per translated
block), so the lookup must scan and prefer entries with a guest origin. An
earlier version bisected and reported 0 mapped samples for the wrong reason —
"not found" rather than "not named". Both versions reach the same conclusion
here, and only the current one can be trusted if that changes.

---

## 6. What this benchmark does not show

- **No GPU, no frames, no games.** Frame-time percentiles from
  `results/frame_baseline.json` are from an earlier phase and unrelated.
- **No second program.** A one-program result is an anecdote with error bars.
- **No startup measurement across the two runtimes under identical conditions.**
  MetalSharp publishes cold/warm startup on its own hardware; this report does
  not claim a comparison.
- **No claim that the specialization is safe for arbitrary inputs.** It is safe
  for the inputs it was verified on, under a guard that is re-checkable, and the
  record says so.
- **LZMA is not representative of interactive software.** It is CPU-bound, it
  allocates predictably, and it does not touch the GPU, the window server, or
  the registry.

---

## 7. How this compares with MetalSharp's own numbers

`docs/PERFORMANCE.md` of MetalSharp Wine 4.0 (Apple M4, 2026-10-22) reports
x86-64 through FEX at 0.94–1.01× native ARM64 on its C microbenchmarks, 1.67×
native for CPython overall, and warm startup of 31 ms against 25 ms native. Those
numbers say translation overhead is modest for code FEX compiles well, and this
report's 0.97× against Rosetta is consistent with that: the runtime is not the
bottleneck for LZMA — the algorithm is, and it is already x86-64-optimal.

The two reports do not conflict; they answer different questions. MetalSharp
measures how good its translator is. This one measures how much is left for
anything else to remove, and finds ~9% of wall clock in this workload, plus a
1.57× distance to native that no runtime-level tuning can close.
