import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from LSG.runtime.action_adapter import action_for, official_candidates
from LSG.runtime.config import PROJECT
from LSG.runtime.lsg_contract import create_authorization, validate_response_authorization
from LSG.runtime.medication_term import extract_leaflet_term
from LSG.runtime.response_adapter import call_response
from LSG.runtime.pipeline import Pipeline

CONTEXT=json.loads((PROJECT/'configs/elevator-hall-context.json').read_text())
PROVENANCE={name:'0'*64 for name in ('splitter_checkpoint_sha256','gate_checkpoint_sha256',
                                     'splitter_manifest_sha256','gate_manifest_sha256')}
PREDICTION={'decision':'AUTHORIZE','task_type':'RESPONSE_TASK',
            'response_branch':'MEDICATION_INFORMATION','defer_type':None,
            'confidence':{'decision':.99,'task_type':.99,'response_branch':.99}}


class FakeCompletions:
    def __init__(self, answer):self.answer=answer;self.calls=[]
    def create(self,**kwargs):
        self.calls.append(kwargs)
        content=json.dumps({'explanation':self.answer,'answer_scope':'KNOWLEDGE',
                            'clinical_decision':None,'product_identity':None,
                            'verified_user_facts':[]},ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                               message=SimpleNamespace(content=content))])


class MedicationTermTests(unittest.TestCase):
    def test_reported_phrases_map_to_bounded_api_action(self):
        for text in ('药品说明书上写着吞服是什么意思',
                     '药品说明书上写着吞服，应该怎么服用这种药'):
            with self.subTest(text=text):
                self.assertEqual(extract_leaflet_term(text),'吞服')
                candidates=official_candidates(text,CONTEXT)
                action=action_for(text,PREDICTION,CONTEXT,candidates)
                self.assertEqual(action['type'],'EXPLAIN_MEDICATION_TERM')
                self.assertEqual(action['parameters'],{'term':'吞服','answer_scope':'GENERAL_TERM_ONLY'})
                auth=create_authorization(text,PREDICTION,candidates,CONTEXT,1,'lsg-session-test',PROVENANCE)
                validate_response_authorization(auth,'EXPLAIN_MEDICATION_TERM')
                fake=FakeCompletions('“吞服”表示将药物吞下。')
                client=SimpleNamespace(chat=SimpleNamespace(completions=fake))
                with patch.dict('os.environ',{'MODEL_API_KEY':'fake','MODEL_NAME':'fake-model',
                                               'MODEL_BASE_URL':'https://fake.invalid'}), \
                     patch('LSG.runtime.response_adapter.create_client',return_value=client):
                    result,mode=call_response(auth)
                self.assertEqual(mode,'real_api')
                self.assertEqual(result['status'],'GENERATED')
                self.assertIn('吞服',result['text'])
                self.assertEqual(result['applied_obligations'],[
                    'LIMIT_TO_MEDICATION_TERM','DO_NOT_PERSONALIZE_DOSING','DO_NOT_INFER_PRODUCT_INSTRUCTIONS'])
                self.assertEqual(fake.calls[0]['response_format'],{'type':'json_object'})
                self.assertIn(text,fake.calls[0]['messages'][1]['content'])

    def test_fake_pipeline_reaches_response_text(self):
        class Splitter:
            def predict(self,text):return '[意图1] '+text
        class Gate:
            def predict(self,text,candidates):return {**PREDICTION,'gate_input':text}
        fake=FakeCompletions('“吞服”表示将药物吞下。')
        client=SimpleNamespace(chat=SimpleNamespace(completions=fake))
        pipeline=Pipeline(Splitter(),Gate(),integration_manifest={},model_provenance=PROVENANCE)
        with patch.dict('os.environ',{'MODEL_API_KEY':'fake','MODEL_NAME':'fake-model',
                                       'MODEL_BASE_URL':'https://fake.invalid'}), \
             patch('LSG.runtime.response_adapter.create_client',return_value=client):
            result=pipeline.run('药品说明书上写着吞服是什么意思',CONTEXT,dispatch=True)
        self.assertEqual(result['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(result['dispatch_plan']['items'][0]['action_type'],'EXPLAIN_MEDICATION_TERM')
        self.assertIsNone(result['fail_closed_reason'])
        self.assertIn('吞服',result['final_response_text'])

    def test_personal_dose_and_missing_api_stay_blocked(self):
        unsafe='药品说明书上写着每次2片，我应该吃几片'
        self.assertIsNone(extract_leaflet_term(unsafe))
        with self.assertRaises(ValueError):
            action_for(unsafe,PREDICTION,CONTEXT,official_candidates(unsafe,CONTEXT))
        text='药品说明书上写着吞服是什么意思'
        auth=create_authorization(text,PREDICTION,official_candidates(text,CONTEXT),
                                  CONTEXT,1,'lsg-session-test',PROVENANCE)
        with patch.dict('os.environ',{'MODEL_API_KEY':'','MODEL_NAME':'','MODEL_BASE_URL':''}):
            result,mode=call_response(auth)
        self.assertEqual(mode,'boundary_only')
        self.assertEqual(result['status'],'BLOCKED_MISSING_API_ENV')

    def test_model_personal_dose_output_fails_generation_only(self):
        text='药品说明书上写着吞服是什么意思'
        auth=create_authorization(text,PREDICTION,official_candidates(text,CONTEXT),
                                  CONTEXT,1,'lsg-session-test',PROVENANCE)
        fake=FakeCompletions('您应该每天吃两粒。')
        client=SimpleNamespace(chat=SimpleNamespace(completions=fake))
        with patch.dict('os.environ',{'MODEL_API_KEY':'fake','MODEL_NAME':'fake-model',
                                       'MODEL_BASE_URL':'https://fake.invalid'}), \
             patch('LSG.runtime.response_adapter.create_client',return_value=client):
            result,_=call_response(auth)
        self.assertEqual(result['status'],'RESPONSE_GENERATION_FAILED')
        self.assertEqual(auth.decision,'AUTHORIZE')

if __name__=='__main__':unittest.main()
