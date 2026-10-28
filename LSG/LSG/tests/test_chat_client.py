"""No service or model is started by these client tests."""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from LSG.scripts import chat_lsg


def result(decision, *, text=None, items=None, followups=None, session='lsg-session-fake'):
    return {'native_result': {'aggregate_decision': decision, 'session_id': session,
            'dispatch_plan': {'items': items or [], 'followups': followups or []},
            'per_intent_predictions': [], 'response_results': [],
            'final_response_text': text, 'fail_closed_reason': None}}


class ChatClientTests(unittest.TestCase):
    def test_response_text_only(self):
        out = io.StringIO()
        with patch.object(chat_lsg, 'post_json', return_value=result('AUTHORIZE', text='自然语言回答')), redirect_stdout(out):
            self.assertEqual(chat_lsg.run_conversation('http://localhost', '请求'), 0)
        self.assertEqual(out.getvalue().strip(), '自然语言回答')

    def test_department_reject_message_has_no_medication_warning(self):
        from LSG.scripts.run_lsg import human_result
        rejected=result('REJECT')['native_result']
        rejected['raw_input']='头疼两天，不发烧，应该挂什么科'
        message=human_result(rejected)
        self.assertIn('就诊科室',message)
        self.assertNotIn('用药',message)

    def test_mixed_department_and_medication_reject_names_rejected_intent(self):
        from LSG.scripts.run_lsg import human_result
        rejected=result("REJECT")["native_result"]
        rejected["raw_input"]="头疼该挂什么科室？头疼能吃布洛芬吗？"
        rejected["parsed_intents"]=["头疼该挂什么科室？","头疼能吃布洛芬吗？"]
        rejected["per_intent_predictions"]=[{"decision":"DEFER"},{"decision":"REJECT"}]
        message=human_result(rejected)
        self.assertIn("不能判断",message)
        self.assertIn("药物",message)
        self.assertNotIn("就诊科室",message)

    def test_robot_task_prints_task(self):
        robot = {"route": "SPINE_TO_RoboGuard_DRY_RUN", "intent_index": 1,
                 "authorized_task": {"target": {"id": "elevator-hall"}}}
        out = io.StringIO()
        with patch.object(chat_lsg, "post_json", return_value=result("AUTHORIZE", items=[robot])), redirect_stdout(out):
            chat_lsg.run_conversation("http://localhost", "带我去电梯厅")
        self.assertIn("ROBOT_TASK", out.getvalue())
        self.assertIn("DRY_RUN", out.getvalue())

    def test_defer_continues_same_session(self):
        first = result('DEFER', followups=[{'type': 'NEEDS_USER_INPUT', 'intent_index': 1, 'message': '请补充症状'}])
        second = result('AUTHORIZE', text='补充后回答')
        out = io.StringIO()
        with patch.object(chat_lsg, 'post_json', side_effect=[first, second]) as post, \
             patch.object(chat_lsg, 'read_input', return_value='眼睛不舒服'), redirect_stdout(out):
            chat_lsg.run_conversation('http://localhost', '挂什么科')
        self.assertEqual(post.call_args_list[1].args[1], '/continue')
        self.assertEqual(post.call_args_list[1].args[2]['session_id'], 'lsg-session-fake')
        self.assertEqual(post.call_args_list[1].args[2]['supplements'], {'1': '眼睛不舒服'})
        self.assertIn('补充后回答', out.getvalue())

    def test_cancel_stops_without_continue(self):
        first = result('DEFER', followups=[{'type': 'NEEDS_USER_INPUT', 'intent_index': 1, 'message': '请补充'}])
        with patch.object(chat_lsg, 'post_json', return_value=first) as post, \
             patch.object(chat_lsg, 'read_input', return_value='取消'), redirect_stdout(io.StringIO()):
            self.assertEqual(chat_lsg.run_conversation('http://localhost', '请求'), 0)
        self.assertEqual(post.call_count, 1)


if __name__ == '__main__':
    unittest.main()
