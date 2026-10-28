import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from LSG.runtime.config import PROJECT,paths,verify
from LSG.runtime.models import parse_split,validate_gate
from LSG.runtime.pipeline import Pipeline,aggregate
from LSG.runtime.orchestrator import LSGOrchestrator
from LSG.runtime.session import SessionStore
from LSG.runtime.lsg_contract import validate_response_authorization,validate_schema

CONTEXT=json.loads((PROJECT/'tests/fixtures/public-care-available.json').read_text())
NAV_CONTEXT=json.loads((PROJECT/'tests/fixtures/navigation-enabled.json').read_text())
A={'decision':'AUTHORIZE','task_type':'RESPONSE_TASK','response_branch':'CARE_GUIDANCE','defer_type':None,'confidence':{'decision':.99,'task_type':.99,'response_branch':.99}}
D={'decision':'DEFER','task_type':None,'response_branch':None,'defer_type':'NEEDS_USER_INPUT','confidence':{'decision':.99,'defer_type':.99}}
R={'decision':'REJECT','task_type':None,'response_branch':None,'defer_type':None,'confidence':{'decision':.99}}
ROBOT={**A,'task_type':'ROBOT_TASK','response_branch':None}

class Splitter:
    def predict(self,text):
        import re
        return '\n'.join(f'[意图{i}] {part}' for i,part in enumerate(re.split(r'；(?:另外)?',text),1))
class Gate:
    def predict(self,text,candidates):
        choice=R if '危险' in text else D if '缺少' in text and '已确认' not in text else ROBOT if '302床' in text else A
        return {**choice,'gate_input':text}

def pipeline():
    with patch('LSG.runtime.pipeline.verify',return_value={}):return Pipeline(Splitter(),Gate())

