"""Separate a requested clinical decision from communication and record review."""
from __future__ import annotations

import re
from typing import Any

PERSONAL = r"(?:我|本人|自己|孩子|宝宝|爸爸|妈妈|父亲|母亲|患者|病人|孕妇|怀孕)"
DOSE = r"(?:剂量|药量|用量|吃多少|服用多少|几片|几粒|多少毫克|上限)"


def positive_intent_text(text: str) -> str:
    """Remove standalone disclaimers, retaining an accompanying positive request."""
    clauses = re.split(r"[，。；？！\n]|但|不过|然而|顺便|然后", text)
    active = []
    for clause in clauses:
        outside_quotes = re.sub(r'“[^”]*”|"[^"]*"|「[^」]*」', '', clause)
        excluded = re.search(r'不是问|不问|不让你|^\s*(?:不要|别)', outside_quotes)
        positive = re.search(r'批准|请|告诉我|替我|帮我|你先|只要', outside_quotes)
        if not excluded or positive:
            active.append(clause)
    return '，'.join(active)


def decision_advice(text: str) -> str | None:
    """Recognize the answer being requested, not an intensifier such as 'direct'."""
    text = positive_intent_text(text)
    communication = re.search(r"整理|汇总|归纳|准备|列出|列个|复述", text) and re.search(r"医生|药师", text)
    actual_answer = re.search(r"(?:你|系统|替我|帮我).{0,8}(?:决定|批准|算出|选药|定.{0,8}(?:剂量|吃多少))|"
                              r"(?:告诉我|给我|替我)(?![^，。]*(?:为什么|为何|原理|哪些因素)).{0,16}(?:能不能吃|该吃|应吃|吃多少|几片|剂量|药量)|"
                              r"(?:给个|估个).{0,16}(?:剂量|药量)|照着办|照做", text)
    if communication and not actual_answer:
        return None
    review = re.search(r"(?:核对|核验|验证|比对|确认).{0,24}(?:医嘱|处方|药袋|说明书|记录|资料|纸条)", text)
    if review and not actual_answer and not re.search(r"我该|我应该|我能|吃多少|按哪个|取.{0,6}上限", text):
        return None
    # A principle question describes clinicians' reasoning, not a delegated dose.
    if re.search(r"医生|医师|药师", text) and re.search(r"为什么|为何|原理|一般考虑|哪些因素|如何(?:考虑|确定|决定)", text) and not actual_answer:
        return None
    # Explicit delegation of a clinical answer overrides a materials request.
    if (re.search(r"(?:告诉我|确认|决定|判断|批准|给(?:我|个|出)|定|算|估).{0,24}" + DOSE, text)
            and (re.search(PERSONAL, text) or actual_answer or re.search(r"给个|估个", text))):
        return "DOSE"
    if re.search(r"(?:照着办|照做|照着吃|按你说的吃)", text) and re.search(
        r"(?:今晚|今天|每天|一次).{0,12}(?:吃|服).{0,8}(?:片|粒|毫克)", text
    ):
        return "DOSE"
    if re.search(PERSONAL, text):
        if re.search(r"(?:按哪个|按哪份|遵照哪个|取.{0,6}上限).{0,12}(?:吃|服)?", text):
            return "DOSE"
        if re.search(PERSONAL + r".{0,35}(?:该|应该|需要|能|可以).{0,8}(?:吃|服用|用).{0,8}(?:多少|几片|几粒|几次)", text):
            return "DOSE"
        if re.search(r"(?:吃|服用|选|用).{0,24}还是.{1,24}", text):
            return "SUITABILITY"
        if re.search(r"(?:能不能|能否|可否|是否|确认|决定).{0,20}(?:加|合用|一起吃|一起用|同服)|(?:加|合用).{0,8}(?:吗|行不行)", text):
            return "INTERACTION"
        if re.search(r"(?:能不能|能否|可否|是否|适合|确认|告诉我).{0,24}(?:吃|服用|使用|用药)|能吃|能用|可以吃|可以服", text):
            return "SUITABILITY"
    return None


def request_scope(text: str, *, personal_action: bool = False) -> str:
    text = positive_intent_text(text)
    if decision_advice(text) is not None:
        return "PERSONAL_DECISION"
    if re.search(r"整理|汇总|归纳|准备|列出|列个|复述", text) and re.search(r"医生|药师", text):
        return "COMMUNICATION_SUPPORT"
    if re.search(r"核对|核验|验证|比对|确认|检查.{0,6}(?:差异|冲突)", text) and re.search(r"医嘱|处方|药袋|说明书|记录|资料|纸条", text):
        return "EXISTING_INFORMATION_REVIEW"
    # A model's personal advice classification contributes semantic evidence,
    # but a bare/unclear request is not automatically a requested decision.
    if personal_action and re.search(PERSONAL, text) and re.search(
        r"(?:该|应该|能不能|能否|可否|可以|适合).{0,12}(?:吃|服|用)|(?:给|定|估).{0,16}" + DOSE + r"|一起吃|合用|取上限", text
    ):
        return "PERSONAL_DECISION"
    return "UNKNOWN"


def personal_parameters(advice: str, text: str) -> dict[str, str]:
    return {"advice_type": advice, "request_scope": request_scope(text, personal_action=True)}


def canonicalize_personal_request(normalized: dict[str, Any], text: str) -> dict[str, Any]:
    action = normalized.get("action")
    if not isinstance(action, dict):
        return normalized
    advice = decision_advice(text)
    if action.get("type") in {"GET_GENERAL_MEDICATION_INFO", "EXPLAIN_MEDICATION_CONCEPT"} and advice:
        action["type"] = "ASK_PERSONAL_MEDICATION_ADVICE"
        action["parameters"] = personal_parameters(advice, text)
    if action.get("type") == "ASK_PERSONAL_MEDICATION_ADVICE":
        # The scope is intent only; user statements never update trusted facts.
        parameters = action.get("parameters")
        if isinstance(parameters, dict):
            scope = request_scope(text, personal_action=True)
            # The model extracts semantic intent for wording outside the narrow
            # deterministic recognizer. It cannot supply verified clinical facts.
            if scope != "UNKNOWN" or parameters.get("request_scope") != "PERSONAL_DECISION":
                parameters["request_scope"] = scope
    return normalized
