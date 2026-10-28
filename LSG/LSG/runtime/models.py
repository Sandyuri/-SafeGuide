"""Frozen splitter and Gate A inference. Model weights are loaded read only."""
import json
import re
import sys
from pathlib import Path
from .config import paths

LABELS = {'decision': ['AUTHORIZE','DEFER','REJECT'], 'task_type': ['RESPONSE_TASK','ROBOT_TASK'],
          'response_branch': ['CARE_GUIDANCE','PUBLIC_CARE_INFORMATION','MEDICATION_INFORMATION','DEPARTMENT_GUIDANCE'],
          'defer_type': ['NEEDS_USER_INPUT','HUMAN_ESCALATION_REQUIRED']}
LINE = re.compile(r'^\[意图([1-3])\] (\S.*)$')

def parse_split(raw):
    if not isinstance(raw, str): raise ValueError('splitter_output_type')
    lines = raw.splitlines()
    if not 1 <= len(lines) <= 3: raise ValueError('splitter_line_count')
    result = []
    for number, line in enumerate(lines, 1):
        match = LINE.fullmatch(line)
        if not match or int(match.group(1)) != number:
            raise ValueError('splitter_protocol')
        result.append(match.group(2))
    return result

def parse_runtime_split(raw_input, raw):
    """Remove only a verifiable repeated prompt echo after an intent sentence.

    The raw model text stays in the audit. Unknown extra text remains invalid.
    This compatibility rule applies only when the model emitted 2-3 numbered
    intents, every retained clause appears in the user's source text, and the
    discarded suffix starts by repeating that same clause.
    """
    intents=parse_split(raw)
    if len(intents)<2 or '要求：' in raw_input:
        return intents, 'none'
    cleaned=[];changed=False
    source=re.sub(r'[\s，。；;！？?!、,.：:]', '', raw_input)
    for intent in intents:
        if '。 要求：' not in intent:
            if '要求：' in intent:
                raise ValueError('splitter_unverified_prompt_echo')
            cleaned.append(intent)
            continue
        prefix,suffix=intent.split('。 要求：',1)
        prefix=prefix.strip()
        normalized=re.sub(r'[\s，。；;！？?!、,.：:]', '', prefix)
        evidence=suffix.lstrip()
        if evidence.startswith("请"):
            evidence=evidence[1:].lstrip()
        if not prefix or not normalized or normalized not in source or not evidence.startswith(prefix):
            raise ValueError("splitter_unverified_prompt_echo")
        cleaned.append(prefix+'。')
        changed=True
    return cleaned, 'verified_prompt_echo_removed' if changed else 'none'

def validate_gate(p, minimum_confidence=0.5):
    decision = p.get('decision'); task = p.get('task_type'); branch = p.get('response_branch'); defer = p.get('defer_type')
    active = {'decision': True, 'task_type': decision == 'AUTHORIZE',
              'response_branch': decision == 'AUTHORIZE' and task == 'RESPONSE_TASK', 'defer_type': decision == 'DEFER'}
    for head, on in active.items():
        value = p.get(head)
        if on and value not in LABELS[head]: raise ValueError('invalid_' + head)
        if not on and value is not None: raise ValueError('masked_' + head)
        if on and (not isinstance(p.get('confidence', {}).get(head), (int,float)) or p['confidence'][head] < minimum_confidence or p['confidence'][head] > 1):
            raise ValueError('low_confidence_' + head)
    return p


def verify_coverage(raw_input, intents):
    """Fail closed when a substantive source clause disappears from all intents."""
    from difflib import SequenceMatcher
    def clean(value):
        return re.sub(r"[\s，。；;！？?!、,.：:]", "", value)
    clauses=[clean(part) for part in re.split(r"[。；;！？?!]",raw_input) if len(clean(part))>=6]
    predicted=[clean(part) for part in intents]
    # Explicit requests about different people need separate Gate evaluations.
    # A single combined intent must not reach a Response branch as if it were
    # a single-person request.
    people=re.search(r'妈妈|母亲|爸爸|父亲|孩子|女儿|儿子|家人|朋友',raw_input)
    self_reference=re.search(r'我本人|我自己',raw_input)
    if len(intents)==1 and '分别' in raw_input and people and self_reference:
        raise ValueError('splitter_undersegmented_multiple_people')
    # A splitter may normalize punctuation, but it may not invent long repeated
    # clauses. Excessive expansion is fail-closed before any Gate/dispatch call.
    source_length=len(clean(raw_input))
    predicted_length=sum(len(part) for part in predicted)
    if predicted_length > source_length*1.35+8:
        raise ValueError('splitter_unfounded_expansion')
    for clause in clauses:
        if not any(clause in item for item in predicted) and max((SequenceMatcher(None,clause,item).ratio() for item in predicted),default=0)<0.65:
            raise ValueError('splitter_source_clause_omitted')
    return intents

