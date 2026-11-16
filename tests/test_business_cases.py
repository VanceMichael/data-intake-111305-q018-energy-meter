import json
import unittest
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class BusinessCaseFixtureTests(unittest.TestCase):
    def test_business_cases_are_concrete_and_cover_interleavings(self):
        cases = json.loads((FIXTURES / "business_cases.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 6)
        names = []
        for case in cases:
            self.assertEqual(set(case), {"name", "situation", "observable"})
            self.assertTrue(case["name"].strip() and case["situation"].strip() and case["observable"].strip())
            self.assertNotEqual(case["situation"], "situation")
            names.append(case["name"])
        for expected in ("签发后更正", "跨月换表与编号复用", "未知倍率待核", "乱序补传", "重复投递幂等", "调整接受后再更正"):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
