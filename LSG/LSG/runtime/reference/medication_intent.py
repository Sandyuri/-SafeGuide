"""Ground semantic answer scopes in request spans, never in clinical facts."""
from __future__ import annotations

import re
from typing import Any

try:
    from .medication_request_scope import decision_advice
    from .medication_information_normalization import explicit_medication_risk
except ImportError:
    from medication_request_scope import decision_advice
    from medication_information_normalization import explicit_medication_risk

EXPLANATION = 'GET_MEDICATION_EXPLANATION'
EMERGENCY = 'GET_MEDICATION_EMERGENCY_GUIDANCE'
SUBSTITUTION = 'REQUEST_CLINICIAN_EVALUATION_SUBSTITUTION'
SAFE_SCOPES = {'KNOWLEDGE', 'COMMUNICATION_PREPARATION', 'PROVIDED_TEXT_EXPLANATION', 'INGREDIENT_COMPARISON'}
COMMON_OBLIGATIONS = ['LIMIT_TO_REQUESTED_EXPLANATION', 'DO_NOT_MAKE_PERSONAL_MEDICATION_DECISION',
                      'DO_NOT_CONFIRM_PRODUCT_IDENTITY', 'DO_NOT_VERIFY_USER_CLAIMS']
EMERGENCY_OBLIGATIONS = ['GIVE_IMMEDIATE_EMERGENCY_HELP_GUIDANCE', 'DO_NOT_DIAGNOSE_OR_PRESCRIBE', 'DO_NOT_CLAIM_NOTIFICATION']
EMERGENCY_TEXT = ('请立即联系急救或当地中毒咨询/急诊服务（中国大陆可拨打120）；儿童误服多片药，即使暂时没有不适也不要等待症状。'
                  '如有嘴唇或咽喉肿胀、呼吸或说话困难，应立即求助。不要等待找齐药名、体重或完整资料后才联系。'
                  '请让身边成人协助，保留药盒和剩余药物供医护人员查看；不要自行催吐或再给药。'
                  '本系统仅提供求助指引，没有实际呼叫或通知医生，也不作诊断或治疗方案。')


def latest_turn(raw: str) -> str:
    return raw.split('\n[用户补充]\n')[-1]


