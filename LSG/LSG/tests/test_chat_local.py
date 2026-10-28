"""Single-terminal loop tests with a fake resident application."""
import io
import unittest
from contextlib import redirect_stdout
from LSG.scripts.chat_lsg_local import run_loop


def result(decision, text=None, followups=None):
    return {'native_result': {'aggregate_decision': decision, 'session_id': 'lsg-session-fake',
            'dispatch_plan': {'items': [], 'followups': followups or []},
            'per_intent_predictions': [], 'response_results': [],
            'final_response_text': text, 'fail_closed_reason': None}}


class FakeApp:
    def __init__(self, first, continued=None):
        self.first=first;self.continued=continued;self.evaluations=[];self.continuations=[]
    def evaluate(self, text, **kwargs):
        self.evaluations.append((text,kwargs));return self.first
    def continue_session(self, session_id, supplements, **kwargs):
        self.continuations.append((session_id,supplements,kwargs));return self.continued


class LocalChatTests(unittest.TestCase):
    def test_multiple_requests_reuse_one_app(self):
        app=FakeApp(result('AUTHORIZE','回答'))
        answers=iter(['第一句','第二句','/退出'])
        out=io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(run_loop(app,read=lambda prompt:next(answers)),0)
        self.assertEqual([x[0] for x in app.evaluations],['第一句','第二句'])
        self.assertEqual(out.getvalue().count('回答'),2)

    def test_defer_continues_then_next_request(self):
        pending=[{'type':'NEEDS_USER_INPUT','intent_index':1,'message':'请补充'}]
        app=FakeApp(result('DEFER',followups=pending),result('AUTHORIZE','补充后回答'))
        answers=iter(['初始请求','眼睛痒两天','/退出'])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run_loop(app,read=lambda prompt:next(answers)),0)
        self.assertEqual(app.continuations[0][0],'lsg-session-fake')
        self.assertEqual(app.continuations[0][1],{1:'眼睛痒两天'})

    def test_cancel_abandons_pending_and_keeps_process(self):
        pending=[{'type':'NEEDS_USER_INPUT','intent_index':1,'message':'请补充'}]
        app=FakeApp(result('DEFER',followups=pending))
        answers=iter(['初始请求','/取消','/退出'])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run_loop(app,read=lambda prompt:next(answers)),0)
        self.assertEqual(len(app.evaluations),1)
        self.assertEqual(app.continuations,[])


if __name__=='__main__':unittest.main()
