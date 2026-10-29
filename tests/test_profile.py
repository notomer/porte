import unittest

from porte.backends import profile


SAMPLE = """Analysis of sampling wine (pid 58438) every 1 millisecond
Process:         7z.exe [58438]
Code Type:       X86-64 (translated)
Platform:        macOS

Call graph:
    1224 Thread_1  DispatchQueue_1: com.apple.main-thread  (serial)
    + 1224 start  (in dyld) + 3253  [0x3000690f5]

Sort by top of stack, same collapsed (when >= 5):
        ???  (in <unknown binary>)  [0x7ff892472b80]        600
        ???  (in Rosetta Runtime Routines)  [0x7ff7ffc5aed0]        200
        ???  (in Rosetta JIT)  [0x200196e5c]        150
        ???  (in Rosetta JIT)  [0x200196e68]        50

Binary Images:
       0x200000000 -        0x2000fffff  wine (11.20) [0x2] /x/wine
"""


class ProfileParserTests(unittest.TestCase):
    def test_classifies_each_layer_separately(self):
        report = profile.parse_sample(SAMPLE)
        self.assertEqual(report.code_type, "X86-64 (translated)")
        self.assertEqual(report.total_samples, 1000)
        self.assertEqual(report.classes["unattributed"], 600)
        self.assertEqual(report.classes["rosetta-runtime"], 200)
        self.assertEqual(report.classes["guest-translated"], 200)

    def test_translation_overhead_is_not_counted_as_guest_code(self):
        report = profile.parse_sample(SAMPLE)
        shares = report.shares
        self.assertAlmostEqual(shares["guest-translated"], 0.2, places=4)
        # The Rosetta runtime is overhead, not the guest's own work.
        self.assertNotIn("rosetta-runtime", ("guest-translated",))
        self.assertAlmostEqual(
            shares["rosetta-runtime"] / (shares["rosetta-runtime"]
                                         + shares["guest-translated"]),
            0.5, places=4)

    def test_symbolised_runtime_and_host_frames(self):
        text = SAMPLE + (
            "        ???  (in ntdll.so)  load address 0x2086be000 + 0x2d575"
            "  [0x2086eb575]        30\n"
            "        ???  (in CoreFoundation)  [0x7ff809439f8c]        20\n")
        report = profile.parse_sample(text)
        self.assertEqual(report.classes["compat-runtime"], 30)
        self.assertEqual(report.classes["host"], 20)

    def test_notes_state_the_attribution_limit(self):
        report = profile.parse_sample(SAMPLE)
        joined = " ".join(report.notes)
        self.assertIn("JIT addresses", joined)


if __name__ == "__main__":
    unittest.main()
