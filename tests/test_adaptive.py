"""Tests for the adaptive-semantics specialization.

Two things must hold, and both are about refusing to apply an optimization:
the guard has to reject multi-threaded execution, and the proof has to reject a
change that alters output or does not actually help.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from porte.backends import adaptive  # noqa: E402


class GuardTest(unittest.TestCase):
    def test_single_threaded_execution_allows_the_specialization(self):
        observation = adaptive.Observation(
            program="x.exe", wall_s=10.0, cpu_s=9.4, processes=1, samples=100)
        self.assertTrue(observation.single_threaded)
        candidates = adaptive.candidates(observation)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].env, adaptive.NO_TSO)
        self.assertIn("at most one thread was executing",
                      candidates[0].reason)

    def test_two_threads_overlapping_forbids_it(self):
        observation = adaptive.Observation(
            program="x.exe", wall_s=10.0, cpu_s=19.0, processes=1, samples=100)
        self.assertFalse(observation.single_threaded)
        self.assertEqual(adaptive.candidates(observation), [])

    def test_a_second_process_forbids_it(self):
        observation = adaptive.Observation(
            program="x.exe", wall_s=10.0, cpu_s=9.0, processes=2, samples=100)
        self.assertFalse(observation.single_threaded)
        self.assertEqual(adaptive.candidates(observation), [])


class ProofTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porte-test-adaptive-"))
        self.input = self.tmp / "in.bin"
        self.input.write_bytes(b"adaptive-semantics-" * 2048)

    def job(self) -> dict:
        # The work and the extra byte are both controlled by the environment,
        # so the *same* command can stand in for a fast-but-wrong and for a
        # slow-but-correct specialization. The loop is CPU-bound on purpose:
        # a sleeping program cannot show the guard a CPU-per-wall ratio.
        script = ("i=0; while [ $i -lt ${PORTE_ITERS:-400000} ]; "
                  "do i=$((i+1)); done; cp {in} {out}; "
                  "if [ -n \"$PORTE_MARK\" ]; then printf x >> {out}; fi")
        return {
            "name": "fake-workload",
            "input": str(self.input),
            "backends": [{"label": "base", "kind": "native",
                          "program": "/bin/sh", "args": ["-c", script]}],
        }

    def candidate(self, env: dict) -> adaptive.Candidate:
        return adaptive.Candidate(name="candidate", env=env, reason="test")

    def test_a_verified_win_is_accepted(self):
        record = adaptive.evaluate(self.job(), "base",
                                   self.candidate({"PORTE_ITERS": "100000"}),
                                   repeats=2)
        self.assertTrue(record.accepted, record.reason)
        self.assertGreater(record.evidence["speedup"], 1.03)
        self.assertTrue(record.evidence["identical_output"])
        self.assertTrue(record.key.startswith("/bin/sh".replace("/", ""))
                        or "sh" in record.key)

    def test_a_change_that_alters_output_is_rejected(self):
        record = adaptive.evaluate(self.job(), "base",
                                   self.candidate({"PORTE_ITERS": "100000",
                                                   "PORTE_MARK": "1"}),
                                   repeats=2)
        # The candidate is faster, but the *program* writes different bytes
        # when the variable is set, which is exactly what must never ship.
        self.assertFalse(record.accepted)
        self.assertIn("byte for byte", record.reason)

    def test_a_slower_specialization_is_rejected(self):
        record = adaptive.evaluate(self.job(), "base",
                                   self.candidate({"PORTE_ITERS": "900000"}),
                                   repeats=2)
        self.assertFalse(record.accepted)
        self.assertIn("below the", record.reason)

    def test_only_accepted_records_load_back(self):
        root = self.tmp / "cache"
        accepted = adaptive.Record(program="x", key="a", accepted=True,
                                   reason="ok")
        rejected = adaptive.Record(program="y", key="b", accepted=False,
                                   reason="no")
        adaptive.save(accepted, root)
        adaptive.save(rejected, root)
        self.assertIsNotNone(adaptive.load("a", root))
        self.assertIsNone(adaptive.load("b", root))
        self.assertIsNone(adaptive.load("missing", root))


if __name__ == "__main__":
    unittest.main()
