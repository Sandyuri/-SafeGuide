"""Medication action and follow-up checks without loading model weights."""
import json
import unittest

from LSG.runtime.action_adapter import action_for, official_candidates
from LSG.runtime.config import PROJECT
from LSG.runtime.pipeline import followup_message

CONTEXT = json.loads((PROJECT / 'configs/elevator-hall-context.json').read_text())
MEDICATION_AUTHORIZED = {
    'decision': 'AUTHORIZE',
    'task_type': 'RESPONSE_TASK',
    'response_branch': 'MEDICATION_INFORMATION',
    'defer_type': None,
}


class MedicationActionMappingTests(unittest.TestCase):
    def test_general_use_question_maps_to_uses(self):
        text = '布洛芬一般用于治疗什么？'
        action = action_for(text, MEDICATION_AUTHORIZED, CONTEXT, official_candidates(text, CONTEXT))
        self.assertEqual(action['type'], 'GET_GENERAL_MEDICATION_INFO')
        self.assertEqual(action['parameters']['info_type'], 'USES')
        self.assertEqual(action['resource']['id'], '布洛芬')

    def test_temperature_question_with_known_name_stays_unverified(self):
        text = '布洛芬冲泡应该用多少度的水？'
        action = action_for(text, MEDICATION_AUTHORIZED, CONTEXT, official_candidates(text, CONTEXT))
        self.assertEqual(action['type'], 'GET_GENERAL_MEDICATION_QUESTION')
        self.assertEqual(action['parameters']['answer_mode'], 'PRODUCT_INSTRUCTIONS_UNVERIFIED')

    def test_temperature_defer_requests_product_details(self):
        message = followup_message('冲泡药品应该用多少度的水比较合适？', CONTEXT)
        self.assertIn('药品名称', message)
        self.assertIn('说明书', message)


if __name__ == '__main__':
    unittest.main()
