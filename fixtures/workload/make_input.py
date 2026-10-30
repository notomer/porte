#!/usr/bin/env python3
"""Build the deterministic input used by the cross-runtime benchmark.

The benchmark needs a file whose *content is identical everywhere*, so that a
timing difference between two runtimes cannot come from the input. This script
writes one from a seed: no network, no system files, same bytes on any machine
and any Python 3.

    python3 fixtures/workload/make_input.py /tmp/porte-workload.bin --mb 30

The mix is deliberate, because LZMA's speed depends on what it is compressing:

* 45% text-like bytes      — long matches, typical of documents and logs
* 35% structured binary    — fixed-width records, typical of application data
* 20% incompressible bytes — the worst case for the match finder

The output is deterministic for a given `--mb` and `--seed`; the script prints
the SHA-256 so a report can prove which input it used.
"""
from __future__ import annotations

import argparse
import hashlib
import random
from pathlib import Path

WORDS = (
    "the of and to in is that for it as was with be by on not he this are or "
    "his from at which but have an they you were her all she there would their "
    "we him been has when who will more no if out so said what up its about "
    "into than them can only other new some could time these two may then do "
    "first any my now such like our over man me even most made after also did "
).split()


def text_block(rng: random.Random, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        line = " ".join(rng.choice(WORDS) for _ in range(rng.randint(4, 14)))
        out += line.encode("ascii") + b"\n"
    return bytes(out[:size])


def record_block(rng: random.Random, size: int) -> bytes:
    """Fixed-width records: repeated keys with varying values."""
    out = bytearray()
    index = 0
    while len(out) < size:
        out += (b"\x01R\x00" + index.to_bytes(4, "little")
                + rng.getrandbits(24).to_bytes(3, "little")
                + b"status=active;flags=%02x;" % (index & 0xFF)
                + (b"payload=" + bytes(rng.getrandbits(8) for _ in range(9))))
        index += 1
    return bytes(out[:size])


def incompressible_block(rng: random.Random, size: int) -> bytes:
    return rng.randbytes(size)


def build(megabytes: int, seed: int) -> bytes:
    rng = random.Random(seed)
    total = megabytes * 1024 * 1024
    text = int(total * 0.45)
    records = int(total * 0.35)
    return (text_block(rng, text) + record_block(rng, records)
            + incompressible_block(rng, total - text - records))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="where to write the corpus")
    parser.add_argument("--mb", type=int, default=30, help="size in MiB")
    parser.add_argument("--seed", type=int, default=20261029)
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing file")
    args = parser.parse_args()

    target = Path(args.path)
    if target.exists() and not args.force:
        data = target.read_bytes()
        print(f"{target} already exists: {len(data)} bytes, "
              f"sha256 {hashlib.sha256(data).hexdigest()}")
        return 0
    data = build(args.mb, args.seed)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print(f"{target}: {len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
