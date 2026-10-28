#!/usr/bin/env python3
"""Prepared local HTTP service. Do not run during code-only integration."""
import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent))
from LSG.runtime.application import LSGApplication

class Handler(BaseHTTPRequestHandler):
    app=None
    def log_message(self,format,*args):
        # No request bodies or credentials in access logs.
        pass
    def reply(self,code,obj):
        body=json.dumps(obj,ensure_ascii=False,default=str).encode()
        self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_GET(self):
        try:
            if self.path=='/health':result=self.app.health()
            elif self.path=='/model-info':result=self.app.model_info()
            elif self.path.startswith('/sessions/'):
                result=self.app.session(self.path.removeprefix('/sessions/'))
            else:return self.reply(404,{'error':'not_found'})
            self.reply(200,result)
        except Exception as error:self.reply(400,{'error':type(error).__name__,'reason':str(error)})
    def do_POST(self):
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length>65536:raise ValueError('request_too_large')
            data=json.loads(self.rfile.read(length))
            if self.path=='/evaluate':
                result=self.app.evaluate(data['text'],session_id=data.get('session_id'),request_id=data.get('request_id'),trusted_context=data.get('trusted_context'),dispatch=data.get('dispatch',False))
            elif self.path=='/continue':
                result=self.app.continue_session(data['session_id'],{int(k):v for k,v in data['supplements'].items()},request_id=data.get('request_id'),trusted_context=data.get('trusted_context'),dispatch=data.get('dispatch',False))
            else:return self.reply(404,{'error':'not_found'})
            self.reply(200,result)
        except Exception as error:self.reply(400,{'error':type(error).__name__,'reason':str(error)})

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',default=str(ROOT/'configs/runtime.json'))
    args=parser.parse_args()
    started=time.perf_counter()
    app=LSGApplication(args.config).start()
    startup_ms=round((time.perf_counter()-started)*1000)
    Handler.app=app
    host=app.config.get('listen_host','127.0.0.1');port=app.config.get('listen_port',8765)
    server=ThreadingHTTPServer((host,port),Handler)
    info=app.model_info()
    print(json.dumps({'pid':os.getpid(),'startup_ms':startup_ms,'splitter_backend':app.config['splitter_backend'],
                      'gate_backend':app.config['gate_backend'],'splitter_hash':info['provenance']['splitter_checkpoint_sha256'],
                      'gate_hash':info['provenance']['gate_checkpoint_sha256'],'device':os.environ.get('LSG_DEVICE','auto'),
                      'health_url':f'http://{host}:{port}/health'},ensure_ascii=False),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();app.shutdown()
if __name__=='__main__':main()
