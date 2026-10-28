"""CPU-only regression for explicit multi-person requests merged by Splitter."""
import json
import unittest
from LSG.runtime.config import PROJECT
from LSG.runtime.pipeline import Pipeline
from LSG.scripts.run_lsg import human_result

CONTEXT=json.loads((PROJECT/'configs/elevator-hall-context.json').read_text())
PROVENANCE={'splitter_checkpoint_sha256':'a'*64,'gate_checkpoint_sha256':'b'*64,
            'splitter_manifest_sha256':'c'*64,'gate_manifest_sha256':'d'*64}

class SingleIntentSplitter:
    def predict(self,text):return '[意图1] '+text

class CountingGate:
    def __init__(self):self.calls=0
    def predict(self,text,candidates):
        self.calls+=1
        return {'decision':'AUTHORIZE','task_type':'RESPONSE_TASK',
                'response_branch':'DEPARTMENT_GUIDANCE','defer_type':None,
                'confidence':{'decision':.99,'task_type':.99,'response_branch':.99},
                'gate_input':text}

class MultiPersonGuardTests(unittest.TestCase):
    def test_merged_people_fail_closed_before_gate_or_response(self):
        gate=CountingGate()
        result=Pipeline(SingleIntentSplitter(),gate,integration_manifest={},
                        model_provenance=PROVENANCE).run(
            '我妈妈有些发烧，我本人眼睛痒两天，分别应该挂什么科',CONTEXT,dispatch=True)
        self.assertEqual(result['aggregate_decision'],'DEFER')
        self.assertIn('splitter_undersegmented_multiple_people',result['fail_closed_reason'])
        self.assertEqual(gate.calls,0)
        self.assertEqual(result['dispatch_plan']['items'],[])
        self.assertEqual(result['response_results'],[])
        self.assertIn('分别输入',human_result(result))

    def test_invalid_multi_intent_split_has_clear_message_and_no_gate_call(self):
        class ExpandedSplitter:
            def predict(self,text):
                return ('[意图1] 先告诉我眼睛痒要挂什么科。 要求：无关内容。\n'
                        '[意图2] 然后带我去挂号。 要求：最多三条。')
        gate=CountingGate()
        result=Pipeline(ExpandedSplitter(),gate,integration_manifest={},
                        model_provenance=PROVENANCE).run(
            '先告诉我眼睛痒要挂什么科，然后带我去挂号',CONTEXT,dispatch=True)
        self.assertEqual(result['aggregate_decision'],'DEFER')
        self.assertTrue(any(code in result['fail_closed_reason'] for code in
                            ('splitter_unfounded_expansion','splitter_source_clause_omitted',
                             'splitter_unverified_prompt_echo')))
        self.assertEqual(gate.calls,0)
        self.assertEqual(result['dispatch_plan']['items'],[])
        self.assertIn('分别输入',human_result(result))

    def test_single_person_department_question_not_blocked(self):
        gate=CountingGate()
        result=Pipeline(SingleIntentSplitter(),gate,integration_manifest={},
                        model_provenance=PROVENANCE).run(
            '我本人眼睛痒两天，应该挂什么科',CONTEXT,dispatch=False)
        self.assertEqual(result['aggregate_decision'],'AUTHORIZE')
        self.assertEqual(gate.calls,1)
        self.assertEqual(result['dispatch_plan']['items'][0]['route'],'Response')

if __name__=='__main__':unittest.main()
