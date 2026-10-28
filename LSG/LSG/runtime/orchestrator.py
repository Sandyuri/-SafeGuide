"""LSG-native multi-turn orchestration. Every resumed intent is re-evaluated."""
from datetime import datetime,timezone
import copy
import unicodedata
from uuid import uuid4
from .session import SessionStore
from .pipeline import missing_information_type


def _choice_key(value):
    """Ignore display-only spacing in an offered catalog name, not its words."""
    normalized=unicodedata.normalize('NFKC',value)
    return ''.join(char for char in normalized
                   if not char.isspace() and unicodedata.category(char)!='Cf')

class LSGOrchestrator:
    MAX_SUPPLEMENTS=2
    def __init__(self,pipeline,store=None):
        self.pipeline=pipeline;self.store=store or SessionStore()

    def start(self,text,context,**kwargs):
        session_id='lsg-session-'+str(uuid4())
        result=self.pipeline.run(text,context,session_id=session_id,**kwargs)
        self._save(result,context,attempt=0,original_input=text)
        return result

    def resume(self,session_id,supplements,context,**kwargs):
        record=self.store.get(session_id)
        if record is None:raise ValueError('unknown_session_id')
        if record['state']!='PENDING_USER_INPUT':raise ValueError('no_pending_intent')
        if record['context_hash']!=self.pipeline_context_hash(context):raise ValueError('session_context_mismatch')
        pending=record['pending']
        expected={int(x['intent_index']) for x in pending}
        if not isinstance(supplements,dict) or set(supplements)!=expected or any(not isinstance(v,str) or not v.strip() for v in supplements.values()):
            raise ValueError('pending_supplement_mismatch')
        if record['attempt']>=self.MAX_SUPPLEMENTS:
            return self._escalate(record,'supplement_limit_reached')
        base_intents=record.get('base_intents',record['intents'])
        if len(record['intents'])!=len(base_intents):
            return self._escalate(record,'session_intent_count_mismatch')
        resolutions=record.get('last_result',{}).get('location_resolutions') or []
        aliases=(context.get('reference_resolution') or {}).get('location_aliases') or {}
        selections={}
        for index,answer in supplements.items():
            resolution=resolutions[index-1] if index <= len(resolutions) else None
            chosen=answer.strip()
            if resolution and resolution.get('status')=='AMBIGUOUS':
                offered={_choice_key(entry['alias']):entry for entry in resolution['candidates']}
                if len(offered)!=len(resolution['candidates']) or _choice_key(chosen) not in offered:
                    return self._invalid_location_choice(record,index,
                        {entry['alias'] for entry in resolution['candidates']},chosen)
                selections[index]=offered[_choice_key(chosen)]
            elif resolution and resolution.get('status')=='UNKNOWN' and chosen in aliases:
                selections[index]={'alias':chosen,'location_id':aliases[chosen]}
        history={str(k):list(v) for k,v in record.get('supplement_history',{}).items()}
        clauses=[]
        for i,text in enumerate(record['intents'],1):
            if i in expected:
                history.setdefault(str(i),[]).append(supplements[i].strip())
                # A new destination names the current target. Keep older answers
                # in the audit history, but do not present superseded locations
                # to the Gate as simultaneous candidate destinations.
                relevant=history[str(i)][-1:] if missing_information_type(base_intents[i-1])=='DESTINATION_DETAILS' else history[str(i)]
                if i in selections:
                    # The user chose one exact allow-list destination. Present a
                    # canonical navigation request to the Gate while retaining
                    # the original intent and supplement in the session audit.
                    clauses.append('带我去'+selections[i]['alias'])
                else:
                    clauses.append('，'.join([*relevant,base_intents[i-1]]))
            else:clauses.append(text)
        # Splitter outputs often end each intent with 。; joining those clauses
        # directly creates 。； and may make the next numbered intent start with ；.
        combined='；'.join(clause.rstrip('。；;，, ') for clause in clauses)
        result=self.pipeline.run(combined,context,session_id=session_id,
                                 expected_intent_count=len(base_intents),
                                 component_split_inputs=clauses if len(base_intents)>1 else None,
                                 location_selections=selections,**kwargs)
        attempt=record['attempt']+1
        if result.get('fail_closed_reason','') and 'resume_intent_count_changed' in result['fail_closed_reason']:
            return self._count_mismatch(record,result,attempt,history)
        self._save(result,context,attempt=attempt,original_input=record['original_input'],previous=record,base_intents=base_intents,supplement_history=history)
        return result

    def _invalid_location_choice(self,record,index,allowed,chosen):
        result=copy.deepcopy(record['last_result'])
        attempt=record['attempt']+1
        if attempt>=self.MAX_SUPPLEMENTS:
            return self._escalate(record,'location_choice_limit_reached')
        result['dispatch_plan']['items']=[]
        result['dispatch_plan']['response_fan_in']=[]
        result['dispatch_plan']['followups']=[{'intent_index':index,'type':'NEEDS_USER_INPUT',
            'message':'请只从当前候选中选择：'+'或'.join('“'+name+'”' for name in sorted(allowed))+'。'}]
        result['response_results']=[];result['final_response_text']=None
        result['fail_closed_reason']='location_choice_outside_offered_candidates'
        record['attempt']=attempt;record['last_result']=result
        record['audit_history'].append({'attempt':attempt,'raw_input':chosen,
            'aggregate_decision':'DEFER','fail_closed_reason':result['fail_closed_reason'],
            'authorizations':[]})
        record['updated_at']=datetime.now(timezone.utc).isoformat()
        self.store.put(record)
        return result

    def _count_mismatch(self,record,result,attempt,history):
        """Keep the original pending mapping when a supplement splits into new intents."""
        pending=copy.deepcopy(record['pending'])
        for item in pending:item['attempt']=attempt
        state='PENDING_USER_INPUT' if attempt<self.MAX_SUPPLEMENTS else 'HUMAN_ESCALATION_REQUIRED'
        if state=='PENDING_USER_INPUT':
            followups=[{'intent_index':item['intent_index'],'type':'NEEDS_USER_INPUT',
                        'message':'补充内容被识别为额外请求。请只补充原请求所缺的信息；若要改为新的带路任务，请取消本轮后重新提出，并使用可信地点的准确名称。'}
                       for item in pending]
        else:
            followups=[{'intent_index':None,'type':'HUMAN_ESCALATION_REQUIRED','message':'请联系人工处理。'}]
        result['dispatch_plan']['items']=[]
        result['dispatch_plan']['response_fan_in']=[]
        result['dispatch_plan']['followups']=followups
        result['response_results']=[];result['final_response_text']=None
        record['state']=state;record['attempt']=attempt;record['pending']=pending
        record['supplement_history']=history;record['last_result']=result
        record['audit_history'].append({'attempt':attempt,'raw_input':result['raw_input'],
            'aggregate_decision':'DEFER','fail_closed_reason':result['fail_closed_reason'],
            'authorizations':[]})
        record['updated_at']=datetime.now(timezone.utc).isoformat()
        self.store.put(record)
        return result

    def _save(self,result,context,attempt,original_input,previous=None,base_intents=None,supplement_history=None):
        decisions=result['per_intent_predictions']
        pending=[{'intent_index':i+1,'original_intent':result['parsed_intents'][i],
                  'missing_information_type':missing_information_type(result['parsed_intents'][i]),'defer_type':p['defer_type'],'candidates':result['runtime_candidates'][i], 'candidate_hash':result['authorizations'][i]['runtime_candidate_hash'] if i<len(result['authorizations']) else None,
                  'attempt':attempt} for i,p in enumerate(decisions) if p['decision']=='DEFER']
        # Location clarification happens before Gate prediction; keep it as
        # pending input without inventing a Gate DEFER prediction.
        for followup in result['dispatch_plan']['followups']:
            index=followup.get('intent_index')
            if followup.get('type')=='NEEDS_USER_INPUT' and isinstance(index,int) and not any(x['intent_index']==index for x in pending):
                pending.append({'intent_index':index,'original_intent':result['parsed_intents'][index-1],
                    'missing_information_type':'DESTINATION_DETAILS','defer_type':'NEEDS_USER_INPUT',
                    'candidates':result['runtime_candidates'][index-1],
                    'candidate_hash':None,'attempt':attempt})
        needs_human=any(item.get('type')=='HUMAN_ESCALATION_REQUIRED'
                        for item in result['dispatch_plan']['followups'])
        state='PENDING_USER_INPUT' if result['aggregate_decision']=='DEFER' and pending and not needs_human and all(x['defer_type']=='NEEDS_USER_INPUT' for x in pending) and attempt<self.MAX_SUPPLEMENTS else 'HUMAN_ESCALATION_REQUIRED' if result['aggregate_decision']=='DEFER' else 'TERMINAL'
        record={'session_id':result['session_id'],'state':state,'original_input':original_input,
                'intents':result['parsed_intents'],'base_intents':base_intents or result['parsed_intents'],
                'supplement_history':supplement_history or {},'pending':pending,'attempt':attempt,
                'context_hash':self.pipeline_context_hash(context),'last_result':result,
                'suspended_authorized_intents':[{'intent_index':i+1,'text':result['parsed_intents'][i]} for i,p in enumerate(decisions) if p['decision']=='AUTHORIZE'],
                'audit_history':(previous.get('audit_history',[]) if previous else [])+[{'attempt':attempt,'raw_input':result['raw_input'],'aggregate_decision':result['aggregate_decision'],'fail_closed_reason':result['fail_closed_reason'],'authorizations':result['authorizations']}],
                'updated_at':datetime.now(timezone.utc).isoformat()}
        if state=='HUMAN_ESCALATION_REQUIRED' and result['aggregate_decision']=='DEFER' and not needs_human:
            result['dispatch_plan']['followups']=[{'intent_index':None,'type':'HUMAN_ESCALATION_REQUIRED','message':'请联系人工处理。'}]
        self.store.put(record)
        return record

    def _escalate(self,record,reason):
        result=record['last_result'];result['aggregate_decision']='DEFER';result['fail_closed_reason']=reason
        result['dispatch_plan']['items']=[];result['dispatch_plan']['followups']=[{'intent_index':None,'type':'HUMAN_ESCALATION_REQUIRED','message':'请联系人工处理。'}]
        result['response_results']=[];result['final_response_text']=None
        record['state']='HUMAN_ESCALATION_REQUIRED';record['last_result']=result;self.store.put(record)
        return result

    @staticmethod
    def pipeline_context_hash(context):
        from .lsg_contract import digest
        return digest(context)
