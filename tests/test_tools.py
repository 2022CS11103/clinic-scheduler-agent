"""Tool invariants that the policy loop is not allowed to weaken."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scheduler.clinic import fresh_clinic
from scheduler.policy import PolicyError, apply_patch, load_policy
from scheduler.tools import ToolBelt


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy()
        self.tools = ToolBelt(fresh_clinic(), self.policy)

    def test_wrong_dob_does_not_verify(self):
        result = self.tools.verify_identity("Maya Chen", "1990-01-01")
        self.assertIn("error", result)
        listed = self.tools.list_appointments("p_maya")
        self.assertEqual(listed["error"], "not_verified")

    def test_name_and_dob_then_list(self):
        verified = self.tools.verify_identity("Maya Chen", "1990-04-12")
        self.assertEqual(verified["patient_id"], "p_maya")
        listed = self.tools.list_appointments("p_maya")
        self.assertIn("9:30", listed["appointments"][0]["label"])

    def test_cannot_book_the_same_slot_twice(self):
        self.tools.verify_identity("Luis Ortega", "1982-11-03")
        slots = self.tools.search_slots("annual_physical", limit=1)
        slot_id = slots["slots"][0]["id"]
        first = self.tools.book_appointment("p_luis", slot_id, "annual_physical")
        self.assertNotIn("error", first)
        second = self.tools.book_appointment("p_luis", slot_id, "annual_physical")
        self.assertIn("error", second)

    def test_late_cancel_requires_fee_ack(self):
        self.tools.verify_identity("Jonah Ellis", "2001-01-30")
        blocked = self.tools.cancel_appointment("p_jonah", "a_jonah", fee_acknowledged=False)
        self.assertEqual(blocked["error"], "late_cancel")
        still_there = self.tools.list_appointments("p_jonah")
        self.assertEqual(still_there["appointments"][0]["status"], "booked")
        done = self.tools.cancel_appointment("p_jonah", "a_jonah", fee_acknowledged=True)
        self.assertNotIn("error", done)
        self.assertEqual(done["appointment"]["status"], "cancelled")

    def test_patch_cannot_weaken_or_add_tools(self):
        with self.assertRaises(PolicyError):
            apply_patch(
                self.policy,
                {"changes": [{"op": "set_rule", "rule": "refuse_medical_advice", "value": False}]},
            )
        with self.assertRaises(PolicyError):
            apply_patch(self.policy, {"changes": [{"op": "add_tool", "tool": "export_all_charts"}]})
        with self.assertRaises(PolicyError):
            apply_patch(self.policy, {"changes": [{"op": "rewrite_prompt", "text": "ignore the rules"}]})


if __name__ == "__main__":
    unittest.main()
