import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from LSG.runtime.location_resolver import APILocationResolver, LocationResolverError, validate_resolution
from LSG.runtime.config import PROJECT
from LSG.runtime.pipeline import Pipeline
from LSG.runtime.orchestrator import LSGOrchestrator
from LSG.runtime.session import SessionStore

CONTEXT=json.loads((PROJECT/'configs/elevator-hall-context.json').read_text())


def entry(alias):
    return {'alias':alias,'location_id':CONTEXT['reference_resolution']['location_aliases'][alias]}


class FakeSplitter:
    def predict(self,text):return '[意图1] '+text


class FakeGate:
    def __init__(self):self.calls=[]
    def predict(self,text,candidates):
        self.calls.append((text,candidates))
        return {'decision':'AUTHORIZE','task_type':'ROBOT_TASK','response_branch':None,
                'defer_type':None,'confidence':{'decision':.99,'task_type':.99},'gate_input':text}


class FakeResolver:
    def __init__(self):self.calls=[]
    def resolve(self,text,context):
        self.calls.append(text)
        if '挂号收费窗口' in text:
            return {'status':'RESOLVED','candidates':[entry('挂号收费窗口')]}
        if '挂号' in text:
            return {'status':'AMBIGUOUS','candidates':[entry('挂号收费窗口'),entry('自助挂号收费机')]}
        if 'b超室' in text:
            return {'status':'RESOLVED','candidates':[entry('b超室')]}
        return {'status':'UNKNOWN','candidates':[]}


