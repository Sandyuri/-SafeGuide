"""Contract and generation checks for Gate-authorized unverified medication questions."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from LSG.runtime.action_adapter import action_for, official_candidates
from LSG.runtime.config import PROJECT
from LSG.runtime.lsg_contract import create_authorization, validate_response_authorization
from LSG.runtime.pipeline import Pipeline
from LSG.runtime.response_adapter import call_response

CONTEXT = json.loads((PROJECT / 'configs/elevator-hall-context.json').read_text())
PROVENANCE = {
    'splitter_checkpoint_sha256':'a'*64, 'gate_checkpoint_sha256':'b'*64,
    'splitter_manifest_sha256':'c'*64, 'gate_manifest_sha256':'d'*64,
}
PREDICTION = {
    'decision':'AUTHORIZE', 'task_type':'RESPONSE_TASK',
    'response_branch':'MEDICATION_INFORMATION', 'defer_type':None,
    'confidence':{'decision':.99,'task_type':.99,'response_branch':.99},
}


def authorization(text):
    return create_authorization(text, PREDICTION, official_candidates(text, CONTEXT),
                                CONTEXT, 1, 'lsg-session-test', PROVENANCE)


class FakeResponses:
    def __init__(self, url, answer):
        self.url = url
        self.answer = answer
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return {
            'output':[{'type':'web_search_call','action':{'sources':[{'url':self.url}]}}],
            'output_text':self.answer,
        }


class GeneralMedicationQuestionTests(unittest.TestCase):
    def test_unverified_product_temperature_has_local_bounded_answer(self):
        text = '感冒灵，冲泡药品应该用多少度的水'
        auth = authorization(text)
        action = auth.evaluated_request['action']
        self.assertEqual(action['type'], 'GET_GENERAL_MEDICATION_QUESTION')
        self.assertIsNone(action['resource'])
        self.assertEqual(action['parameters'], {
            'answer_mode':'PRODUCT_INSTRUCTIONS_UNVERIFIED',
            'product_identity_status':'UNVERIFIED',
        })
        validate_response_authorization(auth, 'GET_GENERAL_MEDICATION_QUESTION')
        with patch.dict('os.environ', {'MODEL_API_KEY':'','MODEL_NAME':'','MODEL_BASE_URL':''}):
            result, mode = call_response(auth)
        self.assertEqual(mode, 'real_local')
        self.assertEqual(result['status'], 'GENERATED')
        self.assertIn('无法', result['text'])
        self.assertIn('说明书', result['text'])
        self.assertFalse(any(char.isdigit() for char in result['text']))
        self.assertEqual(auth.decision, 'AUTHORIZE')

    def test_api_general_question_requires_official_search_source(self):
        text = '感冒灵一般有什么用途'
        auth = authorization(text)
        self.assertEqual(auth.evaluated_request['action']['parameters']['answer_mode'],
                         'PUBLIC_GENERAL_INFORMATION')
        fake = FakeResponses('https://www.nmpa.gov.cn/xxgk/example.html',
                             '公开资料介绍了该药品名称的一般用途；具体产品仍须核对包装说明书。')
        client = SimpleNamespace(responses=fake)
        with patch.dict('os.environ', {
            'MODEL_API_KEY':'fake', 'MODEL_NAME':'fake-model',
            'MODEL_BASE_URL':'https://fake.invalid',
        }), patch('LSG.runtime.response_adapter.create_client', return_value=client):
            result, mode = call_response(auth)
        self.assertEqual(mode, 'real_api')
        self.assertEqual(result['status'], 'GENERATED')
        self.assertEqual(result['source_urls'], [fake.url])
        self.assertEqual(fake.calls[0]['tool_choice'], 'required')

        bad = SimpleNamespace(responses=FakeResponses(
            'https://example.com/unverified', '无法核实该名称的一般资料。'))
        with patch.dict('os.environ', {
            'MODEL_API_KEY':'fake', 'MODEL_NAME':'fake-model',
            'MODEL_BASE_URL':'https://fake.invalid',
        }), patch('LSG.runtime.response_adapter.create_client', return_value=bad):
            failed, _ = call_response(auth)
        self.assertEqual(failed['status'], 'RESPONSE_GENERATION_FAILED')
        self.assertEqual(failed['failure_reason'], 'NO_OFFICIAL_MEDICATION_SOURCE')
        self.assertEqual(auth.decision, 'AUTHORIZE')

    def test_api_personal_dose_output_fails_generation_only(self):
        auth = authorization('感冒灵一般有什么用途')
        fake = SimpleNamespace(responses=FakeResponses(
            'https://www.nmpa.gov.cn/xxgk/example.html', '建议您每天服用两袋。'))
        with patch.dict('os.environ', {
            'MODEL_API_KEY':'fake', 'MODEL_NAME':'fake-model',
            'MODEL_BASE_URL':'https://fake.invalid',
        }), patch('LSG.runtime.response_adapter.create_client', return_value=fake):
            result, _ = call_response(auth)
        self.assertEqual(result['status'], 'RESPONSE_GENERATION_FAILED')
        self.assertEqual(result['failure_reason'], 'GENERAL_QUESTION_OUTPUT_OUT_OF_SCOPE')
        self.assertEqual(auth.decision, 'AUTHORIZE')

    def test_personal_dose_still_fails_closed(self):
        text = '我感冒了吃板蓝根，一次应该吃几袋'
        with self.assertRaisesRegex(ValueError, 'personal_medication_decision_unsupported'):
            action_for(text, PREDICTION, CONTEXT, official_candidates(text, CONTEXT))

    def test_combination_safety_question_still_fails_closed(self):
        text = '布洛芬和西替利嗪可以一起吃吗'
        with self.assertRaisesRegex(ValueError, 'personal_medication_decision_unsupported'):
            action_for(text, PREDICTION, CONTEXT, official_candidates(text, CONTEXT))

    def test_fake_pipeline_authorizes_and_dispatches_local_answer(self):
        class Splitter:
            def predict(self, text):
                return '[意图1] ' + text

        class Gate:
            def predict(self, text, candidates):
                return {**PREDICTION, 'gate_input':text}

        pipeline = Pipeline(Splitter(), Gate(), integration_manifest={},
                            model_provenance=PROVENANCE)
        with patch.dict('os.environ', {'MODEL_API_KEY':'','MODEL_NAME':'','MODEL_BASE_URL':''}):
            result = pipeline.run('感冒灵，冲泡药品应该用多少度的水', CONTEXT, dispatch=True)
        self.assertEqual(result['aggregate_decision'], 'AUTHORIZE')
        self.assertIsNone(result['fail_closed_reason'])
        self.assertEqual(result['response_results'][0]['response']['status'], 'GENERATED')
        self.assertIn('说明书', result['final_response_text'])


if __name__ == '__main__':
    unittest.main()
