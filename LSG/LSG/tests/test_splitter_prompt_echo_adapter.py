"""CPU/Fake checks for bounded runtime cleanup of repeated Splitter prompt echoes."""
import json
import unittest
from LSG.runtime.config import PROJECT
from LSG.runtime.models import parse_runtime_split, verify_coverage
from LSG.runtime.pipeline import Pipeline
from LSG.runtime.orchestrator import LSGOrchestrator
from LSG.runtime.session import SessionStore

CONTEXT=json.loads((PROJECT/'configs/elevator-hall-context.json').read_text())
PROVENANCE={'splitter_checkpoint_sha256':'a'*64,'gate_checkpoint_sha256':'b'*64,
            'splitter_manifest_sha256':'c'*64,'gate_manifest_sha256':'d'*64}
TEXT='先告诉我眼睛痒要挂什么科，然后带我去挂号'
RAW=('[意图1] 先告诉我眼睛痒要挂什么科。 要求：先告诉我眼睛痒要挂什么科。 请直接回答。 请直接回答。\n'
     '[意图2] 然后带我去挂号。 要求：然后带我去挂号。 请直接回答。 请直接回答。')

class Splitter:
    def predict(self,text):
        if text==TEXT:return RAW
        return '[意图1] '+text

class Gate:
    def __init__(self):self.calls=[]
    def predict(self,text,candidates):
        self.calls.append(text)
        robot='带我去' in text
        return {'decision':'AUTHORIZE','task_type':'ROBOT_TASK' if robot else 'RESPONSE_TASK',
                'response_branch':None if robot else 'DEPARTMENT_GUIDANCE','defer_type':None,
                'confidence':{'decision':.99,'task_type':.99,'response_branch':.99},'gate_input':text}

class Resolver:
    def resolve(self,text,context):
        aliases=context['reference_resolution']['location_aliases']
        selected={'alias':'挂号收费窗口','location_id':aliases['挂号收费窗口']}
        return {'status':'RESOLVED','candidates':[selected]} if '挂号收费窗口' in text else {
            'status':'AMBIGUOUS','candidates':[selected,{'alias':'自助挂号收费机',
              'location_id':aliases['自助挂号收费机']}]}

class EchoAdapterTests(unittest.TestCase):
    def test_actual_echo_is_trimmed_but_raw_is_audited_and_location_clarified(self):
        intents,mode=parse_runtime_split(TEXT,RAW)
        self.assertEqual(mode,'verified_prompt_echo_removed')
        self.assertEqual(verify_coverage(TEXT,intents),['先告诉我眼睛痒要挂什么科。','然后带我去挂号。'])
        gate=Gate();pipeline=Pipeline(Splitter(),gate,integration_manifest={},
                                     model_provenance=PROVENANCE,location_resolver=Resolver())
        orchestrator=LSGOrchestrator(pipeline,SessionStore())
        first=orchestrator.start(TEXT,CONTEXT,dispatch=True)
        self.assertEqual(first['splitter_raw_output'],RAW)
        self.assertEqual(first['splitter_normalization'],'verified_prompt_echo_removed')
        self.assertEqual(first['aggregate_decision'],'DEFER')
        self.assertEqual(first['fail_closed_reason'],'location_needs_clarification')
        self.assertEqual(gate.calls,[])
        second=orchestrator.resume(first['session_id'],{2:'挂号收费窗口'},CONTEXT,dispatch=False)
        self.assertNotIn('。；',second['raw_input'])
        self.assertEqual(second['splitter_output_source'],'PER_INTENT_REAL_MODEL_CALLS')
        self.assertEqual(len(second['splitter_component_raw_outputs']),2)
        self.assertEqual(second['aggregate_decision'],'AUTHORIZE')
        self.assertEqual([item['route'] for item in second['dispatch_plan']['items']],
                         ['Response','SPINE_TO_RoboGuard_DRY_RUN'])
        self.assertEqual(second['dispatch_plan']['response_fan_in'],[1])
        self.assertEqual(len(gate.calls),2)
        self.assertEqual(second['response_results'],[])

    def test_unverifiable_suffix_and_extra_line_stay_invalid(self):
        with self.assertRaisesRegex(ValueError,'splitter_unverified_prompt_echo'):
            parse_runtime_split(TEXT,'[意图1] 虚构请求。 要求：虚构请求。\n[意图2] 然后带我去挂号。')
        with self.assertRaisesRegex(ValueError,'splitter_protocol'):
            parse_runtime_split(TEXT,'[意图1] 先告诉我眼睛痒要挂什么科。\n额外说明')
        intents,mode=parse_runtime_split('请解释“要求：”这个词','[意图1] 请解释“要求：”这个词')
        self.assertEqual(mode,'none')
        self.assertEqual(len(intents),1)

    def test_verified_polite_prompt_echo_is_trimmed(self):
        text="先告诉我头疼应该挂什么科，再帮我判断现在我适不适合吃布洛芬"
        raw=("[意图1] 先告诉我头疼应该挂什么科。 要求：请先告诉我头疼应该挂什么科。 请先告诉我头疼应该挂什么科。\n"
             "[意图2] 再帮我判断现在我适不适合吃布洛芬。 要求：请再帮我判断现在我适不适合吃布洛芬。")
        intents,mode=parse_runtime_split(text,raw)
        self.assertEqual(mode,"verified_prompt_echo_removed")
        self.assertEqual(intents,["先告诉我头疼应该挂什么科。","再帮我判断现在我适不适合吃布洛芬。"])

if __name__=='__main__':unittest.main()
