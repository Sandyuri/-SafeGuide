"""Resident LSG application. Import and health are CPU/code-only."""
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from uuid import uuid4
from .config import PROJECT, default_context_path, verify
from .lsg_contract import provenance, validate_schema
from .pipeline import Pipeline
from .orchestrator import LSGOrchestrator
from .session import SessionStore
from .action_adapter import load_department_catalog

class ServiceUnavailable(RuntimeError): pass
class QueueFull(RuntimeError): pass

class LSGApplication:
    def __init__(self, config_path=None, *, splitter_factory=None, gate_factory=None,
                 verifier=None, provenance_factory=None, session_store=None, location_resolver_factory=None):
        path=Path(config_path) if config_path else PROJECT/'configs/runtime.json'
        self.config=json.loads(path.read_text(encoding='utf-8'))
        self.registry=json.loads((PROJECT/self.config['model_registry']).read_text(encoding='utf-8'))
        self.splitter_factory=splitter_factory
        self.gate_factory=gate_factory
        self.location_resolver_factory=location_resolver_factory
        self.verifier=verifier or verify
        self.provenance_factory=provenance_factory or provenance
        self.session_store=session_store or SessionStore(PROJECT/'runs/sessions')
        self.splitter=None;self.gate=None;self.pipeline=None;self.orchestrator=None
        self.catalogs=None;self.default_context=None;self.model_manifest=None
        self._ready=False;self._closed=False;self._model_load_count=0;self._hash_verification_count=0
        self._lifecycle=threading.Lock();self._session_lock=threading.Lock()
        self._slots=threading.BoundedSemaphore(self.config.get('max_queue_length',8))
        self._worker=None

    def _entry(self, name, role):
        entry=self.registry.get(name)
        if not entry or entry.get('role')!=role or not entry.get('enabled'):
            raise ValueError('disabled_or_unknown_backend:'+name)
        if not entry.get('expected_hash') or not entry.get('artifact_path'):
            raise ValueError('incomplete_backend_registry:'+name)
        return entry

    def start(self):
        with self._lifecycle:
            if self._closed: raise ServiceUnavailable('service_closed')
            if self._ready: return self
            splitter_entry=self._entry(self.config['splitter_backend'],'splitter')
            gate_entry=self._entry(self.config['gate_backend'],'gate')
            try:
                manifest=self.verifier()  # real frozen hashes, exactly once at startup
                self._hash_verification_count+=1
                model_provenance=self.provenance_factory()
                if model_provenance.get('splitter_checkpoint_sha256') != splitter_entry['expected_hash'] or model_provenance.get('gate_checkpoint_sha256') != gate_entry['expected_hash']:
                    raise ValueError('registry_checkpoint_hash_mismatch')
                catalog=load_department_catalog()
                default_context=json.loads(default_context_path().read_text(encoding='utf-8'))
                if self.splitter_factory is None:
                    from .backends import InstructSplitterBackend
                    splitter=InstructSplitterBackend()
                else: splitter=self.splitter_factory()
                if self.gate_factory is None:
                    from .backends import EmbeddingGateBackend,InstructGateBackend
                    gate=(EmbeddingGateBackend if self.config['gate_backend']=='embedding_v0.1' else InstructGateBackend)()
                else: gate=self.gate_factory()
                if self.location_resolver_factory is not None:
                    location_resolver=self.location_resolver_factory()
                elif self.splitter_factory is None and self.gate_factory is None:
                    from .location_resolver import APILocationResolver
                    location_resolver=APILocationResolver()
                else:
                    location_resolver=None
                pipeline=Pipeline(splitter,gate,integration_manifest=manifest,
                                  model_provenance=model_provenance,catalogs=catalog,
                                  location_resolver=location_resolver)
                orchestrator=LSGOrchestrator(pipeline,self.session_store)
                worker=ThreadPoolExecutor(max_workers=1,thread_name_prefix='lsg-inference')
            except Exception:
                self.splitter=self.gate=self.pipeline=self.orchestrator=self.catalogs=self.default_context=self.model_manifest=None
                self._ready=False
                raise
            self.splitter=splitter;self.gate=gate;self.pipeline=pipeline
            self.orchestrator=orchestrator;self.catalogs=catalog;self.default_context=default_context
            self.model_manifest={'splitter':splitter_entry,'gate':gate_entry,'verified':manifest,'provenance':model_provenance}
            self._worker=worker;self._ready=True;self._model_load_count+=1
            return self

    def health(self):
        result={'status':'ready' if self._ready else 'stopped' if self._closed else 'not_started',
                'ready':self._ready,'splitter_backend':self.config['splitter_backend'],
                'gate_backend':self.config['gate_backend'],
                'model_load_count':self._model_load_count,
                'hash_verification_count':self._hash_verification_count}
        torch=sys.modules.get('torch')
        if self._ready and torch is not None and torch.cuda.is_available():
            result['cuda_memory_mib']={'allocated':round(torch.cuda.memory_allocated()/1048576,1),
                'reserved':round(torch.cuda.memory_reserved()/1048576,1),
                'peak_allocated':round(torch.cuda.max_memory_allocated()/1048576,1),
                'peak_reserved':round(torch.cuda.max_memory_reserved()/1048576,1)}
        return result

    def model_info(self):
        return {'loaded':self._ready,'splitter':self.registry.get(self.config['splitter_backend']),
                'gate':self.registry.get(self.config['gate_backend']),
                'provenance':self.model_manifest['provenance'] if self._ready else None}

    def _run(self, fn):
        if not self._ready or self._closed:raise ServiceUnavailable('service_not_ready')
        if not self._slots.acquire(blocking=False):raise QueueFull('request_queue_full')
        future=self._worker.submit(self._serialized,fn)
        future.add_done_callback(lambda _:self._slots.release())
        try:return future.result(timeout=self.config.get('request_timeout_seconds',120))
        except FutureTimeout:raise TimeoutError('request_timed_out') from None

    def _serialized(self,fn):
        with self._session_lock:return fn()

    def _present(self,result,external_request_id):
        native=dict(result)
        native['request_id']=external_request_id
        native['intent_results']=[{'intent_index':i+1,'prediction':p,'authorization':native['authorizations'][i] if i<len(native['authorizations']) else None}
                                  for i,p in enumerate(native['per_intent_predictions'])]
        validate_schema('lsg-orchestration-result.schema.json',native)
        legacy={'schema_version':'lsg.legacy-compatible.v0.1','decision_source':'LEARNED_SAFEGATE',
                'request_id':external_request_id,'session_id':native['session_id'],
                'aggregate_decision':native['aggregate_decision'],
                'intent_results':native['intent_results'],'dispatch_plan':native['dispatch_plan'],
                'context_snapshot_hash':native['context_snapshot_hash'],
                'splitter_version':self.config['splitter_backend'],'gate_version':self.config['gate_backend'],
                'splitter_hash':native['provenance'].get('splitter_checkpoint_sha256'),
                'gate_hash':native['provenance'].get('gate_checkpoint_sha256'),
                'fail_closed_reason':native['fail_closed_reason']}
        validate_schema('lsg-legacy-compatible-result.schema.json',legacy)
        return {'native_result':native,'legacy_compatible_result':legacy}

    def evaluate(self,text,*,session_id=None,request_id=None,trusted_context=None,dispatch=False):
        if session_id is not None:raise ValueError('use_continue_for_session')
        if not isinstance(text,str) or not text.strip():raise ValueError('empty_input')
        context=trusted_context if trusted_context is not None else self.default_context
        external_id=request_id or 'lsg-external-'+str(uuid4())
        return self._run(lambda:self._present(self.orchestrator.start(text,context,dispatch=dispatch),external_id))

    def continue_session(self,session_id,supplements,*,request_id=None,trusted_context=None,dispatch=False):
        if not session_id:raise ValueError('missing_session_id')
        context=trusted_context if trusted_context is not None else self.default_context
        external_id=request_id or 'lsg-external-'+str(uuid4())
        return self._run(lambda:self._present(self.orchestrator.resume(session_id,supplements,context,dispatch=dispatch),external_id))

    def session(self,session_id):
        if not self._ready:raise ServiceUnavailable('service_not_ready')
        record=self.session_store.get(session_id)
        if record is None:raise ValueError('unknown_session_id')
        return record

    def shutdown(self):
        with self._lifecycle:
            self._ready=False;self._closed=True
            if self._worker:self._worker.shutdown(wait=True,cancel_futures=True)
            self._worker=None;self.pipeline=None;self.orchestrator=None
            self.splitter=None;self.gate=None;self.catalogs=None;self.default_context=None
