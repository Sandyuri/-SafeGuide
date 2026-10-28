"""Closed, product-independent medication concepts and intent boundaries."""
from __future__ import annotations

import re
from typing import Any

try:
    from .medication_information_normalization import explicit_medication_risk
    from .medication_request_scope import decision_advice, personal_parameters
except ImportError:  # Script-style entry points.
    from medication_information_normalization import explicit_medication_risk
    from medication_request_scope import decision_advice, personal_parameters

KNOWLEDGE = "EXPLAIN_MEDICATION_CONCEPT"
CONCEPTS = {
    "WHOLE_TABLET": "“整片吞服”表示保持药片完整后吞下；不是先掰开、嚼碎或碾碎。缓释制剂旨在逐步释放药物，破坏结构可能改变释放方式。这里仅解释术语，不确认任何手中产品可以怎样处理；实际操作须核对具体产品说明书并咨询药师。",
    "AFTER_MEAL": "“饭后服用”通常表示在进餐之后服药。仅凭这个词不能推导所有药都适用的固定间隔，也不能确定某个产品的剂量、频次或是否适合您；具体要求以该产品说明书和医嘱为准。",
    "WARM_WATER": "“温开水冲服”通常指按说明书用温开水冲调后口服。这个词本身没有给出水量、具体温度、药量或频次；这里不为未知产品补充这些要求，也不批准改变制剂的处理方式。",
    "EXTENDED_RELEASE": "“缓释”指制剂设计为让药物逐步释放。它是剂型概念，不代表某个产品可掰开、碾碎或替换；能否这样处理须核对具体产品说明书并咨询药师。",
    "RENAL_DOSE_PRINCIPLE": "部分药物或其代谢产物经肾脏清除。肾功能下降时，清除可能变慢，药物蓄积和不良反应风险可能增加，因此医生可能调整用药。不同药物并不相同，不能据此决定某位老人的具体剂量或自行减量；需要医生结合具体药物和检查结果判断。",
    "ANTIBIOTIC_SWITCH_RISK": "不同抗菌药的适用感染、抗菌范围、风险及相互作用可能不同，随意更换可能影响治疗并增加不良反应或耐药风险。这是一般风险解释，不判断某人应使用或换成哪种药；具体调整应由医生评估。",
    "ABSORPTION_SEPARATION_PRINCIPLE": "有些药物与其他药物或补充剂同时服用时，可能相互影响吸收。例如钙制剂可能影响左甲状腺素的吸收，因此资料可能提示分开服用。这里仅解释相互作用原理，不为您确定服药顺序或间隔；具体安排须核对产品说明书并咨询医生或药师。",
    "DUPLICATE_INGREDIENT_RISK": "不同名称或用途的产品可能含有相同有效成分，重复使用可能叠加暴露量并增加不良反应风险。对乙酰氨基酚过量可能造成肝损伤。这里不确认任何手中产品的成分，也不决定哪些产品可以合用或给出个人剂量；应核对具体产品成分并请药师检查。",
    "GLUCOCORTICOID_WITHDRAWAL_PRINCIPLE": "泼尼松等糖皮质激素在某些使用情况下会影响人体自身激素调节，突然停用可能引起不适，也可能使原有疾病反复。风险与具体药物、用药时间及情况有关。这只是一般原理，不判断您是否可以停药，也不提供减量或停药方案；具体调整应咨询开药医生。",
    "PRODUCT_VERIFICATION_PRINCIPLE": "药名、有效成分、剂型和规格决定产品是什么以及资料是否适用于它。同名或外观相似的产品可能并不相同，核对这些信息有助于避免拿错、重复或误用。这里仅解释核对原则，不确认手中药品身份、资料可靠性或产品可互换性。",
    "DOSE_ADJUSTMENT_FACTORS": "医生调整药量可能考虑治疗目标、疗效、不良反应、年龄、肝肾功能、其他用药以及检查结果等。仅凭药片数量变化不能知道某人的具体调整原因，也不能据此判断调整是否合适。这里不代替医生决定剂量或修改方案；个体原因应向开药医生确认。",
    "PRESCRIPTION_MEDICINE": "“处方药”指需要凭医生处方，并按相应审核要求获取和使用的药品。这是管理类别，不表示它必然更有效或适合某个人，也不代表仅凭药名或用户自述就已核验处方。是否适用及具体用法应由医生或药师结合实际情况判断。",
}
CONCEPT_OBLIGATIONS = ["LIMIT_TO_MEDICATION_CONCEPT", "DO_NOT_PERSONALIZE_DOSING", "DO_NOT_INFER_PRODUCT_INSTRUCTIONS"]


