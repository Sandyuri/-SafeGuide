"""Portable package paths and immutable dependency verification."""
import hashlib,json,os
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1]
OVERRIDES={
 'splitter_artifact':'LSG_SPLITTER_ARTIFACT','gate_artifact':'LSG_GATE_ARTIFACT',
 'splitter_base_model':'LSG_SPLITTER_BASE_MODEL','gate_base_model':'LSG_GATE_BASE_MODEL',
 'roboguard_adapter':'LSG_ROBOGUARD_ADAPTER','roboguard_allowlist':'LSG_ROBOGUARD_ALLOWLIST'}

def _resolve(value):
    candidate=Path(value).expanduser()
    return (candidate if candidate.is_absolute() else PROJECT/candidate).resolve()

def paths():
    values=json.loads((PROJECT/'configs/paths.json').read_text(encoding='utf-8'))
    return {key:_resolve(os.environ.get(OVERRIDES[key]) or value) for key,value in values.items()}

def default_context_path():
    config=json.loads((PROJECT/'configs/runtime.json').read_text(encoding='utf-8'))
    return _resolve(os.environ.get('LSG_CONTEXT_PATH') or config['default_context'])

def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def _verify_sums(base):
    for line in (base/'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        if not line.strip():continue
        digest,relative=line.split('  ',1)
        if sha(base/relative)!=digest:raise ValueError('frozen_hash_mismatch:'+str(base/relative))

def verify(*,verify_base_models=True):
    p=paths(); manifest={'package_root':str(PROJECT),'hashes':{},'test_accessed':False}
    for key in ('splitter_artifact','gate_artifact'):
        base=p[key];_verify_sums(base)
        data=json.loads((base/'manifest.json').read_text(encoding='utf-8'))
        checkpoint=base/data['checkpoint']
        if sha(checkpoint)!=data['checkpoint_sha256']:raise ValueError('checkpoint_hash_mismatch:'+key)
        manifest['hashes'][key+'_checkpoint']=data['checkpoint_sha256']
        manifest['hashes'][key+'_manifest']=sha(base/'manifest.json')
    registry=json.loads((PROJECT/'configs/base_models.json').read_text(encoding='utf-8'))
    if verify_base_models:
        for key,path_key in (('qwen3_0_6b','splitter_base_model'),('qwen3_embedding_0_6b','gate_base_model')):
            base=p[path_key]
            for relative,digest in registry[key]['hashes'].items():
                if sha(base/relative)!=digest:raise ValueError('base_model_hash_mismatch:'+key+'/'+relative)
                manifest['hashes'][key+'/'+relative]=digest
    for relative in ('response/care_guidance.py','response/generator.py','response/medication.py','response/public_care.py','response/department_catalog.json','integrations/roboguard/adapter.py','integrations/roboguard/first_floor_allowlist.json'):
        manifest['hashes'][relative]=sha(PROJECT/relative)
    source=json.loads(p['roboguard_allowlist'].read_text(encoding='utf-8')); local=json.loads((PROJECT/'configs/elevator-hall-context.json').read_text(encoding='utf-8'))
    expected=dict(source);expected['schema_version']='0.2';expected.pop('navigation_destination_resolution',None)
    if local!=expected:raise ValueError('elevator_context_source_mismatch')
    return manifest
