"""Keep general product-label directions separate from personal dosing advice."""

from __future__ import annotations

from typing import Any
import re

try:
    from .medication_request_scope import positive_intent_text, request_scope
except ImportError:
    from medication_request_scope import positive_intent_text, request_scope

_GENERIC_DIRECTIONS = (
    "通常怎么吃", "一般怎么吃", "说明书怎么吃", "说明书一般怎么吃",
    "说明书用法用量", "说明书一般用法", "标签用法用量",
)
_PERSONAL_DOSING = (
    "我该怎么吃", "我应该怎么吃", "我能吃多少", "我该吃多少",
    "我应该吃多少", "我服用多少", "给我多少剂量",
    "患者应该吃多少", "患者该吃多少", "患者应该怎么吃",
)


def explicit_medication_risk(raw_text: str) -> tuple[str, str, str] | None:
    """Recognize explicit clinical requests, never trusted treatment facts.

    This conservative backstop complements model classification. Generic label
    questions alone do not establish a personal regimen-change request.
    """
    if request_scope(raw_text) in {"COMMUNICATION_SUPPORT", "EXISTING_INFORMATION_REVIEW"}:
        return None
    raw_text = positive_intent_text(raw_text)
    personal = re.search(r"(?:我|患者|病人|孩子|宝宝|给他|给她)", raw_text) or re.search(
        r"(?:请|帮忙|能否|能不能|可以|要不要).{0,8}(?:停药|换药|加量|减量|改量)|"
        r"(?:停药|换药|加量|减量|改量)(?:吗|行吗|可以吗)", raw_text
    )
    if personal:
        for change, pattern in (
            ("STOP", r"停药|停用|停止(?:吃|服用)|不再(?:吃|服用)"),
            ("SWITCH", r"换药|换成|改用|替换"),
            ("DOSE", r"改量|加量|减量|加倍|减半|增加剂量|减少剂量|调整剂量"),
            ("ADD", r"加药|加一种药|增加一种药"),
        ):
            if re.search(pattern, raw_text):
                return "ALTER_PRESCRIBED_MEDICATION", "change_type", change
        if re.search(r"一起(?:吃|用|服)|合(?:并|用)|同(?:时|服)|搭配", raw_text):
            return "ASK_PERSONAL_MEDICATION_ADVICE", "advice_type", "INTERACTION"
        if re.search(r"适合|能不能(?:吃|用|服)|能(?:吃|用|服)吗|可以(?:吃|用|服).*吗", raw_text):
            return "ASK_PERSONAL_MEDICATION_ADVICE", "advice_type", "SUITABILITY"
    if any(phrase in raw_text for phrase in _PERSONAL_DOSING) or re.search(
        r"(?:我|患者|病人|孩子|宝宝).{0,40}(?:一次|现在|每天|该|应该|需要).{0,12}(?:吃|服用|用).{0,8}(?:多少|几片|几粒|几次|剂量)|"
        r"(?:给我|替我|帮我).{0,12}(?:定|决定|确定|计算).{0,12}(?:剂量|用量)", raw_text
    ):
        return "ASK_PERSONAL_MEDICATION_ADVICE", "advice_type", "DOSE"
    return None


def canonicalize_medication_information_boundary(
    normalized: dict[str, Any], raw_text: str,
) -> dict[str, Any]:
    action = normalized.get("action")
    if not isinstance(action, dict) or action.get("type") != "GET_GENERAL_MEDICATION_INFO":
        return normalized
    parameters = action.get("parameters")
    if not isinstance(parameters, dict):
        return normalized
    risk = explicit_medication_risk(raw_text)
    if risk:
        action["type"] = risk[0]
        action["parameters"] = {risk[1]: risk[2]}
        return normalized
    if any(phrase in raw_text for phrase in _GENERIC_DIRECTIONS):
        parameters["info_type"] = "DIRECTIONS"
    return normalized
