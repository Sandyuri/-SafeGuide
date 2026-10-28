"""Extract a source-grounded, product-independent leaflet term.

This helper selects a narrow answer scope. It never identifies a product or
approves a personal medication decision.
"""
import re

_LABEL = re.compile(r'(?:药品)?说明书.{0,6}?(?:写着|标注|注明|写有)\s*[“"「]?')
_STOP = re.compile(r'[，。；;！？?：:“”"「」]|是什么意思|什么意思|怎么理解|应该|怎么|如何|能否|是否|可以|要不要')
_UNSAFE = re.compile(r'\d|[一二三四五六七八九十]+(?:片|粒|袋|毫克)|剂量|用量|频次|每天|每日|每次|一次|停药|换药|加量|减量|合用')
_PERSONAL = re.compile(r'(?:我|患者|孩子|老人|家人).{0,12}(?:应该|该|能否|可以|需要).{0,12}(?:吃|服|用)|(?:帮我|替我|代我).{0,12}(?:决定|安排)')


def extract_leaflet_term(text: str) -> str | None:
    if not isinstance(text, str) or not text.strip() or _PERSONAL.search(text):
        return None
    match = _LABEL.search(text)
    if match is None:
        return None
    tail = text[match.end():]
    term = _STOP.split(tail, maxsplit=1)[0].strip('“”"「」 、\t\n')
    if not 1 <= len(term) <= 12 or _UNSAFE.search(term):
        return None
    return term
