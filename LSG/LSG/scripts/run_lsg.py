#!/usr/bin/env python3
"""Persistent LSG CLI. One model load serves an entire interactive session."""
import argparse
import json
import sys
import re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
from LSG.runtime.pipeline import Pipeline
from LSG.runtime.orchestrator import LSGOrchestrator
from LSG.runtime.session import SessionStore
from LSG.runtime.config import default_context_path


def human_result(result):
    decision = result['aggregate_decision']
    if decision == "REJECT":
        intents = result.get("parsed_intents") or []
        predictions = result.get("per_intent_predictions") or []
        rejected = [intents[i] for i,p in enumerate(predictions)
                    if i < len(intents) and p.get("decision") == "REJECT"]
        rejected_text = " ".join(rejected) if rejected else (result.get("raw_input") or "")
        if re.search(r"布洛芬|药|用药|吃|服用|剂量", rejected_text):
            return "我不能判断您现在是否适合服用这种药物。请遵循药品说明书和医嘱，或咨询医生、药师。"
        if any(word in rejected_text for word in ("挂什么科", "挂哪个科", "应该挂什么科", "该挂什么科", "科室")):
            return "当前无法提供可靠的就诊科室建议，请咨询医院预检分诊处或现场工作人员。"
        return "这个请求我不能协助完成。"
    if decision == 'DEFER':
        needs_input = [item['message'] for item in result['dispatch_plan']['followups']
                       if item.get('type') == 'NEEDS_USER_INPUT' and item.get('message')]
        if needs_input:
            return '\n'.join(needs_input)
        robot_intents = [i + 1 for i, prediction in enumerate(result.get('per_intent_predictions', []))
                         if prediction.get('decision') == 'AUTHORIZE' and prediction.get('task_type') == 'ROBOT_TASK']
        if robot_intents:
            return json.dumps({'type':'ROBOT_TASK','status':'DEFER',
                               'intent_indices':robot_intents,'authorized_task':None,
                               'handoff_to_spine':False,
                               'reason':'目标地点或可信场景尚未通过 LSG 合同校验'},ensure_ascii=False)
        reason = result.get('fail_closed_reason') or ''
        if 'splitter_undersegmented_multiple_people' in reason:
            return '这涉及两个人的就诊问题，请分别输入每个人的情况，以便逐条进行安全判断和回答。'
        if any(code in reason for code in ('splitter_unfounded_expansion',
                                           'splitter_source_clause_omitted',
                                           'splitter_unverified_prompt_echo',
                                           'splitter_protocol','splitter_line_count')):
            return '这项请求未能可靠拆分，请把两项请求分别输入。'
        if 'robot_target_not_unique_or_allowlisted' in reason:
            return '无法确认目标地点的可信位置，请联系现场工作人员。'
        if reason:
            return '目前无法确认如何安全处理这个请求，请补充具体情况或咨询现场工作人员。'
        questions = [item['message'] for item in result['dispatch_plan']['followups']
                     if item.get('message') and item['message'] != '当前无法处理此类请求。']
        return '\n'.join(questions) if questions else '目前无法确认如何安全处理这个请求，请补充具体情况或咨询现场工作人员。'
    robot_items = [item for item in result['dispatch_plan']['items']
                   if item['route'] == 'SPINE_TO_RoboGuard_DRY_RUN']
    answer = result.get('final_response_text')
    if robot_items:
        tasks = [{'type':'ROBOT_TASK','status':'AUTHORIZED_FOR_PLANNING',
                  'execution':'DRY_RUN','intent_index':item['intent_index'],
                  'authorized_task':item['authorized_task']} for item in robot_items]
        rendered = '\n'.join(json.dumps(task, ensure_ascii=False) for task in tasks)
        return answer + '\n' + rendered if answer else rendered
    if answer:
        return answer
    failures = [entry['response'].get('failure_reason') or entry['response']['status']
                for entry in result['response_results'] if entry['response']['status'] != 'GENERATED']
    if failures:
        return '请求已获授权，但回答暂时无法生成，请稍后重试。'
    return '目前无法生成回答，请稍后重试。'


def _read(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _show(result, args):
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding='utf-8')
    print(rendered if args.json else human_result(result), flush=True)


def _pending(store, session_id):
    record=store.get(session_id)
    return record['pending'] if record and record['state']=='PENDING_USER_INPUT' else []


def main():
    parser = argparse.ArgumentParser(description='LSG: 一次请求及其 DEFER 补充，完成后退出')
    parser.add_argument('--text', help='单次请求；省略时交互输入一次请求')
    parser.add_argument('--context', type=Path, help='可信上下文 JSON；默认使用 configs/runtime.json 配置')
    parser.add_argument('--output', type=Path, help='保存最近一轮完整审计 JSON')
    parser.add_argument('--json', action='store_true', help='在终端显示完整审计 JSON')
    parser.add_argument('--timing', action='store_true', help='兼容旧参数；耗时仅保存于审计输出')
    parser.add_argument('--session-id', help='单次恢复已有 LSG 会话')
    parser.add_argument('--intent-index', type=int, help='单个 pending intent 的编号')
    parser.add_argument('--supplements-json', help='多个 pending intent 的 JSON 映射')
    parser.add_argument('--session-store', type=Path, default=ROOT/'runs/sessions', help='会话状态目录')
    args = parser.parse_args()
    context_path=args.context or default_context_path()
    context=json.loads(context_path.read_text(encoding='utf-8'))
    try:
        from transformers.utils import logging as transformers_logging
    except ModuleNotFoundError:
        # Test doubles and code-only contract checks do not load a transformer
        # backend. A real Pipeline still raises its own dependency error when
        # model construction is attempted.
        transformers_logging = None
    if transformers_logging is not None:
        transformers_logging.set_verbosity_error()
    pipeline=Pipeline()
    store=SessionStore(args.session_store)
    orchestrator=LSGOrchestrator(pipeline,store)
    one_shot=args.text is not None or args.session_id is not None or args.supplements_json is not None
    if one_shot:
        if args.session_id:
            if args.supplements_json:
                supplements={int(k):v for k,v in json.loads(args.supplements_json).items()}
            elif args.intent_index is not None:
                text=args.text if args.text is not None else _read('请输入补充信息：')
                if text is None:return 0
                supplements={args.intent_index:text}
            else:parser.error('继续会话需要 --intent-index 或 --supplements-json')
            result=orchestrator.resume(args.session_id,supplements,context)
        else:
            if not args.text or not args.text.strip():parser.error('请求不能为空')
            result=orchestrator.start(args.text.strip(),context)
        _show(result,args)
        if _pending(store,result['session_id']) and not args.json:
            print('session_id: '+result['session_id'],file=sys.stderr)
        return 0
    active_session=None
    while True:
        pending=_pending(store,active_session) if active_session else []
        if pending:
            supplements={}
            for item in pending:
                index=item['intent_index']
                while True:
                    answer=_read(f'请补充意图{index}（输入 /取消 放弃本轮）：')
                    if answer is None or answer in {'/退出','退出','/取消','取消','放弃本轮','/放弃本轮'}:return 0
                    if answer:
                        supplements[index]=answer
                        break
                if supplements is None:break
            result=orchestrator.resume(active_session,supplements,context)
        else:
            text=_read('请输入自然语言请求：')
            if text is None or text in {'/退出','退出','/取消','取消','放弃本轮','/放弃本轮'}:return 0
            if not text:continue
            result=orchestrator.start(text,context)
        _show(result,args)
        active_session=result['session_id'] if _pending(store,result['session_id']) else None
        if active_session is None:return 0


if __name__ == '__main__':
    raise SystemExit(main())
