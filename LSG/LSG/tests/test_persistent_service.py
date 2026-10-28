"""CPU-only architecture tests. All model backends are explicit fakes."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ['CUDA_VISIBLE_DEVICES']=''
from LSG.runtime.application import LSGApplication,ServiceUnavailable
from LSG.runtime.backends import FakeSplitterBackend,FakeGateBackend
from LSG.runtime.session import SessionStore
from LSG.runtime.config import PROJECT
from LSG.runtime.models import LABELS

REG=json.loads((PROJECT/'configs/model_registry.json').read_text())
S=REG['instruct_v0.1']['expected_hash'];G=REG['embedding_v0.1']['expected_hash']
PROV={'splitter_checkpoint_sha256':S,'gate_checkpoint_sha256':G,
      'splitter_manifest_sha256':'a'*64,'gate_manifest_sha256':'b'*64}
CONTEXT=json.loads((PROJECT/'configs/elevator-hall-context.json').read_text())

def prediction(text,candidates):
    if '危险' in text: decision='REJECT'
    elif '缺少' in text and '已确认' not in text: decision='DEFER'
    else:decision='AUTHORIZE'
    robot='电梯厅' in text and '带我' in text
    task='ROBOT_TASK' if robot and decision=='AUTHORIZE' else 'RESPONSE_TASK' if decision=='AUTHORIZE' else None
    return {'decision':decision,'task_type':task,
            'response_branch':'CARE_GUIDANCE' if task=='RESPONSE_TASK' else None,
            'defer_type':'NEEDS_USER_INPUT' if decision=='DEFER' else None,
            'confidence':{key:.99 for key in LABELS},
            'raw_heads':{'decision':decision,'task_type':'ROBOT_TASK' if robot else 'RESPONSE_TASK',
                         'response_branch':'CARE_GUIDANCE','defer_type':'NEEDS_USER_INPUT'},
            'gate_input':'FAKE:'+text}

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.count={'splitter':0,'gate':0,'verify':0}
        def split_factory():self.count['splitter']+=1;return FakeSplitterBackend()
        def gate_factory():self.count['gate']+=1;return FakeGateBackend(prediction)
        def verifier():self.count['verify']+=1;return {'mock':True}
        self.app=LSGApplication(splitter_factory=split_factory,gate_factory=gate_factory,
                                verifier=verifier,provenance_factory=lambda:PROV,
                                session_store=SessionStore(Path(self.tmp.name)/'sessions'))
        self.addCleanup(self.app.shutdown)
    def start(self):self.app.start();return self.app
    def test_health_does_not_load(self):
        self.assertFalse(self.app.health()['ready']);self.assertFalse(self.app.model_info()['loaded'])
        self.assertEqual(self.count,{'splitter':0,'gate':0,'verify':0})
    def test_once_and_repeated_requests(self):
        a=self.start();a.start()
        for _ in range(3):self.assertEqual(a.evaluate('挂号后如何签到？',trusted_context=CONTEXT)['native_result']['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(self.count,{'splitter':1,'gate':1,'verify':1})
    def test_one_two_three_and_batch(self):
        calls=[];base=self.start().gate
        def batch(texts,candidates):
            calls.append(len(texts))
            return [prediction(t,c) for t,c in zip(texts,candidates)]
        with patch.object(base,'predict',side_effect=batch):
            for n in (1,2,3):
                r=self.app.evaluate('；'.join(['挂号后如何签到？']*n),trusted_context=CONTEXT)['native_result']
                self.assertEqual(len(r['intent_results']),n)
        self.assertEqual(calls,[1,2,3])
        self.assertEqual(self.count['gate'],1)
    def test_gate_swap_same_contract(self):
        self.start();a=self.app.evaluate('挂号后如何签到？',trusted_context=CONTEXT)['native_result']
        self.app.gate.predict_fn=lambda t,c:prediction(t,c)
        b=self.app.evaluate('挂号后如何签到？',trusted_context=CONTEXT)['native_result']
        self.assertEqual(a['per_intent_predictions'][0]['decision'],b['per_intent_predictions'][0]['decision'])
    def test_aggregate_and_barriers(self):
        self.start()
        for text,expected in [('挂号后如何签到？；挂号后缺少信息','DEFER'),('挂号后如何签到？；挂号后危险请求','REJECT'),('挂号后缺少信息；挂号后危险请求','REJECT')]:
            r=self.app.evaluate(text,trusted_context=CONTEXT,dispatch=True)['native_result']
            self.assertEqual(r['aggregate_decision'],expected)
            self.assertEqual(r['dispatch_plan']['items'],[]);self.assertEqual(r['response_results'],[])
    def test_defer_resume_authorize(self):
        self.start();a=self.app.evaluate('挂号后缺少信息，如何签到？',trusted_context=CONTEXT)['native_result']
        b=self.app.continue_session(a['session_id'],{1:'已确认挂号信息'},trusted_context=CONTEXT)['native_result']
        self.assertEqual(b['aggregate_decision'],'AUTHORIZE')
        self.assertIn('已确认',b['raw_input'])
    def test_defer_twice_escalates(self):
        self.start();a=self.app.evaluate('挂号后缺少信息，如何签到？',trusted_context=CONTEXT)['native_result']
        b=self.app.continue_session(a['session_id'],{1:'仍不确定'},trusted_context=CONTEXT)['native_result']
        self.assertEqual(b['aggregate_decision'],'DEFER')
        c=self.app.continue_session(a['session_id'],{1:'仍不确定'},trusted_context=CONTEXT)['native_result']
        self.assertEqual(c['dispatch_plan']['followups'][0]['type'],'HUMAN_ESCALATION_REQUIRED')
    def test_invalid_splitter_protocol_fail_closed(self):
        from LSG.runtime.backends import SplitterResult
        self.start()
        with patch.object(self.app.splitter,'split',return_value=SplitterResult('[意图2] 错误编号')):
            r=self.app.evaluate('请解释饭后服用的意思',trusted_context=CONTEXT,dispatch=True)['native_result']
        self.assertEqual(r['aggregate_decision'],'DEFER')
        self.assertEqual(r['dispatch_plan']['items'],[])
        self.assertIn('splitter_protocol',r['fail_closed_reason'])

    def test_splitter_unfounded_expansion_fail_closed(self):
        self.start()
        self.app.splitter.split_fn=lambda text:[text+'。需要解释饭后服用的意思。'*8]
        r=self.app.evaluate('请解释饭后服用的意思',trusted_context=CONTEXT,dispatch=True)['native_result']
        self.assertEqual(r['aggregate_decision'],'DEFER')
        self.assertEqual(r['dispatch_plan']['items'],[])
        self.assertIn('splitter_unfounded_expansion',r['fail_closed_reason'])

    def test_bad_session(self):
        self.start()
        with self.assertRaises(ValueError):self.app.continue_session('lsg-session-invalid',{1:'已确认'},trusted_context=CONTEXT)
    def test_multiresponse_order(self):
        self.start();r=self.app.evaluate('挂号后如何签到？；挂号后如何候诊？',trusted_context=CONTEXT)['native_result']
        self.assertEqual(r['dispatch_plan']['response_fan_in'],[1,2])
    def test_response_fanout_fanin_with_mock(self):
        self.start()
        def mock_response(auth):
            return {'status':'GENERATED','text':'MOCK:'+auth.evaluated_request['raw_text'],
                    'request_id':auth.request_id},'explicit_mock'
        with patch('LSG.runtime.pipeline.call_response',side_effect=mock_response) as called:
            r=self.app.evaluate('挂号后如何签到？；挂号后如何候诊？',trusted_context=CONTEXT,dispatch=True)['native_result']
        self.assertEqual(called.call_count,2)
        self.assertEqual([x['intent_index'] for x in r['response_results']],[1,2])
        self.assertTrue(r['final_response_text'].startswith('1. MOCK:'))
        self.assertEqual(r['aggregate_decision'],'AUTHORIZE')

    def test_response_failure_keeps_gate_decision(self):
        self.start()
        with patch('LSG.runtime.pipeline.call_response',side_effect=RuntimeError('mock_failure')):
            r=self.app.evaluate('挂号后如何签到？',trusted_context=CONTEXT,dispatch=True)['native_result']
        self.assertEqual(r['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(r['response_results'][0]['response']['status'],'RESPONSE_GENERATION_FAILED')

    def test_response_robot(self):
        self.start()
        with patch('LSG.runtime.pipeline.robot_handoff',return_value={'type':'MOCK_ROBOT_DRY_RUN'}):
            r=self.app.evaluate('挂号后如何签到？；请带我去电梯厅',trusted_context=CONTEXT)['native_result']
        self.assertEqual([x['route'] for x in r['dispatch_plan']['items']],['Response','SPINE_TO_RoboGuard_DRY_RUN'])
        self.assertEqual(r['response_results'],[])
    def test_department_request_uses_full_formal_catalog(self):
        from LSG.runtime.action_adapter import official_candidates,load_department_catalog
        formal={name for group in load_department_catalog()['categories'].values() for name in group}
        candidates=official_candidates('头疼两天，不发烧，应该挂什么科',CONTEXT)
        self.assertTrue(candidates['department_catalog']['applicable'])
        self.assertEqual(set(candidates['department_catalog']['candidates']),formal)
        self.assertEqual(official_candidates('请解释饭后服用的意思',CONTEXT)['department_catalog']['candidates'],[])

    def test_candidate_bounds_fail_closed(self):
        self.start()
        with patch('LSG.runtime.pipeline.official_candidates',return_value={'department_catalog':{'candidates':['虚构科室']},'trusted_locations':{'candidates':[]}}):
            r=self.app.evaluate('挂号后如何签到？',trusted_context=CONTEXT)['native_result']
        self.assertEqual(r['aggregate_decision'],'DEFER');self.assertEqual(r['dispatch_plan']['items'],[])
        self.assertIn('candidate_out_of_bounds',r['fail_closed_reason'])
    def test_bad_hash_start_unavailable(self):
        self.app.provenance_factory=lambda:{**PROV,'gate_checkpoint_sha256':'0'*64}
        with self.assertRaises(ValueError):self.app.start()
        self.assertFalse(self.app.health()['ready']);self.assertEqual(self.count['splitter'],0)
    def test_bad_config_start_unavailable(self):
        self.app.config['gate_backend']='unknown_gate_backend'
        with self.assertRaises(ValueError):self.app.start()
        self.assertFalse(self.app.health()['ready'])
    def test_legacy_does_not_impersonate_core(self):
        self.start();r=self.app.evaluate('挂号后如何签到？',trusted_context=CONTEXT,request_id='external-1')
        legacy=r['legacy_compatible_result']
        self.assertEqual(legacy['decision_source'],'LEARNED_SAFEGATE')
        self.assertEqual(legacy['request_id'],'external-1')
        self.assertNotIn('matched_rules',legacy);self.assertNotIn('policy_version',legacy)
        self.assertEqual(legacy['intent_results'][0]['authorization']['context_snapshot_hash'],legacy['context_snapshot_hash'])
    def test_no_old_core_calls(self):
        active=''.join(p.read_text() for p in (PROJECT/'runtime').glob('*.py'))
        for forbidden in ('SafeGateOrchestrator','from scripts.run_safegate','from orchestration.core_bridge','evaluate_legacy('):
            self.assertNotIn(forbidden,active)
    def test_shutdown(self):
        self.start();self.app.shutdown();self.assertFalse(self.app.health()['ready'])
        with self.assertRaises(ServiceUnavailable):self.app.evaluate('挂号后如何签到？',trusted_context=CONTEXT)

if __name__=='__main__':unittest.main()