def concept_in_text(text: str) -> str:
    for concept, pattern in (
        ("WHOLE_TABLET", r"整片吞服"),
        ("AFTER_MEAL", r"饭后(?:服用|吃|服药)"),
        ("WARM_WATER", r"温开水冲服"),
        ("RENAL_DOSE_PRINCIPLE", r"肾功能|肾脏.{0,8}(?:清除|药量|剂量)"),
        ("ANTIBIOTIC_SWITCH_RISK", r"(?:抗菌药|抗生素).{0,40}(?:换|替代)"),
        ("ABSORPTION_SEPARATION_PRINCIPLE", r"(?:左甲状腺素|甲状腺素).{0,24}(?:钙|补充剂)|钙.{0,24}甲状腺素|(?:药物|药品).{0,24}(?:吸收|错开|分开)"),
        ("DUPLICATE_INGREDIENT_RISK", r"(?:成分|对乙酰氨基酚).{0,60}(?:重复|叠加|重叠)|(?:重复|叠加).{0,24}(?:成分|对乙酰氨基酚)"),
        ("GLUCOCORTICOID_WITHDRAWAL_PRINCIPLE", r"(?:泼尼松|糖皮质激素|激素药).{0,50}(?:停|撤)|(?:停|撤).{0,24}(?:泼尼松|糖皮质激素|激素药)"),
        ("PRODUCT_VERIFICATION_PRINCIPLE", r"(?:核对|查对|检查|确认).{0,24}(?:药名|规格|剂型|成分)|(?:药名|规格|剂型).{0,24}(?:核对|查对)"),
        ("DOSE_ADJUSTMENT_FACTORS", r"(?:医生|医师).{0,60}(?:减量|调.{0,4}(?:药量|剂量)|药量|剂量|减到|减小)|(?:减量|调整剂量).{0,24}(?:考虑|因素|依据)"),
        ("PRESCRIPTION_MEDICINE", r"(?<!非)处方药"),
        ("EXTENDED_RELEASE", r"缓释(?:片|制剂)?"),
    ):
        if re.search(pattern, text):
            return concept
    return "UNKNOWN"


def medication_scope(text: str, *, inherit_concept: bool = False) -> tuple[str, dict] | None:
    """Classify explicit scope changes; a generic disclaimer never overrides risk."""
    risk = explicit_medication_risk(text)
    if risk and risk[0] == "ALTER_PRESCRIBED_MEDICATION":
        return risk[0], {risk[1]: risk[2]}
    advice = decision_advice(text)
    if advice:
        return "ASK_PERSONAL_MEDICATION_ADVICE", personal_parameters(advice, text)
    if risk:
        return risk[0], personal_parameters(risk[2], text) if risk[0] == "ASK_PERSONAL_MEDICATION_ADVICE" else {risk[1]: risk[2]}
    # Requesting approval for a change is not asking why a clinician previously
    # changed treatment. Match the requested decision, not the historical dose.
    change_request = re.search(r"(?:批准|允许|替我决定|帮我决定).{0,18}(停|换|减|增|加|改)", text)
    if change_request:
        change = {"停": "STOP", "换": "SWITCH", "减": "DOSE", "增": "DOSE", "加": "DOSE", "改": "DOSE"}[change_request[1]]
        return "ALTER_PRESCRIBED_MEDICATION", {"change_type": change}
    if re.search(r"(?:我|患者|孩子|奶奶|爷爷).{0,8}(?:能否|可否|是否适合).{0,5}(?:吃|服|用)", text):
        return "ASK_PERSONAL_MEDICATION_ADVICE", personal_parameters("SUITABILITY", text)
    if re.search(r"(?:批准|允许|替我决定|帮我决定).{0,15}(?:掰|碾|嚼|打开|拆)|"
                 r"(?:我|患者|孩子).{0,12}(?:能否|能不能|可以).{0,8}(?:掰|碾|嚼)|"
                 r"(?:我|患者|孩子).{0,6}(?:要|想要|想|准备|打算)(?:把.{0,8})?(?:掰|碾|嚼)|"
                 r"(?:替我|帮我).{0,8}(?:安排|决定).{0,8}(?:用药|治疗)", text):
        return "REQUEST_TREATMENT_PLAN", {}
    # Strip only standalone disclaimers, never a following positive request.
    clauses = re.split(r"[，。；？！\n]|但|不过|然而|顺便|然后", text)
    identity_text = "，".join(clause for clause in clauses if not (
        re.search(r"不是问|不问|不让你", clause)
        and not re.search(r"批准|允许|替我决定|帮我决定|你先|请", clause)))
    if re.search(r"什么药|无标签|没有标签|没标签|确认.{0,8}(?:药品|药片|身份)|"
                 r"(?:能否|能不能|是否|可以).{0,10}(?:掰开|碾碎|嚼碎|打开胶囊)|"
                 r"(?:这片|这颗|这款药|这个药).{0,12}(?:掰|碾|嚼)", identity_text):
        return "GET_GENERAL_MEDICATION_INFO", {"info_type": "PRECAUTIONS", "medication_recognition_status": "UNKNOWN"}
    # A specific object + a use decision is different from mentioning that
    # object's quoted label. Framing words do not resolve product requirements.
    specific = r"(?:这个药|这(?:片|颗|款|种)药?|手(?:里|中)的药|该产品)"
    if re.search(specific + r".{0,18}(?:要|该|应该|需要|能否|能不能|是否|可以).{0,8}(?:用水|送|吞|嚼|掰|碾|吃|服用|冲|打开)|"
                 + specific + r".{0,12}(?:怎么|如何)(?:吃|用|服|处理|冲)|"
                 + specific + r".{0,24}(?:水|吞|送|嚼).{0,12}还是.{0,12}(?:水|吞|送|嚼)", identity_text):
        return "GET_GENERAL_MEDICATION_INFO", {"info_type": "DIRECTIONS", "medication_recognition_status": "UNKNOWN"}
    explain = bool(re.search(r"解释|什么意思|怎么理解|为什么|为啥|为何|原理|原因|考虑什么|哪些因素|什么依据|知道这个词|用法词|术语|概念", text))
    concept = concept_in_text(text)
    term_intent = bool(re.search(r"用法词|术语|概念|这个词|这句话|词.*(?:意思|解释)", text))
    if inherit_concept and not explain and re.search(r"用途|用法|用量|剂量|规格|通常怎么|一般怎么|适应症|副作用", text):
        info_type = "USES" if re.search(r"用途|适应症", text) else "DIRECTIONS" if re.search(r"用法|用量|剂量|通常怎么|一般怎么", text) else "PRECAUTIONS"
        return "GET_GENERAL_MEDICATION_INFO", {"info_type": info_type, "medication_recognition_status": "UNKNOWN"}
    if (explain and (concept != "UNKNOWN" or term_intent)) or inherit_concept:
        return KNOWLEDGE, {"concept": concept}
    return None


