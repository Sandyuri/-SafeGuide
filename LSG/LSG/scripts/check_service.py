#!/usr/bin/env python3
import argparse,json,urllib.request
p=argparse.ArgumentParser();p.add_argument('--url',default='http://127.0.0.1:8765');a=p.parse_args()
for path in ('/health','/model-info'):
    with urllib.request.urlopen(a.url+path,timeout=5) as response:
        print(json.dumps(json.load(response),ensure_ascii=False,indent=2))
