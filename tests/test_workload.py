"""Tests for the cross-runtime workload instrument.

The instrument's job is to make a claim about *one* program on *one* input, so
the tests check that it actually measures what it says: wall clock and CPU come
from the kernel, memory comes from the kernel, and correctness is the digest of
the output file rather than a log line the program printed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from porte.backends import workload  # noqa: E402


class WorkloadHarnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porte-test-workload-"))
        self.input = self.tmp / "in.bin"
        self.input.write_bytes(b"porte-workload-" * 4096)

    def test_job_expands_environment_and_paths(self):
        job_path = self.tmp / "job.json"
        job_path.write_text(json.dumps({
            "name": "t",
            "input": "${PORTE_TEST_INPUT}",
            "backends": [{"label": "copy", "kind": "native",
                          "program": "${PORTE_TEST_CP}",
                          "args": ["{in}", "{out}"]}],
        }), encoding="utf-8")
        os.environ["PORTE_TEST_INPUT"] = str(self.input)
        os.environ["PORTE_TEST_CP"] = "/bin/cp"
        try:
            job = workload.load_job(job_path)
        finally:
            del os.environ["PORTE_TEST_INPUT"]
            del os.environ["PORTE_TEST_CP"]
        self.assertEqual(job["input"], str(self.input))
        self.assertEqual(job["backends"][0]["program"], "/bin/cp")

    def test_two_configurations_of_one_program_are_compared(self):
        report = workload.run({
            "name": "copy",
            "input": str(self.input),
            "repeats": 2,
            # A process that lives long enough to be sampled: the instrument
            # reads CPU and memory from the kernel while the run is in flight.
            "backends": [
                {"label": "plain", "kind": "native", "program": "/bin/sh",
                 "args": ["-c", "sleep 0.3; exec cp {in} {out}"]},
                {"label": "with-env", "kind": "native", "program": "/bin/sh",
                 "env": {"PORTE_TEST_MARKER": "1"},
                 "args": ["-c", "sleep 0.3; exec cp {in} {out}"]},
            ],
        })
        summary = report["summary"]
        self.assertEqual(sorted(summary), ["plain", "with-env"])
        for row in summary.values():
            self.assertEqual(row["runs"], 2)
            self.assertEqual(row["exit_codes"], [0])
            self.assertEqual(row["output_sha256"], [workload._sha256(self.input)[0]])
            self.assertGreater(row["wall_ms_median"], 200.0)
            self.assertGreater(row["cpu_ms_median"], 0.0)
            self.assertGreater(row["rss_mb_median"], 0.0)
            self.assertGreaterEqual(row["threads_peak"], 1)
        self.assertTrue(report["correctness"]["identical"])
        self.assertTrue(report["correctness"]["all_exited_zero"])
        self.assertIn("outputs byte-identical across backends: True",
                      workload.summarise(report))

    def test_a_failing_program_is_not_a_fast_run(self):
        report = workload.run({
            "name": "fail",
            "input": str(self.input),
            "repeats": 1,
            "backends": [
                {"label": "missing", "kind": "native",
                 "program": "/bin/cp", "args": ["{in}", "/nonexistent/x"]},
            ],
        })
        row = report["summary"]["missing"]
        self.assertNotEqual(row["exit_codes"], [0])
        self.assertFalse(report["correctness"]["all_exited_zero"])


class CorpusTest(unittest.TestCase):
    def test_corpus_is_reproducible(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "make_input", ROOT / "fixtures" / "workload" / "make_input.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        first = module.build(1, 7)
        second = module.build(1, 7)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1024 * 1024)
        self.assertNotEqual(first, module.build(1, 8))


if __name__ == "__main__":
    unittest.main()
