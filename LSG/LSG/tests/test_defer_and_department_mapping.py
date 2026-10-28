"""Focused CPU-only checks for Gate DEFER wording and fracture department mapping."""
import json
import unittest

from LSG.runtime.action_adapter import action_for, official_candidates
from LSG.runtime.config import PROJECT
from LSG.runtime.lsg_contract import create_authorization
from LSG.runtime.pipeline import Pipeline, followup_message
from LSG.response.generator import _validate_authorization

CONTEXT = json.loads((PROJECT / 'configs/elevator-hall-context.json').read_text())
PROVENANCE = {
    'splitter_checkpoint_sha256':'a'*64,
    'gate_checkpoint_sha256':'b'*64,
    'splitter_manifest_sha256':'c'*64,
    'gate_manifest_sha256':'d'*64,
}
DEPARTMENT_A = {
    'decision':'AUTHORIZE', 'task_type':'RESPONSE_TASK',
    'response_branch':'DEPARTMENT_GUIDANCE', 'defer_type':None,
    'confidence':{'decision':.99,'task_type':.99,'response_branch':.99},
}
MEDICATION_D = {
    'decision':'DEFER', 'task_type':None,
    'response_branch':None, 'defer_type':'NEEDS_USER_INPUT',
    'confidence':{'decision':.99,'defer_type':.99},
}


class Splitter:
    def predict(self, text):
        return '[意图1] ' + text


class Gate:
    def __init__(self, prediction):
        self.prediction = prediction

    def predict(self, text, candidates):
        return {**self.prediction, 'gate_input':text}


class MappingTests(unittest.TestCase):
    def test_navigation_candidates_exclude_explicit_origin(self):
        candidates = official_candidates("请从药房带我去卫生间", CONTEXT)
        self.assertEqual(
            candidates["trusted_locations"]["candidates"],
            [{"alias": "卫生间", "location_id": "sg-f1-restroom"}],
        )

    def test_fracture_department_authorization_is_contract_valid(self):
        for text in ('骨折应该挂什么科', '骨折了来换药，我应该挂什么科'):
            with self.subTest(text=text):
                candidates = official_candidates(text, CONTEXT)
                self.assertTrue(candidates['department_catalog']['applicable'])
                action = action_for(text, DEPARTMENT_A, CONTEXT, candidates)
                self.assertEqual(action['type'], 'RECOMMEND_DEPARTMENT')
                auth = create_authorization(text, DEPARTMENT_A, candidates, CONTEXT,
                                            1, 'lsg-session-test', PROVENANCE)
                self.assertEqual(_validate_authorization(auth)['action']['type'],
                                 'RECOMMEND_DEPARTMENT')
                result = Pipeline(Splitter(), Gate(DEPARTMENT_A),
                                  integration_manifest={}, model_provenance=PROVENANCE).run(
                                      text, CONTEXT, dispatch=False)
                self.assertEqual(result['aggregate_decision'], 'AUTHORIZE')
                self.assertIsNone(result['fail_closed_reason'])
                self.assertEqual(result['dispatch_plan']['items'][0]['route'], 'Response')

    def test_gate_defer_gets_actionable_medication_followup_without_dispatch(self):
        text = '布洛芬可以用于治疗什么'
        result = Pipeline(Splitter(), Gate(MEDICATION_D),
                          integration_manifest={}, model_provenance=PROVENANCE).run(
                              text, CONTEXT, dispatch=True)
        self.assertEqual(result['per_intent_predictions'][0]['decision'], 'DEFER')
        self.assertEqual(result['aggregate_decision'], 'DEFER')
        self.assertIsNone(result['fail_closed_reason'])
        self.assertEqual(result['dispatch_plan']['items'], [])
        self.assertEqual(result['response_results'], [])
        message = result['dispatch_plan']['followups'][0]['message']
        self.assertIn('一般用途', message)
        self.assertIn('本人是否适合', message)

    def test_temperature_and_department_followups_stay_specific(self):
        self.assertIn('药品名称', followup_message('冲泡药品应该用多少度的水', CONTEXT))
        self.assertIn('主要不适', followup_message('我应该挂什么科', CONTEXT))


if __name__ == '__main__':
    unittest.main()
