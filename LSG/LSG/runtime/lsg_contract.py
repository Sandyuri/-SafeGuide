"""Native LSG authorization. Gate owns A/D/R; this module never changes it."""
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from jsonschema import Draft202012Validator
from .config import PROJECT, paths, sha
from .action_adapter import action_for

class OrchestrationResult(dict):
    """Schema-validated LSG runtime result with dict compatibility."""
    def __init__(self, value):
        validate_schema('lsg-orchestration-result.schema.json', value)
        super().__init__(value)
    def to_dict(self):return dict(self)

VERSION = '0.1'
RULE = 'REJECT > DEFER > AUTHORIZE'
OBLIGATIONS = {
    'GET_GENERAL_CARE_GUIDANCE': ['DO_NOT_VERIFY_USER_CLAIMS','LIMIT_TO_GENERAL_CARE_GUIDANCE'],
    'EXPLAIN_MEDICATION_CONCEPT': ['LIMIT_TO_MEDICATION_CONCEPT','DO_NOT_PERSONALIZE_DOSING','DO_NOT_INFER_PRODUCT_INSTRUCTIONS'],
    'EXPLAIN_MEDICATION_TERM': ['LIMIT_TO_MEDICATION_TERM','DO_NOT_PERSONALIZE_DOSING','DO_NOT_INFER_PRODUCT_INSTRUCTIONS'],
    'GET_GENERAL_MEDICATION_INFO': ['STATE_GENERAL_INFORMATION_SCOPE'],
    'GET_GENERAL_MEDICATION_QUESTION': ['STATE_GENERAL_INFORMATION_SCOPE'],
    'RECOMMEND_DEPARTMENT': ['LIMIT_TO_DEPARTMENT_GUIDANCE'],
    'GET_PUBLIC_CARE_INFORMATION': ['USE_PUBLIC_HOSPITAL_SOURCE'],
    'NAVIGATE_TO_LOCATION': ['REQUIRE_ROBOGUARD_EXECUTION_CHECK'],
}

def obligations_for(action):
    kind=action['type']
    if kind=='GET_PUBLIC_CARE_INFORMATION':
        topic = action.get('parameters', {}).get('topic')
        if topic == 'general_drug_purchase':
            return ['USE_AUTHORITATIVE_PUBLIC_SOURCE']
        if topic != 'public_hospital_service':
            raise ValueError('unsupported_public_care_topic')
    if kind=='GET_GENERAL_MEDICATION_QUESTION':
        mode=action['parameters']['answer_mode']
        return {'PRODUCT_INSTRUCTIONS_UNVERIFIED':['DO_NOT_INFER_PRODUCT_INSTRUCTIONS','DO_NOT_PERSONALIZE_DOSING','ADVISE_CHECK_PRODUCT_LABEL'],
                'PUBLIC_GENERAL_INFORMATION':['STATE_GENERAL_INFORMATION_SCOPE','REQUIRE_OFFICIAL_MEDICATION_SOURCE','DO_NOT_PERSONALIZE_DOSING']}[mode]
    if kind=='GET_GENERAL_MEDICATION_INFO':
        info=action['parameters']['info_type']
        return {'USES':['STATE_GENERAL_INFORMATION_SCOPE'],
                'PRECAUTIONS':['STATE_GENERAL_INFORMATION_SCOPE','ADVISE_CHECK_PRODUCT_LABEL'],
                'DIRECTIONS':['STATE_GENERAL_INFORMATION_SCOPE','DO_NOT_PERSONALIZE_DOSING','ADVISE_CHECK_PRODUCT_LABEL']}[info]
    return OBLIGATIONS[kind]

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def provenance():
    p=paths()
    return {'splitter_checkpoint_sha256': sha(p['splitter_artifact']/'best/adapter_model.safetensors'),
            'gate_checkpoint_sha256': sha(p['gate_artifact']/'model.pt'),
            'splitter_manifest_sha256': sha(p['splitter_artifact']/'manifest.json'),
            'gate_manifest_sha256': sha(p['gate_artifact']/'manifest.json')}

def validate_schema(name, value):
    schema=json.loads((PROJECT/'schemas'/name).read_text(encoding='utf-8'))
    errors=list(Draft202012Validator(schema).iter_errors(value))
    if errors: raise ValueError(name+':'+errors[0].message)
    return value

@dataclass(frozen=True)
class LSGAuthorization:
    schema_version: str
    lsg_version: str
    session_id: str
    request_id: str
    evaluation_id: str
    created_at: str
    intent_index: int
    decision: str
    task_type: str | None
    response_branch: str | None
    defer_type: str | None
    confidence: dict
    authorization_scope: str
    obligations: list[str]
    normalized_request: dict
    evaluated_request: dict
    context_snapshot_id: str
    context_snapshot_hash: str
    runtime_candidate_hash: str
    provenance: dict
    fail_closed_reason: str | None
    aggregate_rule: str
    status: str
    terminal: bool

    def to_dict(self): return asdict(self)

def create_authorization(text, prediction, candidates, context, index, session_id, model_provenance):
    decision=prediction['decision']
    action=action_for(text,prediction,context,candidates) if decision=='AUTHORIZE' else None
    if decision=='AUTHORIZE' and action['type'] not in OBLIGATIONS:
        raise ValueError('unsupported_action_obligations')
    task=prediction['task_type']
    scope='RESPOND_WITHIN_OBLIGATIONS' if task=='RESPONSE_TASK' else 'ALLOW_TO_ENTER_PLANNING' if task=='ROBOT_TASK' else 'NONE'
    status='AUTHORIZED_FOR_RESPONSE' if task=='RESPONSE_TASK' else 'AUTHORIZED_FOR_PLANNING' if task=='ROBOT_TASK' else decision
    request_id='lsg-request-'+str(uuid4())
    request={'request_id':request_id,'raw_text':text,'action':action,'ambiguities':[],
             'safety_control_intent':'NORMAL'}
    item=LSGAuthorization(schema_version='lsg.authorization.v0.1',lsg_version=VERSION,
        session_id=session_id,request_id=request_id,evaluation_id='lsg-evaluation-'+str(uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),intent_index=index,decision=decision,
        task_type=task,response_branch=prediction['response_branch'],defer_type=prediction['defer_type'],
        confidence=prediction['confidence'],authorization_scope=scope,
        obligations=obligations_for(action) if action else [],normalized_request=request,
        evaluated_request=request,context_snapshot_id=context['context_id'],context_snapshot_hash=digest(context),
        runtime_candidate_hash=digest(candidates),provenance=model_provenance,fail_closed_reason=None,
        aggregate_rule=RULE,status=status,terminal=True)
    validate_schema('lsg-authorization.schema.json',item.to_dict())
    return item

def validate_response_authorization(item, action_type=None):
    value=item.to_dict() if hasattr(item,'to_dict') else item
    validate_schema('lsg-authorization.schema.json',value)
    if value['decision']!='AUTHORIZE' or value['task_type']!='RESPONSE_TASK' or value['authorization_scope']!='RESPOND_WITHIN_OBLIGATIONS':
        raise ValueError('not_response_authorized')
    action=value['evaluated_request']['action']
    if action_type and action['type']!=action_type:raise ValueError('response_action_mismatch')
    if value['obligations']!=obligations_for(action):raise ValueError('response_obligation_mismatch')
    if value['normalized_request']!=value['evaluated_request'] or value['request_id']!=value['evaluated_request']['request_id']:
        raise ValueError('response_request_mismatch')
    return value
