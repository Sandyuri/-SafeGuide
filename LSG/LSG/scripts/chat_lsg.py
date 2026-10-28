#!/usr/bin/env python3
"""One natural-language conversation over the resident LSG service."""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
from LSG.scripts.run_lsg import human_result

EXIT_WORDS = {'/退出', '退出', '/取消', '取消', '放弃本轮', '/放弃本轮'}


def post_json(base_url, route, payload):
    request = urllib.request.Request(
        base_url.rstrip('/') + route,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json; charset=utf-8'},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.load(error)
            reason = detail.get('reason') or detail.get('error') or str(error.code)
        except (ValueError, UnicodeDecodeError):
            reason = str(error.code)
        raise RuntimeError('LSG 服务请求失败：' + reason) from None
    except urllib.error.URLError:
        raise RuntimeError('无法连接 LSG 服务；请先在另一个终端启动 serve_lsg.py。') from None


def read_input(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def run_conversation(base_url, initial_text, output=None):
    text = initial_text
    while not text:
        text = read_input('请输入自然语言请求：')
        if text is None or text in EXIT_WORDS:
            return 0
    if text in EXIT_WORDS:
        return 0
    payload = {'text': text, 'session_id': None, 'request_id': None,
               'trusted_context': None, 'dispatch': True}
    rounds = []
    response = post_json(base_url, '/evaluate', payload)
    while True:
        native = response['native_result']
        rounds.append(response)
        print(human_result(native), flush=True)
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in rounds), encoding='utf-8')
        pending = [item for item in native['dispatch_plan']['followups']
                   if item.get('type') == 'NEEDS_USER_INPUT' and isinstance(item.get('intent_index'), int)]
        if native['aggregate_decision'] != 'DEFER' or not pending:
            return 0
        supplements = {}
        for item in pending:
            index = item['intent_index']
            while True:
                answer = read_input(f'请补充意图{index}（输入 /取消 放弃本轮）：')
                if answer is None or answer in EXIT_WORDS:
                    return 0
                if answer:
                    supplements[str(index)] = answer
                    break
        response = post_json(base_url, '/continue', {
            'session_id': native['session_id'], 'supplements': supplements,
            'request_id': None, 'trusted_context': None, 'dispatch': True,
        })


def main():
    parser = argparse.ArgumentParser(description='通过常驻 LSG 服务处理一项自然语言请求')
    parser.add_argument('--url', default='http://127.0.0.1:8765', help='本地 LSG 服务地址')
    parser.add_argument('--text', help='直接提交请求；省略时在终端输入')
    parser.add_argument('--output', type=Path, help='将每轮完整合同保存为 JSONL')
    args = parser.parse_args()
    try:
        return run_conversation(args.url, args.text, args.output)
    except (RuntimeError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
