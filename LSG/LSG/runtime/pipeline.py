"""LSG-only Splitter, Gate, native contracts, and dry-run dispatch."""
import json
import re
import os
import time
from uuid import uuid4
from jsonschema import Draft202012Validator
from .config import PROJECT, verify
from .action_adapter import official_candidates, verify_candidates
from .lsg_contract import RULE, VERSION, create_authorization, digest, provenance, OrchestrationResult
from .models import parse_runtime_split, validate_gate, verify_coverage
from .location_resolver import needs_location_resolution, validate_resolution
from .response_adapter import call_response

def aggregate(decisions):
    if 'REJECT' in decisions:return 'REJECT'
    if 'DEFER' in decisions:return 'DEFER'
    return 'AUTHORIZE'

def missing_information_type(text):
    if any(word in text for word in ('挂什么号','挂哪个科','什么科','哪个科','科室')):
        return 'SYMPTOM_DETAILS'
    if any(word in text for word in ('带我去','带我到','前往','导航')):
        return 'DESTINATION_DETAILS'
    if any(word in text for word in ('哪里挂号','去哪挂号','挂号在哪里','挂号处','挂号窗口')):
        return 'REGISTRATION_LOCATION_OR_ROUTE'
    return 'REQUEST_DETAILS'

def followup_message(text, context=None):
    kind=missing_information_type(text)
    if kind=='SYMPTOM_DETAILS':
        return '请补充主要不适、发生部位和持续时间，以便提供就诊科室方向。'
    if kind=='DESTINATION_DETAILS':
        aliases=((context or {}).get('reference_resolution') or {}).get('location_aliases') or {}
        if '挂号' in text:
            options=sorted(alias for alias in aliases if '挂号' in alias)
            if options:
                return '请从可信地点目录选择准确目的地：'+'或'.join('“'+name+'”' for name in options)+'。仅说“挂号处”或“挂号窗口”无法唯一匹配。'
        return '请补充要去的具体地点名称；地点必须在可信目录中。'
    if kind=='REGISTRATION_LOCATION_OR_ROUTE':
        return '请说明您是想了解挂号地点，还是需要机器人带路；若需带路，请使用可信地点的准确名称（如“挂号收费窗口”）。'
    if re.search(r'冲泡|冲药', text) and re.search(r'多少度|水温|热水|温水', text):
        return '请提供具体药品名称、剂型及说明书上的冲泡要求；不同药品适用的水温可能不同。'
    if re.search(r'用途|用于|治疗什么|有什么作用|适应症', text) and re.search(r'药|布洛芬|西替利嗪|对乙酰氨基酚', text):
        return '请确认您只想了解药品的一般用途，还是想判断本人是否适合使用；若只问一般用途，请回复“仅问一般用途”。'
    if re.search(r'药|服用|片|粒|布洛芬|西替利嗪|对乙酰氨基酚', text):
        return '请补充药品完整名称，以及您想了解的一般信息范围（如用途、类别或说明书术语）；个人用药决定请咨询医生或药师。'
    return '请补充这项请求的具体信息。'

def api_ready():
    return all(os.getenv(name) for name in ('MODEL_API_KEY','MODEL_NAME','MODEL_BASE_URL'))

def robot_handoff(authorization, context, expected_provenance, runtime_candidates):
    from LSG.integrations.roboguard.adapter import authorize_for_spine
    return authorize_for_spine(authorization.to_dict(),context,expected_provenance,runtime_candidates,
                               PROJECT/'schemas/lsg-authorization.schema.json').to_dict()

