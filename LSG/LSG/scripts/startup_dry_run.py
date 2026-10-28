#!/usr/bin/env python3
"""Import and initialize the package with fake backends; never loads model weights."""
import json,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent))
from LSG.runtime.application import LSGApplication
from LSG.runtime.backends import FakeSplitterBackend,FakeGateBackend
from LSG.runtime.config import verify
from LSG.runtime.lsg_contract import provenance
from LSG.runtime.session import SessionStore

def prediction(text,candidates):
    return {'decision':'DEFER','task_type':None,'response_branch':None,'defer_type':'NEEDS_USER_INPUT',
            'confidence':{'decision':1.0,'task_type':1.0,'response_branch':1.0,'defer_type':1.0},
            'raw_heads':{'decision':'DEFER','task_type':'RESPONSE_TASK','response_branch':'CARE_GUIDANCE','defer_type':'NEEDS_USER_INPUT'},
            'gate_input':text}
def main():
    with tempfile.TemporaryDirectory(prefix='lsg-startup-') as tmp:
        app=LSGApplication(splitter_factory=lambda:FakeSplitterBackend(lambda text:[text]),
            gate_factory=lambda:FakeGateBackend(prediction),verifier=lambda:verify(verify_base_models=False),
            provenance_factory=provenance,session_store=SessionStore(Path(tmp)/'sessions')).start()
        health=app.health();app.shutdown()
    if not health['ready'] or health['model_load_count']!=1:raise RuntimeError('startup_dry_run_failed')
    print(json.dumps({'status':'OK','models_loaded':False,'gpu_used':False,'health':health},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
