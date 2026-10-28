"""Frozen Gate A inference architecture; contains no training or dataset code."""
import json
import torch
from torch import nn
from torch.nn import functional as F
from transformers import AutoModel
from peft import LoraConfig, get_peft_model

LABELS = {
    'decision':['AUTHORIZE','DEFER','REJECT'],
    'task_type':['RESPONSE_TASK','ROBOT_TASK'],
    'response_branch':['CARE_GUIDANCE','PUBLIC_CARE_INFORMATION','MEDICATION_INFORMATION','DEPARTMENT_GUIDANCE'],
    'defer_type':['NEEDS_USER_INPUT','HUMAN_ESCALATION_REQUIRED'],
}


def candidate_input(row):
    candidates=row['runtime_candidates']; formatted={}
    for name in ('department_catalog','trusted_locations'):
        item=candidates[name]
        if not isinstance(item.get('applicable'),bool) or not isinstance(item.get('candidates'),list):
            raise ValueError('invalid '+name+' candidates')
        values=item['candidates']
        if name=='department_catalog':
            if any(not isinstance(x,str) or not x for x in values): raise ValueError('invalid department candidate')
            values=sorted(values)
        else:
            if any(not isinstance(x,dict) or set(x)!={'alias','location_id'} or not all(isinstance(x[k],str) and x[k] for k in ('alias','location_id')) for x in values):
                raise ValueError('invalid trusted location candidate')
            values=sorted(({'alias':x['alias'],'location_id':x['location_id']} for x in values),key=lambda x:(x['alias'],x['location_id']))
        formatted[name]={'applicable':item['applicable'],'candidates':values}
    return '用户请求：'+row['text']+'\n运行时候选：'+json.dumps(formatted,ensure_ascii=False,sort_keys=True,separators=(',',':'))


class SharedMLPHeads(nn.Module):
    def __init__(self,dimension=1024,hidden=256,dropout=0.2):
        super().__init__()
        self.shared=nn.Sequential(nn.Linear(dimension,hidden),nn.ReLU(),nn.Dropout(dropout))
        self.heads=nn.ModuleDict({head:nn.Linear(hidden,len(classes)) for head,classes in LABELS.items()})
    def forward(self,embedding):
        features=self.shared(embedding)
        return {head:layer(features) for head,layer in self.heads.items()}


class EmbeddingGateModel(nn.Module):
    def __init__(self,base_model_path,dtype):
        super().__init__()
        base=AutoModel.from_pretrained(str(base_model_path),local_files_only=True,dtype=dtype,attn_implementation='sdpa')
        names=[name for name,_ in base.named_modules()]
        for suffix in ('q_proj','v_proj'):
            found=[name for name in names if name.endswith('.'+suffix)]
            if len(found)!=28: raise ValueError(f'expected_28_{suffix}_modules:{len(found)}')
        self.backbone=get_peft_model(base,LoraConfig(r=8,lora_alpha=16,lora_dropout=0.05,target_modules=['q_proj','v_proj'],bias='none'))
        self.backbone.config.use_cache=False
        self.heads=SharedMLPHeads(hidden=256,dropout=0.2)
    def forward(self,ids,mask):
        hidden=self.backbone(input_ids=ids,attention_mask=mask,use_cache=False).last_hidden_state
        index=mask.long().sum(1)-1
        pooled=hidden[torch.arange(len(index),device=hidden.device),index]
        return self.heads(F.normalize(pooled.float(),p=2,dim=1))
