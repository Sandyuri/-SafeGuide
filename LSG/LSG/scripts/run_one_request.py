#!/usr/bin/env python3
import argparse,json,urllib.request
p=argparse.ArgumentParser();p.add_argument('--url',default='http://127.0.0.1:8765');p.add_argument('--text',required=True);p.add_argument('--dispatch',action='store_true');a=p.parse_args()
body=json.dumps({'text':a.text,'session_id':None,'request_id':None,'trusted_context':None,'dispatch':a.dispatch},ensure_ascii=False).encode()
request=urllib.request.Request(a.url+'/evaluate',data=body,headers={'Content-Type':'application/json'})
with urllib.request.urlopen(request,timeout=180) as response: print(json.dumps(json.load(response),ensure_ascii=False,indent=2))
