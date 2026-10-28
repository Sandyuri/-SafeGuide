#!/usr/bin/env python3
"""One terminal, one model load, multiple independent LSG conversations."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
from LSG.runtime.application import LSGApplication
from LSG.scripts.run_lsg import human_result
from LSG.scripts.chat_lsg import read_input

EXIT = {'/退出', '退出'}
CANCEL = {'/取消', '取消', '放弃本轮', '/放弃本轮'}


def run_loop(app, *, initial_text=None, output=None, read=read_input):
    """The supplied app stays loaded until /退出 or Ctrl+C."""
    next_text = initial_text
    while True:
        text = next_text if next_text is not None else read('请输入自然语言请求（/退出 结束）：')
        next_text = None
        if text is None or text in EXIT:
            return 0
        if not text or text in CANCEL:
            continue
        response = app.evaluate(text, dispatch=True)
        while True:
            native = response['native_result']
            print(human_result(native), flush=True)
            if output:
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(response, ensure_ascii=False) + '\n')
            pending = [item for item in native['dispatch_plan']['followups']
                       if item.get('type') == 'NEEDS_USER_INPUT' and isinstance(item.get('intent_index'), int)]
            if native['aggregate_decision'] != 'DEFER' or not pending:
                break
            supplements = {}
            cancelled = False
            for item in pending:
                index = item['intent_index']
                while True:
                    answer = read(f'请补充意图{index}（/取消 放弃本轮；/退出 结束）：')
                    if answer is None or answer in EXIT:
                        return 0
                    if answer in CANCEL:
                        cancelled = True
                        break
                    if answer:
                        supplements[index] = answer
                        break
                if cancelled:
                    break
            if cancelled:
                break
            response = app.continue_session(native['session_id'], supplements, dispatch=True)


def main():
    parser = argparse.ArgumentParser(description='LSG 单终端常驻聊天：加载一次模型，连续提问')
    parser.add_argument('--config', default=str(ROOT/'configs/runtime.json'))
    parser.add_argument('--text', help='启动后立即处理的第一条请求')
    parser.add_argument('--output', type=Path, help='可选：逐轮追加完整审计 JSONL')
    args = parser.parse_args()
    from transformers.utils import logging as transformers_logging
    transformers_logging.set_verbosity_error()
    app = LSGApplication(args.config)
    try:
        app.start()
        return run_loop(app, initial_text=args.text, output=args.output)
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        print('LSG 运行失败：' + str(error), file=sys.stderr)
        return 1
    finally:
        app.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
