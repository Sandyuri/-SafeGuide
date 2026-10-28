#!/usr/bin/env python3
"""Verify packaged artifacts, optional base models, relative configs, and secret hygiene."""
import argparse,json,re,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent))
from LSG.runtime.config import verify

def main():
    p=argparse.ArgumentParser();p.add_argument('--with-base-models',action='store_true');a=p.parse_args()
    for name in ('paths.json','model_registry.json','runtime.json'):
        value=json.loads((ROOT/'configs'/name).read_text(encoding='utf-8'))
        def walk(x):
            if isinstance(x,dict):
                for v in x.values():walk(v)
            elif isinstance(x,list):
                for v in x:walk(v)
            elif isinstance(x,str) and ('/home/' in x or '\\Users\\' in x):raise ValueError('absolute_personal_path:'+name)
        walk(value)
    bad=[]
    patterns=[re.compile(r'sk-[A-Za-z0-9_-]{20,}'),re.compile(r'(?i)(api[_-]?key)\s*[=:]\s*["\']?[A-Za-z0-9_-]{20,}')]
    for path in ROOT.rglob('*'):
        if not path.is_file() or path.name in {'model.pt','adapter_model.safetensors'}:continue
        try:text=path.read_text(errors='strict')
        except (UnicodeDecodeError,OSError):continue
        if any(p.search(text) for p in patterns):bad.append(str(path.relative_to(ROOT)))
    if bad:raise ValueError('possible_api_key:'+','.join(bad))
    manifest=verify(verify_base_models=a.with_base_models)
    print(json.dumps({'status':'OK','base_models_verified':a.with_base_models,'verified_hashes':len(manifest['hashes'])},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
