"""Deterministic candidate and action mapping for LSG native contracts."""
import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from jsonschema import Draft202012Validator
from .config import sha
from .medication_term import extract_leaflet_term


@lru_cache(maxsize=1)
def load_department_catalog():
    catalog_path = Path(__file__).resolve().parents[1] / 'response/department_catalog.json'
    return json.loads(catalog_path.read_text(encoding='utf-8'))

def official_candidates(text, context):
    catalog_path = Path(__file__).resolve().parents[1] / 'response/department_catalog.json'
    catalog = load_department_catalog()
    names = {name for group in catalog['categories'].values() for name in group}
    # The frozen candidate-aware Gate was evaluated with the full official
    # department catalog for department guidance. A user asking '挂什么科' does
    # not know the department name yet, so text-only name matching is invalid.
    department_request = bool(re.search(r'挂.{0,10}科|什么科|哪个科|哪科|科室|导诊', text))
    departments = sorted(names) if department_request or any(name in text for name in names) else []
    aliases = (context.get('reference_resolution') or {}).get('location_aliases') or {}
    if not isinstance(aliases, dict): raise ValueError('invalid_trusted_location_allowlist')
    matched = []
    for alias, location in aliases.items():
        if not isinstance(alias, str) or not isinstance(location, str) or not alias:
            continue
        offsets = [match.start() for match in re.finditer(re.escape(alias), text)]
        # An exact place introduced by 从/由 is an origin, not a candidate
        # navigation target. Retain it only if it also occurs as a target.
        target_occurrence = any(
            not re.search(r"[从由]\s*$", text[max(0, offset - 4):offset])
            for offset in offsets
        )
        if target_occurrence:
            matched.append((alias, location))
    # A longer exact catalog name wins over aliases contained inside it.
    # Independent place names remain separate candidates and fail closed.
    matched = [(alias, location) for alias, location in matched
               if not any(alias != longer and alias in longer for longer, _ in matched)]
    locations = [{'alias': alias, 'location_id': location} for alias, location in matched]
    return {'department_catalog':{'applicable':bool(departments),'candidates':departments,'catalog_id':catalog['hospital'],'sha256':sha(catalog_path)},
            'trusted_locations':{'applicable':bool(locations),'candidates':locations,'context_id':context['context_id']}}


def verify_candidates(candidates, text, context, *, location_resolution=None):
    canonical = official_candidates(text, context)
    if candidates['department_catalog'] != canonical['department_catalog']:
        raise ValueError('candidate_out_of_bounds')
    if location_resolution is None:
        if candidates['trusted_locations'] != canonical['trusted_locations']:
            raise ValueError('candidate_out_of_bounds')
    else:
        from .location_resolver import validate_resolution
        verified = validate_resolution(location_resolution, context)
        expected = {'applicable': bool(verified['candidates']),
                    'candidates': verified['candidates'], 'context_id': context['context_id']}
        if candidates['trusted_locations'] != expected:
            raise ValueError('candidate_out_of_bounds')
    return candidates


def general_medication_mode(text):
    # A product-specific instruction cannot be inferred from an unverified name.
    return ('PRODUCT_INSTRUCTIONS_UNVERIFIED'
            if re.search(r'冲泡|冲药|水温|多少度|怎么服|如何服|用法|服用方式|使用方法|剂量|每次|一次|几袋|几粒', text)
            else 'PUBLIC_GENERAL_INFORMATION')


def personal_medication_decision_requested(text):
    return bool(re.search(
        r'(?:我|您|孩子|老人|家人).{0,20}(?:应该|该|能否|可以|需要).{0,20}(?:吃|服|用|冲)'
        r'|(?:帮我|替我|代我).{0,12}(?:决定|安排).{0,12}(?:用药|吃药|服药|剂量)'
        r'|(?:一次|每次|每天|每日).{0,8}(?:多少|几).{0,5}(?:片|粒|袋|毫克|mg|次)'
        r'|(?:可以|能否|能不能|是否|能).{0,8}(?:一起|同时|合用|混用).{0,8}(?:吃|服|用)?'
        r'|(?:一起|同时|合用|混用).{0,8}(?:吃|服|用).{0,8}(?:可以|吗|么)?',
        text, re.I))


def general_medication_question_action(text):
    if personal_medication_decision_requested(text):
        raise ValueError('personal_medication_decision_unsupported')
    return {'type':'GET_GENERAL_MEDICATION_QUESTION','target':None,'resource':None,
            'parameters':{'answer_mode':general_medication_mode(text),
                          'product_identity_status':'UNVERIFIED'}}


