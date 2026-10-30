"""Tests for the FEX guest-attribution parsers.

The profiler joins two text formats produced by other programs, so the tests use
their real shape: the perf map FEX writes (with and without a guest origin) and
`/usr/bin/sample`'s collapsed top-of-stack section, including the `???` lines
that carry only a raw address.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from porte.backends import fex_profile  # noqa: E402

PERF_MAP = """\
0x7ff4dfed0000 2860 Dispatch_248
0x7ff4dfed0000 2860 FEXJIT
0x7ff4ade70000 1000000 FEXJIT
0x7ff4ade70004 690 \\\\??\\c:\\program files\\7-zip\\7z.exe+0x61380 (0x7ff4ade70004)
0x7ff4ade70004 690 \\\\??\\c:\\program files\\7-zip\\7z.exe
0x7ff4ade70200 120 \\\\??\\c:\\windows\\system32\\kernelbase.dll+0x1116a0 (0x7ff4ade70200)
"""

SAMPLE = """\
Analysis of sampling 7z.exe (pid 4242) every 1 millisecond
Code Type:       ARM64
Call graph:
    100 Thread_1
Call graph:
Sort by top of stack, same collapsed (when >= 5):
        __workq_kernreturn  (in libsystem_kernel.dylib)        40
        ???  (in <unknown binary>)  [0x7ff4ade70004]        30
        __platform_memmove  (in libsystem_platform.dylib)        20
        _dispatch_worker  (in libdispatch.dylib)  [0x1a0000000]        10
"""


class PerfMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porte-test-fex-"))
        self.map = self.tmp / "perf-4242.map"
        self.map.write_text(PERF_MAP, encoding="utf-8")

    def test_blocks_are_parsed_with_guest_origins(self):
        blocks = fex_profile.parse_perf_map(self.map)
        named = [b for b in blocks if b.rva is not None]
        self.assertEqual(len(named), 2)
        first = named[0]
        self.assertEqual(first.start, 0x7ff4ade70004)
        self.assertEqual(first.size, 690)
        self.assertEqual(first.rva, 0x61380)
        self.assertIn("7z.exe", first.module)
        self.assertEqual(blocks, sorted(blocks, key=lambda b: b.start))

    def test_lookup_inside_and_outside_a_block(self):
        blocks = fex_profile.parse_perf_map(self.map)
        self.assertEqual(fex_profile._block_at(blocks, 0x7ff4ade70004).rva,
                         0x61380)
        self.assertEqual(fex_profile._block_at(blocks, 0x7ff4ade70050).rva,
                         0x61380)
        # Outside the named block but inside the 1 MB region marker.
        outside = fex_profile._block_at(blocks, 0x7ff4ade7ffff)
        self.assertIsNotNone(outside)
        self.assertIsNone(outside.rva)
        self.assertIsNone(fex_profile._block_at(blocks, 0x1))

    def test_overlapping_regions_do_not_hide_the_named_block(self):
        # The region marker starts four bytes before the block it contains: a
        # bisect picks whichever it lands on, which is why this is a scan.
        blocks = fex_profile.parse_perf_map(self.map)
        for address in (0x7ff4ade70004, 0x7ff4ade70005, 0x7ff4ade70286):
            found = fex_profile._block_at(blocks, address)
            self.assertIsNotNone(found.rva, hex(address))


class SampleParseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porte-test-fex-sample-"))
        self.sample = self.tmp / "sample.txt"
        self.sample.write_text(SAMPLE, encoding="utf-8")

    def test_top_of_stack_frames_carry_counts_and_addresses(self):
        frames = fex_profile.parse_sample(self.sample)
        by_name = {frame.name: frame for frame in frames}
        self.assertEqual(by_name["__workq_kernreturn"].count, 40)
        self.assertEqual(by_name["__workq_kernreturn"].image,
                         "libsystem_kernel.dylib")
        self.assertEqual(by_name["???"].address, 0x7ff4ade70004)
        self.assertEqual(by_name["_dispatch_worker"].count, 10)


class AttributionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porte-test-fex-attr-"))
        (self.tmp / "m.map").write_text(PERF_MAP, encoding="utf-8")
        (self.tmp / "s.txt").write_text(SAMPLE, encoding="utf-8")

    def test_classes_are_separated_without_guessing(self):
        blocks = fex_profile.parse_perf_map(self.tmp / "m.map")
        frames = fex_profile.parse_sample(self.tmp / "s.txt")
        report = fex_profile.Report(program="7z.exe", pid=4242,
                                    code_type="ARM64", seconds=1.0)
        fex_profile.attribute(report, frames, blocks)
        self.assertEqual(report.total_samples, 100)
        self.assertEqual(report.classes["guest"], 30)       # the mapped address
        self.assertEqual(report.classes["waiting"], 40)     # the kernel wait
        # libdispatch and libsystem_platform: host code, not unattributed.
        self.assertEqual(report.classes["host"], 30)
        self.assertNotIn("unattributed", report.classes)
        self.assertEqual(report.mapped_addresses, 1)
        self.assertEqual(report.sampled_addresses, 2)
        self.assertEqual(report.functions[0]["rva"], "0x61380")

    def test_no_mapping_is_reported_as_a_limit(self):
        # A perf map with only the region markers: every address is unmapped,
        # which must be said out loud rather than reported as guest time.
        path = self.tmp / "empty.map"
        path.write_text("0x7ff4ade70000 1000000 FEXJIT\n", encoding="utf-8")
        blocks = fex_profile.parse_perf_map(path)
        frames = fex_profile.parse_sample(self.tmp / "s.txt")
        report = fex_profile.Report(program="7z.exe", pid=4242,
                                    code_type="ARM64", seconds=1.0)
        fex_profile.attribute(report, frames, blocks)
        self.assertEqual(report.classes.get("guest", 0), 0)
        self.assertEqual(report.classes.get("fex-runtime"), 30)
        self.assertEqual(report.mapped_addresses, 0)


if __name__ == "__main__":
    unittest.main()