class Splitter:
    def __init__(self):
        import torch
        from transformers import AutoTokenizer,AutoModelForCausalLM
        from peft import PeftModel
        from .device import select_device
        p=paths();artifact=p['splitter_artifact'];self.device=select_device(torch)
        self.torch=torch;self.prompt=(artifact/'splitter_prompt.txt').read_text(encoding='utf-8')
        self.tokenizer=AutoTokenizer.from_pretrained(p['splitter_base_model'],local_files_only=True)
        dtype=torch.bfloat16 if self.device=='cuda' else torch.float32
        base=AutoModelForCausalLM.from_pretrained(p['splitter_base_model'],local_files_only=True,dtype=dtype).to(self.device)
        self.model=PeftModel.from_pretrained(base,str(artifact/'best'),local_files_only=True).eval()
        self.max_new_tokens=json.loads((artifact/'generation_config.json').read_text(encoding='utf-8'))['max_new_tokens']
    def predict(self,text):
        prompt=self.tokenizer.apply_chat_template([{'role':'system','content':self.prompt},{'role':'user','content':text}],tokenize=False,add_generation_prompt=True,enable_thinking=False)
        batch=self.tokenizer(prompt,return_tensors='pt').to(self.device)
        with self.torch.inference_mode():
            output=self.model.generate(**batch,do_sample=False,max_new_tokens=self.max_new_tokens,pad_token_id=self.tokenizer.eos_token_id)
        return self.tokenizer.decode(output[0,batch.input_ids.shape[1]:],skip_special_tokens=True)

class Gate:
    def __init__(self):
        import torch
        from transformers import AutoTokenizer
        from .device import select_device
        from .gate_model import EmbeddingGateModel,candidate_input
        p=paths();self.device=select_device(torch);self.torch=torch;self.candidate_input=candidate_input
        self.tokenizer=AutoTokenizer.from_pretrained(p['gate_base_model'],local_files_only=True)
        dtype=torch.float16 if self.device=='cuda' else torch.float32
        self.model=EmbeddingGateModel(p['gate_base_model'],dtype).to(self.device).eval()
        checkpoint=torch.load(p['gate_artifact']/'model.pt',map_location='cpu',weights_only=True)
        current={name for name,parameter in self.model.backbone.named_parameters() if 'lora_' in name}
        if set(checkpoint['lora'])!=current:raise ValueError('gate_lora_checkpoint_key_mismatch')
        self.model.backbone.load_state_dict(checkpoint['lora'],strict=False)
        self.model.heads.load_state_dict(checkpoint['heads'],strict=True)
    def predict(self,text,candidates):
        formatted=self.candidate_input({'text':text,'runtime_candidates':candidates})
        encoded=self.tokenizer(formatted,return_tensors='pt',truncation=False)
        if encoded['input_ids'].shape[1]>256:raise ValueError('gate_input_too_long')
        with self.torch.inference_mode():
            output=self.model(encoded['input_ids'].to(self.device),encoded['attention_mask'].to(self.device))
        raw={};confidence={}
        for head,names in LABELS.items():
            probs=output[head][0].float().softmax(-1);index=int(probs.argmax());raw[head]=names[index];confidence[head]=float(probs[index])
        decision=raw['decision'];task=raw['task_type'] if decision=='AUTHORIZE' else None
        prediction={'decision':decision,'task_type':task,'response_branch':raw['response_branch'] if task=='RESPONSE_TASK' else None,
                    'defer_type':raw['defer_type'] if decision=='DEFER' else None,'confidence':confidence,'raw_heads':raw,'gate_input':formatted}
        return validate_gate(prediction)