class NativeTests(unittest.TestCase):
    def test_embedding_gate_batch_error_identifies_intent(self):
        from LSG.runtime.backends import EmbeddingGateBackend
        class FailingModel:
            def predict(self,text,candidates):
                if text == "second": raise ValueError("low_confidence_decision")
                return A
        backend=EmbeddingGateBackend.__new__(EmbeddingGateBackend)
        backend._model=FailingModel()
        with self.assertRaisesRegex(ValueError,"gate_intent_2:low_confidence_decision"):
            backend.predict(["first","second"],[{},{}])

    def test_hashes_and_splitter(self):
        self.assertIn('gate_artifact_checkpoint',verify(verify_base_models=False)['hashes'])
        self.assertEqual(parse_split('[意图1] 请求一'),['请求一'])
        with self.assertRaises(ValueError):parse_split('[意图2] 错误')
    def test_gate_mask_and_aggregate(self):
        validate_gate(A)
        with self.assertRaises(ValueError):validate_gate({**A,'defer_type':'NEEDS_USER_INPUT'})
        self.assertEqual(aggregate(['AUTHORIZE','DEFER']),'DEFER')
        self.assertEqual(aggregate(['AUTHORIZE','REJECT']),'REJECT')
    def test_gate_only_authorization_response_contract(self):
        r=pipeline().run('挂号后如何签到候诊？',CONTEXT)
        self.assertEqual(r['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(r['response_results'][0]['response']['status'],'GENERATED')
        self.assertTrue(r['final_response_text'])
        auth=r['authorizations'][0]
        validate_schema('lsg-authorization.schema.json',auth)
        validate_response_authorization(auth)
        self.assertEqual(auth['request_id'],r['response_results'][0]['response']['request_id'])
        self.assertNotIn('safegate_output',auth)
    def test_all_response_branches_accept_native_authorization(self):
        from LSG.runtime.action_adapter import official_candidates
        from LSG.runtime.lsg_contract import create_authorization,provenance
        from LSG.response.medication import _validate as medication_validate
        from LSG.response.generator import _validate_authorization as department_validate
        from LSG.response.public_care import _validate as public_validate
        samples=[('请解释“饭后服用”的一般含义。','MEDICATION_INFORMATION',lambda a:medication_validate(a,'EXPLAIN_MEDICATION_CONCEPT')),
                 ('右眼痒了两天，应该咨询哪个科？','DEPARTMENT_GUIDANCE',department_validate),
                 ('医院官网的门诊预约流程在哪里查看？','PUBLIC_CARE_INFORMATION',public_validate)]
        for text,branch,validator in samples:
            with self.subTest(branch=branch):
                prediction={**A,'response_branch':branch}
                auth=create_authorization(text,prediction,official_candidates(text,CONTEXT),CONTEXT,1,'lsg-session-test',provenance())
                validator(auth)

    def test_public_care_search_fallback_and_terms(self):
        from LSG.response.public_care import OFFICIAL_HOST, _hospital_search_terms, _official_page_hint, _says_not_found, _strip_search_narration
        self.assertTrue(_says_not_found("官网公开页面中未检索到具体信息"))
        self.assertEqual(_hospital_search_terms("医院门诊的挂号时间是什么"),"门诊导航 挂号时间 就诊安排")
        self.assertEqual(_hospital_search_terms("医院可以借轮椅吗"),"便民措施 轮椅 租借 服务咨询")
        self.assertEqual(_official_page_hint("医院门诊的挂号时间是什么"),f"https://{OFFICIAL_HOST}/ylwSingle/index_138.aspx")
        narrated="I'll search and crawl the specified official page.\n根据抓取到的官网页面正文：\n\n**挂号时间：** 8:30。\n\n（该页面有多个栏目，但本次抓取未见其他条目。）"
        self.assertEqual(_strip_search_narration(narrated),"**挂号时间：** 8:30。")

    def test_general_purchase_response_requires_authoritative_search_source(self):
        from types import SimpleNamespace
        from LSG.runtime.action_adapter import official_candidates
        from LSG.runtime.lsg_contract import create_authorization, provenance
        from LSG.response.public_care import generate_public_care_response
        text='除了医院，还要哪里可以买到正规的药'
        auth=create_authorization(text,{**A,'response_branch':'PUBLIC_CARE_INFORMATION'},
                                  official_candidates(text,CONTEXT),CONTEXT,1,'lsg-session-test',provenance())
        class FakeResponses:
            def __init__(self,url):self.url=url
            def create(self,**kwargs):
                return {'output':[{'type':'web_search_call','action':{'sources':[{'url':self.url}]}}],
                        'output_text':'可以向有资质的零售药店咨询一般购药渠道。'}
        with patch.dict('os.environ',{'MODEL_NAME':'fake-model'}):
            good=generate_public_care_response(auth,client=SimpleNamespace(responses=FakeResponses('https://www.nmpa.gov.cn/xxgk/example.html')))
            bad=generate_public_care_response(auth,client=SimpleNamespace(responses=FakeResponses('https://example.com/example.html')))
        self.assertEqual(good.status,'GENERATED')
        self.assertEqual(good.applied_obligations,['USE_AUTHORITATIVE_PUBLIC_SOURCE'])
        self.assertEqual(bad.status,'RESPONSE_GENERATION_FAILED')
        self.assertEqual(bad.failure_reason,'NO_OFFICIAL_SEARCH_SOURCE')

    def test_response_failure_keeps_gate_decision(self):
        with patch('LSG.runtime.pipeline.call_response',side_effect=RuntimeError('failure')):
            r=pipeline().run('挂号后如何签到候诊？',CONTEXT)
        self.assertEqual(r['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(r['response_results'][0]['response']['status'],'RESPONSE_GENERATION_FAILED')
    def test_reject_defer_barriers(self):
        for text,decision in [('挂号后缺少信息','DEFER'),('挂号后危险请求','REJECT')]:
            r=pipeline().run(text,CONTEXT)
            self.assertEqual(r['aggregate_decision'],decision)
            self.assertEqual(r['dispatch_plan']['items'],[])
            self.assertEqual(r['response_results'],[])
    def test_nested_location_aliases_use_specific_official_name(self):
        from LSG.runtime.action_adapter import official_candidates
        context=json.loads((PROJECT/'configs/elevator-hall-context.json').read_text())
        for phrase, expected in [('带我去b超室','sg-f1-functional-exam-waiting'),
                                 ('带我去无障碍卫生间','sg-f1-accessible-restroom'),
                                 ('带我去药物咨询室','sg-f1-medication-consultation')]:
            with self.subTest(phrase=phrase):
                candidates=official_candidates(phrase,context)['trusted_locations']['candidates']
                self.assertEqual(len(candidates),1)
                self.assertEqual(candidates[0]['location_id'],expected)
        self.assertEqual(len(official_candidates('带我去咨询处或者咨询室',context)['trusted_locations']['candidates']),2)

    def test_public_information_source_scopes(self):
        from LSG.runtime.action_adapter import action_for, official_candidates
        from LSG.runtime.lsg_contract import create_authorization, provenance
        from LSG.response.public_care import _validate as public_validate, _official_url
        cases=[('除了医院，还要哪里可以买到正规的药','general_drug_purchase','USE_AUTHORITATIVE_PUBLIC_SOURCE'),
               ('哪里可以买到正规的药','general_drug_purchase','USE_AUTHORITATIVE_PUBLIC_SOURCE'),
               ('医院哪里能买药','public_hospital_service','USE_PUBLIC_HOSPITAL_SOURCE'),
               ('医院哪里能借到轮椅','public_hospital_service','USE_PUBLIC_HOSPITAL_SOURCE'),
               ('出院一般需要哪些步骤','public_hospital_service','USE_PUBLIC_HOSPITAL_SOURCE')]
        for text,topic,obligation in cases:
            with self.subTest(text=text):
                prediction={**A,'response_branch':'PUBLIC_CARE_INFORMATION'}
                candidates=official_candidates(text,CONTEXT)
                action=action_for(text,prediction,CONTEXT,candidates)
                self.assertEqual(action['parameters']['topic'],topic)
                auth=create_authorization(text,prediction,candidates,CONTEXT,1,'lsg-session-test',provenance())
                self.assertEqual(auth.obligations,[obligation])
                public_validate(auth)
        self.assertTrue(_official_url('https://www.nmpa.gov.cn/xxgk/example.html','general_drug_purchase'))
        self.assertFalse(_official_url('https://example.com/example.html','general_drug_purchase'))
        self.assertFalse(_official_url('https://www.nmpa.gov.cn/xxgk/example.html','public_hospital_service'))

    def test_candidate_out_of_bounds(self):
        bad={'department_catalog':{'applicable':True,'candidates':['虚构科室']},
             'trusted_locations':{'applicable':False,'candidates':[]}}
        r=pipeline().run('挂号后如何签到候诊？',CONTEXT,candidate_override=bad)
        self.assertEqual(r['aggregate_decision'],'DEFER')
        self.assertIn('candidate_out_of_bounds',r['fail_closed_reason'])
    def test_robot_adapter_dry_run(self):
        r=pipeline().run('请带我到302床。',NAV_CONTEXT)
        self.assertEqual(r['aggregate_decision'],'AUTHORIZE')
        item=r['dispatch_plan']['items'][0]
        self.assertEqual(item['route'],'SPINE_TO_RoboGuard_DRY_RUN')
        self.assertEqual(item['authorized_task']['target']['id'],'ward-a-bed-302')
        self.assertEqual(r['response_results'],[])
    def test_robot_adapter_rejects_wrong_context_and_provenance(self):
        r=pipeline().run('请带我到302床。',NAV_CONTEXT)
        from LSG.runtime.pipeline import robot_handoff
        auth=r['authorizations'][0]
        class Obj:
            def to_dict(self):return auth
        with self.assertRaises(ValueError):robot_handoff(Obj(),CONTEXT,r['provenance'],r['runtime_candidates'][0])
        with self.assertRaises(ValueError):robot_handoff(Obj(),NAV_CONTEXT,{'bad':'0'*64},r['runtime_candidates'][0])
        with self.assertRaises(ValueError):robot_handoff(Obj(),NAV_CONTEXT,r['provenance'],{'trusted_locations':{'candidates':[]}})
    def test_no_old_core_import_or_invocation(self):
        r=pipeline().run('挂号后如何签到候诊？',CONTEXT)
        self.assertEqual(r['aggregate_decision'],'AUTHORIZE')
        self.assertNotIn('orchestration.orchestrator',sys.modules)
        self.assertNotIn('scripts.run_safegate',sys.modules)
        active=''.join(p.read_text() for base in ('runtime','response') for p in (PROJECT/base).glob('*.py'))
        for forbidden in ('SafeGateOrchestrator','InMemorySessionStore','evaluate_legacy','from scripts.run_safegate','from orchestration.core_bridge'):
            self.assertNotIn(forbidden,active)

class CliTests(unittest.TestCase):
    def test_default_context_contains_formal_robot_and_department_sources(self):
        from LSG.runtime.config import default_context_path
        context=json.loads(default_context_path().read_text())
        self.assertEqual(context['reference_resolution']['location_aliases']['电梯厅'],'sg-f1-public-elevator-hall')
        self.assertIn('诊室',context['reference_resolution']['location_aliases'])
        self.assertTrue((PROJECT/'response/department_catalog.json').exists())

    def test_interactive_defer_consumes_next_line_as_supplement(self):
        import io
        from contextlib import redirect_stdout,redirect_stderr
        from LSG.scripts import run_lsg
        with tempfile.TemporaryDirectory() as directory:
            prompts=[]
            answers=iter(['挂号后缺少信息，如何签到？','已确认挂号信息','/退出'])
            def fake_input(prompt):
                prompts.append(prompt);return next(answers)
            stdout=io.StringIO();stderr=io.StringIO()
            with patch.object(run_lsg,'Pipeline',side_effect=lambda:pipeline()), \
                 patch('builtins.input',side_effect=fake_input), \
                 patch.object(sys,'argv',['run_lsg.py','--session-store',directory]), \
                 redirect_stdout(stdout),redirect_stderr(stderr):
                self.assertEqual(run_lsg.main(),0)
            self.assertTrue(any('补充意图1' in prompt for prompt in prompts))
            self.assertIn('查看挂号凭证',stdout.getvalue())
            self.assertEqual(stderr.getvalue(),'')
            self.assertEqual(len(prompts),2)

class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=SessionStore(self.tmp.name);self.o=LSGOrchestrator(pipeline(),self.store)
    def test_defer_supplement_authorize(self):
        first=self.o.start('挂号后缺少信息，如何签到？',CONTEXT)
        self.assertEqual(first['aggregate_decision'],'DEFER')
        second=self.o.resume(first['session_id'],{1:'已确认挂号信息'},CONTEXT)
        self.assertEqual(second['aggregate_decision'],'AUTHORIZE')
        self.assertTrue(second['final_response_text'])
        self.assertEqual(self.store.get(first['session_id'])['state'],'TERMINAL')
    def test_session_store_recreates_deleted_directory(self):
        directory=Path(self.tmp.name)/'removed-sessions'
        store=SessionStore(directory)
        directory.rmdir()
        record={'session_id':'lsg-session-directory-recovery','state':'TERMINAL'}
        store.put(record)
        self.assertEqual(store.get(record['session_id']),record)

    def test_session_restores_from_disk(self):
        first=self.o.start('挂号后缺少信息，如何签到？',CONTEXT)
        restored=LSGOrchestrator(pipeline(),SessionStore(self.tmp.name))
        second=restored.resume(first['session_id'],{1:'已确认挂号信息'},CONTEXT)
        self.assertEqual(second['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(second['session_id'],first['session_id'])

    def test_defer_twice_then_escalate(self):
        first=self.o.start('挂号后缺少信息，如何签到？',CONTEXT)
        second=self.o.resume(first['session_id'],{1:'仍不确定'},CONTEXT)
        self.assertEqual(second['aggregate_decision'],'DEFER')
        third=self.o.resume(first['session_id'],{1:'仍不确定'},CONTEXT)
        self.assertEqual(third['dispatch_plan']['followups'][0]['type'],'HUMAN_ESCALATION_REQUIRED')
        self.assertEqual(self.store.get(first['session_id'])['state'],'HUMAN_ESCALATION_REQUIRED')
    def test_partial_defer_reassesses_all(self):
        first=self.o.start('挂号后如何签到候诊？；另外挂号后缺少信息，如何签到？',CONTEXT)
        self.assertEqual(first['aggregate_decision'],'DEFER')
        self.assertEqual(first['response_results'],[])
        record=self.store.get(first['session_id'])
        self.assertEqual([x['intent_index'] for x in record['pending']],[2])
        self.assertEqual(len(record['suspended_authorized_intents']),1)
        second=self.o.resume(first['session_id'],{2:'已确认挂号信息'},CONTEXT)
        self.assertEqual(second['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(second['dispatch_plan']['response_fan_in'],[1,2])
        self.assertEqual(len(second['response_results']),2)
        self.assertNotEqual(first['authorizations'][0]['evaluation_id'],second['authorizations'][0]['evaluation_id'])
    def test_partial_defer_then_reject_blocks_all(self):
        first=self.o.start('挂号后如何签到候诊？；另外挂号后缺少信息，如何签到？',CONTEXT)
        second=self.o.resume(first['session_id'],{2:'危险补充'},CONTEXT)
        self.assertEqual(second['aggregate_decision'],'REJECT')
        self.assertEqual(second['dispatch_plan']['items'],[])
    def test_human_escalation_does_not_auto_follow_up(self):
        class HumanGate:
            def predict(self,text,candidates):
                return {**D,'defer_type':'HUMAN_ESCALATION_REQUIRED','gate_input':text}
        with patch('LSG.runtime.pipeline.verify',return_value={}):native=Pipeline(Splitter(),HumanGate())
        o=LSGOrchestrator(native,self.store)
        first=o.start('挂号后信息不明，如何签到？',CONTEXT)
        self.assertEqual(self.store.get(first['session_id'])['state'],'HUMAN_ESCALATION_REQUIRED')
        self.assertEqual(first['dispatch_plan']['items'],[])
        with self.assertRaises(ValueError):o.resume(first['session_id'],{1:'补充'},CONTEXT)

    def test_robot_destination_correction_uses_formal_alias(self):
        from LSG.runtime.config import default_context_path
        context=json.loads(default_context_path().read_text())
        class DestinationGate:
            def predict(self,text,candidates):
                choice=ROBOT if len(candidates['trusted_locations']['candidates'])==1 else D
                return {**choice,'gate_input':text}
        with patch('LSG.runtime.pipeline.verify',return_value={}):
            native=Pipeline(Splitter(),DestinationGate())
        o=LSGOrchestrator(native,self.store)
        first=o.start('带我去挂号',context,dispatch=False)
        self.assertEqual(first['aggregate_decision'],'DEFER')
        self.assertIn('挂号收费窗口',first['dispatch_plan']['followups'][0]['message'])
        second=o.resume(first['session_id'],{1:'挂号处'},context,dispatch=False)
        self.assertEqual(second['aggregate_decision'],'DEFER')
        self.assertEqual(second['runtime_candidates'][0]['trusted_locations']['candidates'],[])
        with patch('LSG.runtime.pipeline.robot_handoff',return_value={'type':'MOCK_DRY_RUN'}):
            third=o.resume(first['session_id'],{1:'挂号收费窗口'},context,dispatch=False)
        self.assertEqual(third['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(third['raw_input'],'挂号收费窗口，带我去挂号')
        self.assertEqual(third['dispatch_plan']['items'][0]['route'],'SPINE_TO_RoboGuard_DRY_RUN')
        self.assertEqual(o.store.get(first['session_id'])['supplement_history']['1'],['挂号处','挂号收费窗口'])

    def test_supplement_that_splits_into_new_intent_fails_closed(self):
        class DriftSplitter:
            def predict(self,text):
                if '带我去' in text and '我想知道应该去哪里挂号' in text:
                    return '[意图1] 带我去挂号\n[意图2] 我想知道应该去哪里挂号'
                return '[意图1] '+text
        class DeferGate:
            def predict(self,text,candidates):return {**D,'gate_input':text}
        with patch('LSG.runtime.pipeline.verify',return_value={}):
            native=Pipeline(DriftSplitter(),DeferGate())
        o=LSGOrchestrator(native,self.store)
        with patch('LSG.runtime.pipeline.call_response') as response_api:
            first=o.start('我想知道应该去哪里挂号',CONTEXT,dispatch=True)
            self.assertEqual(first['aggregate_decision'],'DEFER')
            second=o.resume(first['session_id'],{1:'带我去挂号'},CONTEXT,dispatch=True)
            response_api.assert_not_called()
        self.assertEqual(second['aggregate_decision'],'DEFER')
        self.assertIn('resume_intent_count_changed',second['fail_closed_reason'])
        self.assertEqual(second['dispatch_plan']['items'],[])
        self.assertEqual([x['intent_index'] for x in self.store.get(first['session_id'])['pending']],[1])
        self.assertEqual(len(self.store.get(first['session_id'])['intents']),1)
        self.assertIn('额外请求',second['dispatch_plan']['followups'][0]['message'])
        third=o.resume(first['session_id'],{1:'带我去挂号处'},CONTEXT,dispatch=True)
        self.assertEqual(third['dispatch_plan']['followups'][0]['type'],'HUMAN_ESCALATION_REQUIRED')
        self.assertEqual(self.store.get(first['session_id'])['state'],'HUMAN_ESCALATION_REQUIRED')

    def test_restored_legacy_mismatched_session_does_not_crash(self):
        first=self.o.start('挂号后缺少信息，如何签到？',CONTEXT)
        record=self.store.get(first['session_id'])
        record['intents'].append('带我去挂号')
        self.store.put(record)
        result=self.o.resume(first['session_id'],{1:'补充'},CONTEXT)
        self.assertEqual(result['aggregate_decision'],'DEFER')
        self.assertEqual(result['fail_closed_reason'],'session_intent_count_mismatch')
        self.assertEqual(self.store.get(first['session_id'])['state'],'HUMAN_ESCALATION_REQUIRED')

    def test_wrong_session_and_no_pending(self):
        with self.assertRaises(ValueError):self.o.resume('lsg-session-missing',{1:'补充'},CONTEXT)
        first=self.o.start('挂号后如何签到候诊？',CONTEXT)
        with self.assertRaises(ValueError):self.o.resume(first['session_id'],{1:'补充'},CONTEXT)
    def test_multiple_pending_bound_to_indices(self):
        first=self.o.start('挂号后缺少信息甲，如何签到？；另外挂号后缺少信息乙，如何签到？',CONTEXT)
        self.assertEqual(len(self.store.get(first['session_id'])['pending']),2)
        with self.assertRaises(ValueError):self.o.resume(first['session_id'],{1:'已确认'},CONTEXT)
        second=self.o.resume(first['session_id'],{1:'已确认甲',2:'已确认乙'},CONTEXT)
        self.assertEqual(second['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(len(self.store.get(first['session_id'])['audit_history']),2)

if __name__=='__main__':unittest.main()
