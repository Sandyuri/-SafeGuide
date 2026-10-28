#!/usr/bin/env python3
"""Prepare hash-locked base models by verification, local copy, or Hugging Face download."""
import argparse,hashlib,json,shutil,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def verify(target,entry):
    failures=[]
    for relative,digest in entry['hashes'].items():
        path=target/relative
        if not path.is_file(): failures.append(relative+':missing')
        elif sha(path)!=digest: failures.append(relative+':sha256')
    if failures: raise ValueError('base_model_verification_failed:'+','.join(failures))
    return {'target':str(target),'files':len(entry['hashes']),'verified':True}

def copy_locked(source,target,entry):
    target.mkdir(parents=True,exist_ok=True)
    for relative in entry['hashes']:
        src=source/relative; dst=target/relative
        if not src.is_file(): raise FileNotFoundError(src)
        dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',choices=['splitter','gate','all'],default='all')
    parser.add_argument('--source',type=Path,help='Existing local model directory; use one model at a time')
    parser.add_argument('--target',type=Path,help='Override target; use one model at a time')
    parser.add_argument('--download',action='store_true')
    args=parser.parse_args()
    registry=json.loads((ROOT/'configs/base_models.json').read_text(encoding='utf-8'))
    keys={'splitter':['qwen3_0_6b'],'gate':['qwen3_embedding_0_6b'],'all':['qwen3_0_6b','qwen3_embedding_0_6b']}[args.model]
    if (args.source or args.target) and len(keys)!=1: parser.error('--source/--target requires --model splitter or gate')
    results=[]
    for key in keys:
        entry=registry[key]; target=(args.target or ROOT/entry['target']).resolve()
        if args.source: copy_locked(args.source.resolve(),target,entry)
        elif args.download:
            try: from huggingface_hub import snapshot_download
            except ImportError: raise RuntimeError('install requirements.lock before --download') from None
            downloaded=Path(snapshot_download(repo_id=entry['repo_id'],revision=entry['revision'],allow_patterns=list(entry['hashes'])))
            copy_locked(downloaded,target,entry)
        results.append(verify(target,entry))
    print(json.dumps(results,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