def _one_edit(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    small, large = sorted((a, b), key=len)
    return any(large[:i] + large[i + 1:] == small for i in range(len(large)))


def grounded_intent(intent: dict, raw: str) -> dict:
    """Full current-turn segmentation prevents silently dropping mixed requests."""
    segments = intent.get('segments')
    if not isinstance(segments, list) or not segments or ''.join(s.get('text', '') for s in segments) != latest_turn(raw):
        raise ValueError('medication intent segments must cover the exact current user turn')
    requests = []
    for segment in segments:
        text, role = segment.get('text'), segment.get('role')
        if not isinstance(text, str) or not text or role not in {'REQUEST', 'BACKGROUND', 'EXCLUDED', 'QUOTED', 'PROVIDED_TEXT'}:
            raise ValueError('invalid medication intent segment')
        if role == 'EXCLUDED' and not re.search(r'不|别|勿|无需|未|非|没有', text):
            raise ValueError('excluded medication request lacks explicit negation')
        outside_quotes = re.sub(r'“[^”]*”|"[^"]*"|「[^」]*」', '', text)
        if role == 'EXCLUDED' and re.search(r'(?:但|不过|然而|顺便|同时|再|请).{0,12}(?:批准|替我决定|帮我决定|定.{0,6}剂量)', outside_quotes):
            raise ValueError('positive decision cannot be hidden in an excluded segment')
        if role == 'QUOTED' and not (text[0] in '“"\'「' and text[-1] in '”"\'」'):
            raise ValueError('quoted medication segment lacks quotation marks')
        if role == 'PROVIDED_TEXT' and not any(m.get('text') == text for m in intent.get('materials', [])):
            raise ValueError('reported question/text must be grounded in provided materials')
        if role == 'BACKGROUND' and re.search(r'(?:你|替我|帮我).{0,8}(?:决定|批准|定.{0,4}剂量|算.{0,4}剂量)|我(?:该|能不能|能否).{0,8}(?:吃|合用)', text):
            raise ValueError('active medication decision cannot be hidden as background')
        if role == 'REQUEST':
            requests.append(text)
    if not requests:
        raise ValueError('medication intent has no requested output evidence')
    if any(re.search(r'照着办|照做|照着吃|按你说的', text) for text in requests):
        # A quote the user asks to follow is an operative requested output,
        # unlike a prohibited or merely explained quoted instruction.
        requests.extend(s['text'] for s in segments if s['role'] in {'QUOTED', 'PROVIDED_TEXT'})
    topic = intent.get('topic_span', '')
    if not isinstance(topic, str) or not topic or topic not in raw:
        # Repair only a topic paraphrase when the model separately supplied a
        # complete, exact current-turn segmentation. Use its actual request.
        topic = next((part['text'] for part in segments
                      if part['role'] == 'REQUEST' and part['text'] in raw), '')
        if not topic:
            raise ValueError('medication topic lacks source span')
        intent['topic_span'] = topic
    for material in intent.get('materials', []):
        text = material.get('text', '')
        if not text or text not in raw:
            raise ValueError('provided medication material lacks user source')
        if any(not ingredient or ingredient not in text for ingredient in material.get('ingredients', [])):
            raise ValueError('ingredient comparison cannot invent ingredients')
    requested_text = '，'.join(requests)
    candidate = intent.get('name_candidate')
    complete = intent.get('information_status') == 'COMPLETE'
    if candidate:
        source, canonical = candidate.get('source', ''), candidate.get('canonical', '')
        if not source or source not in raw or not canonical:
            raise ValueError('medication name candidate lacks source')
        relation = candidate.get('relation', 'EXACT' if source == canonical else 'SPELLING_VARIANT')
        if (relation == 'EXACT' and source != canonical and canonical in source
                and canonical in raw):
            # The model sometimes includes framing words in source. Retain
            # only the exact medicine name that the user actually supplied.
            candidate['source'] = canonical
            source = canonical
        if (candidate.get('confidence') != 'HIGH' or
                (relation == 'EXACT' and source != canonical) or
                (relation == 'SPELLING_VARIANT' and (min(len(source), len(canonical)) < 4 or not _one_edit(source, canonical))) or
                (relation == 'BRAND_GENERIC' and intent['scope'] != 'KNOWLEDGE') or
                (relation == 'BRAND_GENERIC' and
                 re.search(r'药盒|包装|这盒|这款|这瓶|这个药|手里', requested_text) and
                 not re.search(r'成分(?:栏|表|写|是|为)|active ingredient', raw, re.I))):
            complete = False  # A weak guess asks for the name; it is not a product identity.
    if intent['scope'] == 'KNOWLEDGE' and intent.get('knowledge_kind') in {'GENERAL_USES', 'NAME_RELATION'} and not candidate:
        complete = False
    if intent['scope'] == 'INGREDIENT_COMPARISON':
        complete = complete and len(intent.get('materials', [])) >= 2 and all(m.get('ingredients') for m in intent['materials'])
    if intent['scope'] == 'PROVIDED_TEXT_EXPLANATION':
        complete = complete and bool(intent.get('materials'))
    return {'requests': '，'.join(requests), 'complete': complete}


def canonicalize_medication_intent(normalized: dict[str, Any], raw: str) -> dict[str, Any]:
    intent = normalized.get('medication_intent')
    if intent is None:
        return normalized  # Legacy structured inputs remain compatible.
    # Optional medication metadata must not replace a separately classified
    # care, privacy, clinical or physical action. UNKNOWN is not a domain.
    action_type = (normalized.get('action') or {}).get('type')
    medication_actions = {EXPLANATION, EMERGENCY, SUBSTITUTION,
        'GET_GENERAL_MEDICATION_INFO', 'EXPLAIN_MEDICATION_CONCEPT',
        'ASK_PERSONAL_MEDICATION_ADVICE', 'ALTER_PRESCRIBED_MEDICATION', 'UNKNOWN'}
    if action_type == 'GET_MEDICATION_ACCESS_INFO' and intent.get('scope') == 'KNOWLEDGE':
        checked = grounded_intent(intent, raw)
        requested = checked['requests']
        # Access-channel questions may carry redundant medication knowledge
        # metadata. Keep the access action; never let the metadata authorize a
        # separate personal decision under the single-action contract.
        if (decision_advice(requested) or explicit_medication_risk(requested)
                or re.search(r'(?:替我|帮我|为我).{0,8}(?:选药|决定|定剂量)', requested)):
            raise ValueError('medication intent conflicts with established access action')
        return normalized
    if action_type not in medication_actions:
        if intent.get('scope') != 'UNKNOWN' and action_type not in {
                'REQUEST_DIAGNOSIS', 'REQUEST_TREATMENT_PLAN', 'INTERPRET_PERSONAL_TEST_RESULT'}:
            raise ValueError('medication intent conflicts with established non-medication action')
        normalized.pop('medication_intent')
        return normalized
    checked = grounded_intent(intent, raw)
    requested = checked['requests']
    scope = intent['scope']
    action = normalized.get('action') or {'target': None, 'resource': None, 'parameters': {}}
    strong_decision = re.search(
        r'(?:你|替我|帮我|为我).{0,12}(?:决定|批准|定.{0,8}(?:剂量|吃多少)|算.{0,8}(?:剂量|吃多少)|选药)|'
        r'(?:我|本人|患者|孩子).{0,16}(?:能不能|能否|该|应该|按哪个|取.{0,4}上限).{0,8}(?:吃|服|合用|选)|'
        r'(?:告诉我|回答我).{0,10}(?:能不能吃|能否服|我能否|我该|是否可以合用)|'
        r'(?:给个|估个).{0,12}(?:剂量|药量)|照着办|照做|按你说的', requested)
    advice = decision_advice(requested) if scope == 'PERSONAL_DECISION' or strong_decision else None
    # These are safety backstops for requested clinical outputs, not classifiers
    # for background histories or an explanation of a risk mechanism.
    risk = explicit_medication_risk(requested) if scope == 'PERSONAL_DECISION' or advice or re.search(
        r'(?:我|患者|孩子).{0,8}(?:想|要|准备|打算).{0,8}(?:换药|停药|加量|减量)|'
        r'(?:替我|帮我|你).{0,12}(?:批准|决定|安排|换药|停药|加量|减量)|'
        r'(?:替我|帮我|你).{0,12}(?:定|算|给).{0,12}(?:剂量|用量|吃多少|几片)', requested) else None
    from_scope = None
    informational = SAFE_SCOPES | {'PRODUCT_OPERATION', 'EMERGENCY_HELP'}
    if scope in SAFE_SCOPES:
        try:
            from .medication_knowledge import medication_scope
        except ImportError:
            from medication_knowledge import medication_scope
        candidate_scope = medication_scope(requested)
        if candidate_scope and candidate_scope[0] == 'GET_GENERAL_MEDICATION_INFO':
            from_scope = candidate_scope
    formal_claim = (re.search(r'医师|医生|正式.{0,4}(?:咨询|评估)', raw)
        and re.search(r'AI|系统|问过你|你的说明|问答', raw, re.I)
        and any(re.search(r'(?:是不是|是否|算不算|能否).{0,16}(?:算|视为|认定|完成)|'
                          r'(?:请|你).{0,12}(?:承认|认定|认可).{0,16}(?:咨询|评估|许可)', s['text'])
                for s in intent['segments'] if s['role'] == 'REQUEST'))
    # Explicit approval survives generic disclaimers and a mistaken safe label.
    if formal_claim:
        action.update(type=SUBSTITUTION, target=None, resource=None, recipient=None, parameters={})
    elif scope in informational and re.search(
        r'(?:诊断|判定|判断|确定).{0,8}(?:我|本人|患者|孩子).{0,12}(?:病|过敏)|'
        r'(?:我|患者|孩子).{0,16}(?:是什么病|是不是过敏|病因是什么)|'
        r'(?:给我|替我|帮我).{0,8}诊断', requested):
        action.update(type='REQUEST_DIAGNOSIS', parameters={})
    elif scope in informational and re.search(r'(?:批准|允许|替我决定|帮我决定).{0,16}(?:碾|掰|嚼|治疗|服药方案)', requested):
        action.update(type='REQUEST_TREATMENT_PLAN', parameters={})
    elif scope in informational and (risk or advice):
        if risk and risk[0] == 'ALTER_PRESCRIBED_MEDICATION':
            action.update(type=risk[0], parameters={risk[1]: risk[2]})
        else:
            action.update(type='ASK_PERSONAL_MEDICATION_ADVICE', parameters={
                'advice_type': advice or risk[2], 'request_scope': 'PERSONAL_DECISION'})
    elif scope == 'PERSONAL_DECISION':
        if risk and risk[0] == 'ALTER_PRESCRIBED_MEDICATION':
            action.update(type=risk[0], parameters={risk[1]: risk[2]})
        elif action.get('type') in {'REQUEST_DIAGNOSIS', 'REQUEST_TREATMENT_PLAN', 'ALTER_PRESCRIBED_MEDICATION'}:
            pass  # Retain stricter established clinical action semantics.
        else:
            action.update(type='ASK_PERSONAL_MEDICATION_ADVICE', parameters={
                'advice_type': intent.get('advice_type', advice or (risk[2] if risk else 'SUITABILITY')), 'request_scope': 'PERSONAL_DECISION'})
    elif from_scope is not None:
        action.update(type=from_scope[0], parameters=from_scope[1])
    elif scope in SAFE_SCOPES:
        action.update(type=EXPLANATION, target=None, resource=None, recipient=None, parameters={
            'answer_scope': scope, 'topic': intent['topic_span'],
            'knowledge_kind': intent.get('knowledge_kind') if scope == 'KNOWLEDGE' else None,
            'information_status': 'COMPLETE' if checked['complete'] else 'INCOMPLETE',
            'materials': intent.get('materials', []), 'name_candidate': intent.get('name_candidate')})
    elif scope == 'EMERGENCY_HELP':
        action.update(type=EMERGENCY, target=None, resource=None, recipient=None, parameters={})
    elif scope == 'CLINICIAN_EVALUATION_SUBSTITUTION':
        action.update(type=SUBSTITUTION, target=None, resource=None, recipient=None, parameters={})
    elif scope == 'TRUSTED_VERIFICATION':
        # Existing trusted verification remains non-authorized; no chat claim
        # can complete a record lookup or upgrade provider facts.
        action.update(type='UNKNOWN', target=None, resource=None, recipient=None, parameters={})
        normalized['ambiguities'] = [a for a in normalized.get('ambiguities', [])
            if a['code'] not in {'MISSING_MEDICATION_IDENTITY', 'MISSING_PATIENT_IDENTITY', 'MISSING_MEDICATION_CONCEPT'}]
    elif scope == 'PRODUCT_OPERATION':
        status = action.get('parameters', {}).get('medication_recognition_status', 'UNKNOWN')
        action.update(type='GET_GENERAL_MEDICATION_INFO', target=None, recipient=None, parameters={
            'info_type': 'DIRECTIONS', 'medication_recognition_status': status})
    elif scope == 'UNKNOWN':
        action.update(type=EXPLANATION, target=None, resource=None, recipient=None, parameters={
            'answer_scope': 'KNOWLEDGE', 'topic': intent['topic_span'], 'information_status': 'INCOMPLETE',
            'knowledge_kind': None, 'materials': [], 'name_candidate': None})
    else:
        raise ValueError('unsupported medication answer scope')
    normalized['action'] = action
    if action['type'] in {EXPLANATION, EMERGENCY, SUBSTITUTION}:
        # Only mistaken request-side references/concept gaps are obsolete.
        # Unrelated permission, claim and trusted-context gaps stay visible.
        obsolete = {'MISSING_MEDICATION_IDENTITY', 'MISSING_PATIENT_IDENTITY', 'MISSING_MEDICATION_CONCEPT', 'MISSING_EXPLANATION_INPUT'}
        normalized['ambiguities'] = [a for a in normalized.get('ambiguities', [])
                                     if not (a['code'] in obsolete and a['field'].startswith('action.'))]
        if action['type'] == EXPLANATION and action['parameters']['information_status'] != 'COMPLETE':
            normalized['ambiguities'].append({'code': 'MISSING_EXPLANATION_INPUT', 'field': 'action.parameters.explanation_input',
                'message': intent.get('missing_information') or {
                    'INGREDIENT_COMPARISON': '请分别提供两款产品明确列出的成分原文；不能仅凭药名推测成分。',
                    'PROVIDED_TEXT_EXPLANATION': '请提供想核对的说明书原文；如涉及两款产品，请分别提供药名及相互作用文字。',
                    'COMMUNICATION_PREPARATION': '您想向医生或药师说明哪种情况？可提供用药、反应和发生时间。',
                }.get(scope, '请提供具体想了解的术语、一般作用或原理问题；药名不确定时请提供完整名称。')})
    return normalized
