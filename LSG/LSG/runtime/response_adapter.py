"""API client and authorized Response dispatch without Core imports."""
import json
import os
from urllib.parse import urlsplit
from .lsg_contract import validate_response_authorization

def create_client():
    from openai import OpenAI
    return OpenAI(api_key=os.environ['MODEL_API_KEY'],base_url=os.environ['MODEL_BASE_URL'],timeout=60.0,max_retries=0)

def model_request_options():
    raw=os.getenv('MODEL_EXTRA_BODY_JSON','').strip()
    extra=json.loads(raw) if raw else {}
    if not isinstance(extra,dict):raise ValueError('MODEL_EXTRA_BODY_JSON must be an object')
    endpoint=urlsplit(os.getenv('MODEL_BASE_URL',''))
    if endpoint.scheme=='https' and endpoint.hostname=='api.deepseek.com' and os.getenv('MODEL_NAME')=='deepseek-flash' and not any(x in extra for x in ('thinking','reasoning_effort')):
        extra['thinking']={'type':'disabled'}
    if endpoint.scheme=='https' and endpoint.hostname=='maas.qianwenaiapi.com' and os.getenv('MODEL_NAME')=='deepseek-v4-flash-0731' and not any(x in extra for x in ('enable_thinking','thinking','reasoning_effort')):
        extra['enable_thinking']=False
    return {'extra_body':extra} if extra else {}

def call_response(result):
    validate_response_authorization(result)
    from LSG.response.care_guidance import generate_care_guidance_response
    from LSG.response.medication import generate_medication_response
    from LSG.response.generator import generate_department_response
    from LSG.response.public_care import generate_public_care_response
    action=result.evaluated_request['action']['type']
    if action in ('GET_GENERAL_CARE_GUIDANCE','GET_EMERGENCY_CARE_GUIDANCE'):
        return generate_care_guidance_response(result).to_dict(),'real_local'
    if action=='EXPLAIN_MEDICATION_CONCEPT':
        return generate_medication_response(result).to_dict(),'real_local'
    if action=='GET_GENERAL_MEDICATION_QUESTION' and result.evaluated_request['action']['parameters']['answer_mode']=='PRODUCT_INSTRUCTIONS_UNVERIFIED':
        return generate_medication_response(result).to_dict(),'real_local'
    if not all(os.getenv(k) for k in ('MODEL_API_KEY','MODEL_NAME','MODEL_BASE_URL')):
        return {'status':'BLOCKED_MISSING_API_ENV','text':None,'failure_reason':'MODEL_API_KEY, MODEL_NAME, MODEL_BASE_URL required'},'boundary_only'
    if action=='RECOMMEND_DEPARTMENT':
        return generate_department_response(result,client_factory=create_client).to_dict(),'real_api'
    if action=='GET_PUBLIC_CARE_INFORMATION':
        return generate_public_care_response(result,client_factory=create_client).to_dict(),'real_api'
    if action in ('GET_GENERAL_MEDICATION_INFO','GET_GENERAL_MEDICATION_QUESTION','EXPLAIN_MEDICATION_TERM'):
        return generate_medication_response(result,client_factory=create_client).to_dict(),'real_api'
    raise ValueError('unsupported_response_action')