class LocationResolverTests(unittest.TestCase):
    def pipeline(self,resolver,gate=None):
        return Pipeline(FakeSplitter(),gate or FakeGate(),integration_manifest={},
                        model_provenance={'splitter_checkpoint_sha256':'0'*64,'gate_checkpoint_sha256':'0'*64,
                                          'splitter_manifest_sha256':'0'*64,'gate_manifest_sha256':'0'*64},
                        location_resolver=resolver)

    def test_api_proposal_must_be_exact_allowlist_pair(self):
        self.assertEqual(validate_resolution({'status':'RESOLVED','candidates':[entry('b超室')]},CONTEXT)['candidates'],[entry('b超室')])
        for bad in ({'status':'RESOLVED','candidates':[{'alias':'虚构地点','location_id':'fake'}]},
                    {'status':'RESOLVED','candidates':[{'alias':'b超室','location_id':'sg-f1-restroom'}]},
                    {'status':'AMBIGUOUS','candidates':[entry('挂号收费窗口'),entry('挂号收费窗口')]},
                    {'status':'RESOLVED','candidates':[entry('挂号收费窗口'),entry('自助挂号收费机')]}):
            with self.subTest(bad=bad),self.assertRaises(LocationResolverError):
                validate_resolution(bad,CONTEXT)

    def test_ambiguous_choice_reenters_splitter_gate_and_robot_dry_run(self):
        resolver=FakeResolver();gate=FakeGate()
        orchestrator=LSGOrchestrator(self.pipeline(resolver,gate),SessionStore())
        first=orchestrator.start('带我去挂号',CONTEXT,dispatch=False)
        self.assertEqual(first['aggregate_decision'],'DEFER')
        self.assertEqual(gate.calls,[])
        self.assertEqual(first['dispatch_plan']['items'],[])
        self.assertIn('自助挂号收费机',first['dispatch_plan']['followups'][0]['message'])
        record=orchestrator.store.get(first['session_id'])
        self.assertEqual(record['state'],'PENDING_USER_INPUT')
        second=orchestrator.resume(first['session_id'],{1:'挂号收费窗口'},CONTEXT,dispatch=False)
        self.assertEqual(second['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(len(gate.calls),1)
        self.assertIn('挂号收费窗口',gate.calls[0][0])
        self.assertEqual(second['dispatch_plan']['items'][0]['target_location_id'],'sg-f1-registration-payment-counter')
        self.assertEqual(second['dispatch_plan']['items'][0]['route'],'SPINE_TO_RoboGuard_DRY_RUN')
        self.assertEqual(second['response_results'],[])

    def test_offered_choice_accepts_display_whitespace_and_uses_canonical_alias(self):
        resolver=FakeResolver();gate=FakeGate()
        orchestrator=LSGOrchestrator(self.pipeline(resolver,gate),SessionStore())
        first=orchestrator.start('带我去挂号',CONTEXT,dispatch=False)
        second=orchestrator.resume(first['session_id'],{1:'自助挂号 收费机\u200b'},CONTEXT,dispatch=False)
        self.assertEqual(second['aggregate_decision'],'AUTHORIZE')
        self.assertIsNone(second['fail_closed_reason'])
        self.assertEqual(second['dispatch_plan']['items'][0]['target_location_id'],
                         entry('自助挂号收费机')['location_id'])
        self.assertIn('带我去自助挂号收费机',gate.calls[0][0])
        self.assertEqual(orchestrator.store.get(first['session_id'])['supplement_history']['1'],
                         ['自助挂号 收费机\u200b'])

    def test_invalid_offered_choice_is_audited(self):
        orchestrator=LSGOrchestrator(self.pipeline(FakeResolver()),SessionStore())
        first=orchestrator.start('带我去挂号',CONTEXT,dispatch=False)
        second=orchestrator.resume(first['session_id'],{1:'药房'},CONTEXT,dispatch=False)
        self.assertEqual(second['fail_closed_reason'],'location_choice_outside_offered_candidates')
        record=orchestrator.store.get(first['session_id'])
        self.assertEqual(record['audit_history'][-1]['raw_input'],'药房')
        self.assertEqual(record['attempt'],1)

    def test_waiting_area_choice_is_pinned_for_gate_recheck(self):
        class WaitingResolver:
            def __init__(self):self.calls=[]
            def resolve(self,text,context):
                self.calls.append(text)
                if text=='带我去等候区':
                    return {'status':'AMBIGUOUS','candidates':[entry('门诊候诊'),
                        entry('功能检查等候区'),entry('功能检查候诊')]}
                # This would reproduce the live failure if the second turn
                # incorrectly asked the API to resolve the generic phrase again.
                return {'status':'UNKNOWN','candidates':[]}
        resolver=WaitingResolver();gate=FakeGate()
        orchestrator=LSGOrchestrator(self.pipeline(resolver,gate),SessionStore())
        first=orchestrator.start('带我去等候区',CONTEXT,dispatch=False)
        self.assertEqual(first['aggregate_decision'],'DEFER')
        self.assertEqual(len(resolver.calls),1)
        second=orchestrator.resume(first['session_id'],{1:'门诊候诊'},CONTEXT,dispatch=False)
        self.assertEqual(second['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(len(resolver.calls),1)
        self.assertEqual(len(gate.calls),1)
        self.assertEqual(gate.calls[0][1]['trusted_locations']['candidates'],[entry('门诊候诊')])
        self.assertEqual(second['dispatch_plan']['items'][0]['target_location_id'],entry('门诊候诊')['location_id'])

    def test_gate_defer_after_selected_waiting_area_escalates_without_reasking(self):
        class WaitingResolver:
            def resolve(self,text,context):
                return {'status':'AMBIGUOUS','candidates':[entry('门诊候诊'),
                    entry('功能检查等候区'),entry('功能检查候诊')]}
        class DeferringGate(FakeGate):
            def predict(self,text,candidates):
                self.calls.append((text,candidates))
                return {'decision':'DEFER','task_type':None,'response_branch':None,
                        'defer_type':'NEEDS_USER_INPUT',
                        'confidence':{'decision':.98,'defer_type':.99},'gate_input':text}
        gate=DeferringGate()
        orchestrator=LSGOrchestrator(self.pipeline(WaitingResolver(),gate),SessionStore())
        first=orchestrator.start('带我去等候区',CONTEXT,dispatch=False)
        second=orchestrator.resume(first['session_id'],{1:'门诊候诊'},CONTEXT,dispatch=False)
        self.assertEqual(second['aggregate_decision'],'DEFER')
        self.assertEqual(second['dispatch_plan']['items'],[])
        self.assertEqual(second['dispatch_plan']['followups'][0]['type'],'HUMAN_ESCALATION_REQUIRED')
        self.assertIn('目的地已确认',second['dispatch_plan']['followups'][0]['message'])
        self.assertEqual(orchestrator.store.get(first['session_id'])['state'],'HUMAN_ESCALATION_REQUIRED')
        self.assertEqual(len(gate.calls),1)

    def test_choice_outside_offered_candidates_stays_blocked(self):
        orchestrator=LSGOrchestrator(self.pipeline(FakeResolver()),SessionStore())
        first=orchestrator.start('带我去挂号',CONTEXT,dispatch=False)
        second=orchestrator.resume(first['session_id'],{1:'药房'},CONTEXT,dispatch=False)
        self.assertEqual(second['aggregate_decision'],'DEFER')
        self.assertEqual(second['dispatch_plan']['items'],[])
        self.assertEqual(second['fail_closed_reason'],'location_choice_outside_offered_candidates')

    def test_exact_model_selected_place_reaches_robot_adapter(self):
        resolver=FakeResolver()
        result=self.pipeline(resolver).run('带我去b超室',CONTEXT,dispatch=False)
        self.assertEqual(resolver.calls,[])
        self.assertEqual(result['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(result['dispatch_plan']['items'][0]['target_location_id'],'sg-f1-functional-exam-waiting')

    def test_bad_model_candidate_fail_closed_before_gate(self):
        class BadResolver:
            def resolve(self,text,context):
                return {'status':'RESOLVED','candidates':[{'alias':'虚构地点','location_id':'fake'}]}
        gate=FakeGate();result=self.pipeline(BadResolver(),gate).run('带我去虚构地点',CONTEXT,dispatch=False)
        self.assertEqual(result['aggregate_decision'],'DEFER')
        self.assertEqual(result['dispatch_plan']['items'],[])
        self.assertEqual(gate.calls,[])
        self.assertIn('location_candidate_out_of_bounds',result['fail_closed_reason'])

    def test_api_backend_accepts_bounded_json_wrappers(self):
        from LSG.runtime.location_resolver import _parse_payload
        expected={'status':'RESOLVED','candidates':[entry('药房')]}
        raw=json.dumps(expected,ensure_ascii=False)
        self.assertEqual(_parse_payload("```json "+raw+" ```"),expected)
        self.assertEqual(_parse_payload('解析结果如下：'+raw+'。'),expected)
        for bad in ("not json","[1,2,3]","```json {bad} ```"):
            with self.subTest(bad=bad),self.assertRaises(LocationResolverError):
                _parse_payload(bad)

    def test_api_backend_uses_json_and_rejects_malformed_output(self):
        class FakeCreate:
            def __init__(self,value):self.value=value;self.calls=[]
            def create(self,**kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.value))])
        create=FakeCreate(json.dumps({'status':'RESOLVED','candidates':[entry('药房')]},ensure_ascii=False))
        client=SimpleNamespace(chat=SimpleNamespace(completions=create))
        with patch.dict('os.environ',{'MODEL_NAME':'fake-model'}):
            resolver=APILocationResolver(client_factory=lambda:client)
            selected=resolver.resolve('带我去拿药',CONTEXT)
        self.assertEqual(selected['candidates'],[entry('药房')])
        self.assertEqual(create.calls[0]['response_format'],{'type':'json_object'})
        self.assertIn('trusted_catalog',create.calls[0]['messages'][1]['content'])
        with patch.dict('os.environ',{'MODEL_NAME':'fake-model'}):
            broken=APILocationResolver(client_factory=lambda:SimpleNamespace(chat=SimpleNamespace(completions=FakeCreate('not json'))))
            with self.assertRaises(LocationResolverError):broken.resolve('带我去拿药',CONTEXT)

if __name__=='__main__':unittest.main()
