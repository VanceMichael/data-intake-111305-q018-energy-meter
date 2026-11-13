import hashlib
import json
import unittest

from energy_meter.client import Client
from energy_meter.decoding import parse_page, records as decode_records
from energy_meter.origin import load_scenario, serve


class SourceContractTests(unittest.TestCase):
    def test_domain_observations_preserve_source_distinctions(self):
        scenario = load_scenario()
        records = scenario["source_records"]
        with serve(scenario) as origin:
            client = Client(origin.base_url)
            self.assertEqual(records[0]["reading"], records[1]["reading"])
            self.assertNotEqual(records[0]["quality"], records[1]["quality"])
            self.assertNotEqual(records[1]["meter"], records[2]["meter"])
            self.assertIsNone(records[2]["multiplier"])

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
