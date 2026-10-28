"""Parallel LSG-to-SPINE authorization boundary; no SafeGate Core calls."""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from jsonschema import Draft202012Validator

@dataclass(frozen=True)
class LSGAuthorizedTask:
    schema_version: str
    lsg_version: str
    session_id: str
    evaluation_id: str
    request_id: str
    action_type: str
    target: dict
    resource: dict
    parameters: dict
    authorization_scope: str
    obligations: tuple
    context_id: str
    context_hash: str
    runtime_candidate_hash: str
    provenance: dict
    raw_text: str

    def to_dict(self): return asdict(self)

def authorize_for_spine(authorization, trusted_context, expected_provenance, runtime_candidates, authorization_schema_path):
    schema=json.loads(Path(authorization_schema_path).read_text(encoding='utf-8'))
    errors=list(Draft202012Validator(schema).iter_errors(authorization))
    if errors:raise ValueError('lsg_authorization_schema:'+errors[0].message)
    a=authorization
    if a.get('schema_version')!='lsg.authorization.v0.1' or a.get('lsg_version')!='0.1':
        raise ValueError('unsupported_lsg_contract')
    if a.get('fail_closed_reason') is not None or a.get('aggregate_rule')!='REJECT > DEFER > AUTHORIZE':
        raise ValueError('invalid_lsg_aggregate_contract')
    if a.get('decision')!='AUTHORIZE' or a.get('task_type')!='ROBOT_TASK' or a.get('status')!='AUTHORIZED_FOR_PLANNING':
        raise ValueError('robot_not_authorized')
    if a.get('authorization_scope')!='ALLOW_TO_ENTER_PLANNING' or a.get('obligations')!=['REQUIRE_ROBOGUARD_EXECUTION_CHECK']:
        raise ValueError('robot_scope_or_obligations')
    if a.get('provenance')!=expected_provenance or not all(isinstance(x,str) and len(x)==64 for x in expected_provenance.values()):
        raise ValueError('checkpoint_provenance_mismatch')
    context_hash=hashlib.sha256(json.dumps(trusted_context,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if a.get('context_snapshot_id')!=trusted_context.get('context_id') or a.get('context_snapshot_hash')!=context_hash:
        raise ValueError('context_mismatch')
    candidate_hash=hashlib.sha256(json.dumps(runtime_candidates,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if a.get('runtime_candidate_hash')!=candidate_hash:
        raise ValueError('runtime_candidate_hash_mismatch')
    request=a.get('evaluated_request') or {}
    if request!=a.get('normalized_request') or request.get('request_id')!=a.get('request_id'):
        raise ValueError('request_mismatch')
    action=request.get('action') or {}
    target=action.get('target') or {}
    if action.get('type')!='NAVIGATE_TO_LOCATION' or target.get('kind')!='LOCATION':
        raise ValueError('robot_action_mismatch')
    aliases=(trusted_context.get('reference_resolution') or {}).get('location_aliases') or {}
    trusted=(runtime_candidates.get('trusted_locations') or {}).get('candidates') or []
    if len(trusted)!=1 or not isinstance(trusted[0],dict):
        raise ValueError('location_not_unique_or_allowlisted')
    selected=trusted[0]
    if (set(selected)!={'alias','location_id'} or
            aliases.get(selected['alias'])!=selected['location_id'] or
            selected['location_id']!=target.get('id') or
            action.get('resource')!={'kind':'LOCATION','id':selected['alias']}):
        raise ValueError('location_not_unique_or_allowlisted')
    return LSGAuthorizedTask(schema_version='lsg.robot-task.v0.1',lsg_version=a['lsg_version'],
        session_id=a['session_id'],evaluation_id=a['evaluation_id'],request_id=a['request_id'],
        action_type=action['type'],target=target,resource=action['resource'],parameters=action['parameters'],
        authorization_scope=a['authorization_scope'],obligations=tuple(a['obligations']),
        context_id=a['context_snapshot_id'],context_hash=context_hash,
        runtime_candidate_hash=a['runtime_candidate_hash'],provenance=a['provenance'],raw_text=request['raw_text'])