class Pipeline:
    def __init__(self, splitter=None, gate=None, *, integration_manifest=None, model_provenance=None, catalogs=None, location_resolver=None):
        started=time.perf_counter()
        self.integration_manifest=verify() if integration_manifest is None else integration_manifest
        verified=time.perf_counter()
        self.model_provenance=provenance() if model_provenance is None else model_provenance
        provenanced=time.perf_counter()
        self.splitter=splitter if splitter is not None else self._legacy_splitter()
        split_loaded=time.perf_counter()
        self.gate=gate if gate is not None else self._legacy_gate()
        gate_loaded=time.perf_counter()
        self.startup_timings_ms={'frozen_hash_verification':round((verified-started)*1000),
            'provenance_hashes':round((provenanced-verified)*1000),
            'splitter_load':round((split_loaded-provenanced)*1000),
            'gate_load':round((gate_loaded-split_loaded)*1000),
            'total':round((gate_loaded-started)*1000)}
        self.response_schema=json.loads((PROJECT/'schemas/response-result.schema.json').read_text(encoding='utf-8'))
        self.catalogs=catalogs
        if location_resolver is None and splitter is None and gate is None:
            from .location_resolver import APILocationResolver
            location_resolver=APILocationResolver()
        self.location_resolver=location_resolver

    @staticmethod
    def _legacy_splitter():
        from .models import Splitter
        return Splitter()

    @staticmethod
    def _legacy_gate():
        from .models import Gate
        return Gate()

    def run(self, raw_input, context, *, split_override=None, component_split_inputs=None, candidate_override=None, dispatch=True, session_id=None, expected_intent_count=None, location_selections=None):
        if not isinstance(raw_input,str) or not raw_input.strip():raise ValueError('empty_input')
        started=time.perf_counter()
        result={'schema_version':'lsg.orchestration.v0.1','lsg_version':VERSION,
                'session_id':session_id or 'lsg-session-'+str(uuid4()),'raw_input':raw_input,
                'splitter_raw_output':None,'splitter_output_source':'MODEL_SINGLE_CALL',
                'splitter_component_raw_outputs':[],'splitter_normalization':'none',
                'parsed_intents':[],'gate_inputs':[],
                'per_intent_predictions':[],'runtime_candidates':[],'location_resolutions':[],'authorizations':[],
                'aggregate_decision':'DEFER','aggregate_rule':RULE,'provenance':self.model_provenance,
                'context_snapshot_hash':digest(context),'dispatch_plan':{'items':[],'response_fan_in':[],
                'followups':[],'dry_run_robot':True},'response_results':[],
                'final_response_text':None,'fail_closed_reason':None,
                'timings_ms':{'splitter':0,'gate_and_contract':0,'response':0,'total':0}}
        objects=[]
        try:
            if component_split_inputs is not None:
                if split_override is not None or not isinstance(component_split_inputs,list) or not 2<=len(component_split_inputs)<=3:
                    raise ValueError('invalid_resume_split_components')
                result['splitter_output_source']='PER_INTENT_REAL_MODEL_CALLS'
                intents=[]
                for source in component_split_inputs:
                    if not isinstance(source,str) or not source.strip():
                        raise ValueError('invalid_resume_split_component')
                    component_raw=(self.splitter.split(source).raw_output if hasattr(self.splitter,'split')
                                   else self.splitter.predict(source))
                    component={'source':source,'raw_output':component_raw}
                    result['splitter_component_raw_outputs'].append(component)
                    parsed,component['normalization']=parse_runtime_split(source,component_raw)
                    parsed=verify_coverage(source,parsed)
                    if len(parsed)!=1:raise ValueError('resume_component_intent_count_changed')
                    component['parsed_intent']=parsed[0]
                    intents.append(parsed[0])
                raw='\n'.join(f'[意图{i}] {text}' for i,text in enumerate(intents,1))
                result['splitter_raw_output']=raw
                intents=verify_coverage(raw_input,intents)
            else:
                raw=split_override if split_override is not None else (self.splitter.split(raw_input).raw_output if hasattr(self.splitter,'split') else self.splitter.predict(raw_input))
                result['splitter_raw_output']=raw
                intents,normalization=parse_runtime_split(raw_input,raw)
                result['splitter_normalization']=normalization
                intents=verify_coverage(raw_input,intents)
            result['timings_ms']['splitter']=round((time.perf_counter()-started)*1000)
            gate_started=time.perf_counter()
            result['parsed_intents']=intents
            if expected_intent_count is not None and len(intents)!=expected_intent_count:
                raise ValueError('resume_intent_count_changed')
            candidates_batch=[]
            location_followups=[]
            selections=location_selections or {}
            if not isinstance(selections,dict) or any(not isinstance(i,int) or i<1 or i>len(intents) for i in selections):
                raise ValueError('invalid_location_selection_index')
            for index,text in enumerate(intents,1):
                resolution=None
                candidates=candidate_override if candidate_override is not None else official_candidates(text,context)
                if index in selections:
                    selected=selections[index]
                    if not isinstance(selected,dict) or selected.get('alias') not in text or not needs_location_resolution(text):
                        raise ValueError('location_selection_not_bound_to_intent')
                    resolution=validate_resolution({'status':'RESOLVED','candidates':[selected]},context)
                elif candidate_override is None and needs_location_resolution(text):
                    local_locations=candidates['trusted_locations']['candidates']
                    if len(local_locations)==1:
                        # An exact unique allow-list match needs no external
                        # semantic resolver; bind it deterministically.
                        resolution=validate_resolution({'status':'RESOLVED','candidates':local_locations},context)
                    elif self.location_resolver is not None:
                        resolution=validate_resolution(self.location_resolver.resolve(text,context),context)
                if resolution is not None:
                    candidates['trusted_locations']={'applicable':bool(resolution['candidates']),
                        'candidates':resolution['candidates'],'context_id':context['context_id']}
                    if resolution['status'] != 'RESOLVED':
                        names=[item['alias'] for item in resolution['candidates']]
                        message=('请从可信地点目录选择准确目的地：'+'或'.join('“'+name+'”' for name in names)+'。'
                                 if names else '请补充可信地点目录中的准确目的地名称；若要改去其他目的地，请输入 /取消 后重新提出。')
                        location_followups.append({'intent_index':index,'type':'NEEDS_USER_INPUT','message':message})
                verify_candidates(candidates,text,context,location_resolution=resolution)
                candidates_batch.append(candidates)
                result['location_resolutions'].append(resolution)
            result['runtime_candidates']=candidates_batch
            if location_followups:
                # Clarification is an input boundary before Gate prediction.
                # No model decision or authorization is fabricated for it.
                result['fail_closed_reason']='location_needs_clarification'
                result['dispatch_plan']['followups']=location_followups
                result['timings_ms']['gate_and_contract']=round((time.perf_counter()-gate_started)*1000)
                result['timings_ms']['total']=round((time.perf_counter()-started)*1000)
                return OrchestrationResult(result)
            if hasattr(self.gate,'predict_batch'):
                predictions=self.gate.predict_batch(intents,candidates_batch)
            elif hasattr(self.gate,'predict') and hasattr(self.splitter,'split'):
                predictions=self.gate.predict(intents,candidates_batch)
            else:
                predictions=[self.gate.predict(text,candidates) for text,candidates in zip(intents,candidates_batch)]
            if not isinstance(predictions,list) or len(predictions)!=len(intents):
                raise ValueError('gate_batch_length_mismatch')
            for index,(text,candidates,prediction) in enumerate(zip(intents,candidates_batch,predictions),1):
                if hasattr(self.splitter,'split') and (not isinstance(prediction.get('raw_heads'),dict) or not isinstance(prediction.get('gate_input'),str)):
                    raise ValueError('invalid_gate_prediction_contract')
                p=validate_gate(prediction)
                result['gate_inputs'].append(p.get('gate_input',text));result['per_intent_predictions'].append(p)
                authorization=create_authorization(text,p,candidates,context,index,result['session_id'],self.model_provenance)
                objects.append(authorization);result['authorizations'].append(authorization.to_dict())
            result['timings_ms']['gate_and_contract']=round((time.perf_counter()-gate_started)*1000)
            result['aggregate_decision']=aggregate([p['decision'] for p in result['per_intent_predictions']])
            if result['aggregate_decision']=='DEFER':
                followups=[]
                for i,p in enumerate(result['per_intent_predictions'],1):
                    if p['decision']!='DEFER':
                        continue
                    if i in selections:
                        followups.append({'intent_index':i,'type':'HUMAN_ESCALATION_REQUIRED',
                            'message':'目的地已确认，但目前无法授权自动带路，请联系现场工作人员。'})
                    else:
                        followups.append({'intent_index':i,'type':p['defer_type'],
                            'message':followup_message(result['parsed_intents'][i-1],context)
                            if p['defer_type']=='NEEDS_USER_INPUT' else '请联系人工处理。'})
                result['dispatch_plan']['followups']=followups
            elif result['aggregate_decision']=='AUTHORIZE':
                for index,(prediction,authorization) in enumerate(zip(result['per_intent_predictions'],objects),1):
                    action=authorization.evaluated_request['action']
                    if prediction['task_type']=='RESPONSE_TASK':
                        item={'intent_index':index,'route':'Response','request_id':authorization.request_id,
                              'evaluation_id':authorization.evaluation_id,'action_type':action['type']}
                        result['dispatch_plan']['items'].append(item);result['dispatch_plan']['response_fan_in'].append(index)
                    else:
                        task=robot_handoff(authorization,context,self.model_provenance,result['runtime_candidates'][index-1])
                        result['dispatch_plan']['items'].append({'intent_index':index,'route':'SPINE_TO_RoboGuard_DRY_RUN',
                            'request_id':authorization.request_id,'evaluation_id':authorization.evaluation_id,
                            'target_location_id':action['target']['id'],'authorized_task':task})
                if dispatch:
                    response_started=time.perf_counter()
                    for item in result['dispatch_plan']['items']:
                        if item['route']!='Response':continue
                        try:
                            response,mode=call_response(objects[item['intent_index']-1])
                            errors=list(Draft202012Validator(self.response_schema).iter_errors(response))
                            if errors:raise ValueError('response_schema:'+errors[0].message)
                        except Exception as error:
                            response={'status':'RESPONSE_GENERATION_FAILED','text':None,'failure_reason':type(error).__name__}
                            mode='response_error'
                        result['response_results'].append({'intent_index':item['intent_index'],'mode':mode,'response':response})
                    result['timings_ms']['response']=round((time.perf_counter()-response_started)*1000)
                    if result['response_results'] and all(x['response']['status']=='GENERATED' for x in result['response_results']):
                        texts=[x['response']['text'] for x in result['response_results']]
                        result['final_response_text']=texts[0] if len(texts)==1 else '\n'.join(f'{i}. {text}' for i,text in enumerate(texts,1))
        except Exception as error:
            result['aggregate_decision']='DEFER';result['fail_closed_reason']=type(error).__name__+':'+str(error)
            result['dispatch_plan']={'items':[],'response_fan_in':[],'followups':[{'intent_index':None,
                'type':'HUMAN_ESCALATION_REQUIRED','message':'请联系人工处理。'}],'dry_run_robot':True}
            result['response_results']=[];result['final_response_text']=None
        result['timings_ms']['total']=round((time.perf_counter()-started)*1000)
        return OrchestrationResult(result)