def action_for(text, prediction, context, candidates):
    from .reference.medication_knowledge import medication_scope, KNOWLEDGE
    branch = prediction['response_branch']
    if prediction['task_type'] == 'ROBOT_TASK':
        items = candidates['trusted_locations']['candidates']
        if len(items) != 1: raise ValueError('robot_target_not_unique_or_allowlisted')
        item = items[0]
        return {'type':'NAVIGATE_TO_LOCATION','target':{'kind':'LOCATION','id':item['location_id']},
                'resource':{'kind':'LOCATION','id':item['alias']},'parameters':{}}
    if branch == 'CARE_GUIDANCE':
        if re.search('挂号|签到|候诊',text): business='REGISTRATION'
        elif re.search('缴费|支付|收费',text): business='PAYMENT'
        elif re.search('取药|药房',text): business='MEDICATION_COLLECTION'
        elif re.search('入院',text): business='ADMISSION'
        elif re.search('出院',text): business='DISCHARGE'
        elif re.search('检查准备|禁食',text): business='EXAM_PREPARATION'
        elif re.search('查询|状态',text): business='STATUS_CHANNELS'
        else: raise ValueError('care_business_unsupported')
        return {'type':'GET_GENERAL_CARE_GUIDANCE','target':None,'resource':None,
                'parameters':{'business':business,'information_status':'COMPLETE'}}
    if branch == 'MEDICATION_INFORMATION':
        if personal_medication_decision_requested(text):
            raise ValueError('personal_medication_decision_unsupported')
        scope = medication_scope(text)
        if scope and scope[0] == KNOWLEDGE and scope[1].get('concept') != 'UNKNOWN':
            return {'type':KNOWLEDGE,'target':None,'resource':None,'parameters':scope[1]}
        term = extract_leaflet_term(text)
        if term and (scope is None or scope[0] in (KNOWLEDGE, 'GET_GENERAL_MEDICATION_INFO')):
            return {'type':'EXPLAIN_MEDICATION_TERM','target':None,'resource':None,
                    'parameters':{'term':term,'answer_scope':'GENERAL_TERM_ONLY'}}
        if scope is not None:
            raise ValueError('medication_scope_unsupported')
        # A recognized generic ingredient still does not identify the user's
        # product, formulation, or leaflet instructions.
        if general_medication_mode(text) == 'PRODUCT_INSTRUCTIONS_UNVERIFIED':
            return general_medication_question_action(text)
        names = [name for name in ('布洛芬','西替利嗪','对乙酰氨基酚') if name in text]
        if len(names) != 1:
            # Gate authorized a general medication question, but the runtime
            # has not verified a unique product. Keep that uncertainty in the
            # action instead of turning an adapter vocabulary gap into DEFER.
            return general_medication_question_action(text)
        kind = ('USES' if re.search(r'用途|适应症|用于.{0,8}(?:治疗|缓解)|(?:治疗|治)(?:什么|哪些)|有什么作用', text)
                else 'DIRECTIONS' if re.search(r'用法|怎么服|如何服|服用方式|冲泡|多少度|水温', text)
                else 'PRECAUTIONS')
        return {'type':'GET_GENERAL_MEDICATION_INFO','target':None,'resource':{'kind':'MEDICATION','id':names[0]},
                'parameters':{'info_type':kind,'medication_recognition_status':'KNOWN'}}
    if branch == 'DEPARTMENT_GUIDANCE':
        if not re.search('科|挂号|门诊',text) or not re.search('痒|痛|疼|麻|咳|晕|僵硬|不舒服|症状|骨折|骨裂|扭伤|外伤',text):
            raise ValueError('department_symptom_unsupported')
        return {'type':'RECOMMEND_DEPARTMENT','target':None,'resource':None,
                'parameters':{'symptoms':[text],'symptom_information_status':'COMPLETE','department_disambiguation_status':'COMPLETE'}}
    if branch == 'PUBLIC_CARE_INFORMATION':
        outside_hospital = bool(re.search(r'院外|医院以外|除了医院|除.{0,4}医院.{0,2}外', text))
        purchase_question = bool(re.search(r'正规.{0,8}(?:药房|药店|药品|的药)|(?:哪里|哪儿|何处).{0,10}(?:购|买).{0,8}药|(?:药|药品).{0,8}(?:哪里|哪儿).{0,5}买', text))
        hospital_specific = bool(re.search(r'本院|院内|这家医院|医院里|医院内|医院哪里', text))
        if outside_hospital or (purchase_question and not hospital_specific):
            topic = 'general_drug_purchase'
        else:
            # Gate owns the branch decision. The adapter only selects the
            # permitted evidence source; it must not apply a second, finite
            # keyword allow-list to an already-authorized public-care request.
            topic = 'public_hospital_service'
        return {'type':'GET_PUBLIC_CARE_INFORMATION','target':None,'resource':None,'parameters':{'topic':topic}}
    raise ValueError('unsupported_response_branch')