def canonicalize_medication_scope(normalized: dict[str, Any], text: str,
                                  previous: dict[str, Any] | None = None) -> dict[str, Any]:
    action = normalized.get("action")
    if not isinstance(action, dict):
        return normalized
    old = (previous or {}).get("action") or {}
    if action.get("type") not in {KNOWLEDGE, "GET_GENERAL_MEDICATION_INFO"} and old.get("type") != KNOWLEDGE:
        return normalized
    scope = medication_scope(text, inherit_concept=old.get("type") == KNOWLEDGE)
    if scope is None:
        if action.get("type") == KNOWLEDGE:
            # A model-selected concept without supported source evidence is a
            # request ambiguity, not a confirmed concept or a program error.
            scope = (KNOWLEDGE, {"concept": "UNKNOWN"})
        else:
            return normalized
    kind, parameters = scope
    if action.get("type") not in {KNOWLEDGE, "GET_GENERAL_MEDICATION_INFO"}:
        # Never downgrade a model's clinical classification to a concept because
        # a conservative local phrase detector did not recognize its evidence.
        return normalized
    if (kind == "GET_GENERAL_MEDICATION_INFO" and old.get("type") != KNOWLEDGE
            and action.get("type") == "GET_GENERAL_MEDICATION_INFO"):
        return normalized  # Keep existing product recognition and information rules.
    if kind == KNOWLEDGE:
        # No product identity/operation may be smuggled into this narrow action.
        if action.get("target") is not None or action.get("recipient") is not None:
            raise ValueError("medication concept cannot target a person")
        if action.get("resource") is not None:
            raise ValueError("medication concept cannot establish product identity")
    if kind == "GET_GENERAL_MEDICATION_INFO" and old.get("type") == KNOWLEDGE:
        resource = action.get("resource")
        name = resource.get("id") if isinstance(resource, dict) else None
        if (action.get("parameters", {}).get("medication_recognition_status") == "KNOWN"
                and isinstance(name, str) and name.strip() and name.casefold() in text.casefold()):
            parameters = {**parameters, "medication_recognition_status": "KNOWN"}
    action["type"], action["parameters"] = kind, parameters
    # Remove only obsolete scope-specific ambiguities, never trusted gaps.
    obsolete = {("MISSING_MEDICATION_IDENTITY", "action.resource"),
                ("MISSING_MEDICATION_IDENTITY", "action.resource.id")} if kind == KNOWLEDGE else set()
    obsolete.add(("MISSING_MEDICATION_CONCEPT", "action.parameters.concept"))
    normalized["ambiguities"] = [item for item in normalized.get("ambiguities", [])
                                 if (item["code"], item["field"]) not in obsolete]
    if kind == KNOWLEDGE and parameters["concept"] == "UNKNOWN":
        normalized["ambiguities"].append({"code": "MISSING_MEDICATION_CONCEPT", "field": "action.parameters.concept",
                                          "message": "请提供想解释的用药术语原文或具体原理问题。"})
    return normalized
