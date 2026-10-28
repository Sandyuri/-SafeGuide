"""Deterministic session persistence; JSON files are optional for CLI restarts."""
import json
from pathlib import Path
from threading import RLock

class SessionStore:
    def __init__(self, directory=None):
        self.directory=Path(directory).resolve() if directory else None
        self.data={};self.lock=RLock()
        if self.directory:self.directory.mkdir(parents=True,exist_ok=True)

    def _path(self,session_id):
        if not session_id.startswith('lsg-session-') or '/' in session_id or '\\' in session_id:
            raise ValueError('invalid_session_id')
        return self.directory/(session_id+'.json')

    def get(self,session_id):
        with self.lock:
            if self.directory:
                path=self._path(session_id)
                return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
            return json.loads(json.dumps(self.data.get(session_id))) if session_id in self.data else None

    def put(self,record):
        sid=record['session_id']
        with self.lock:
            if self.directory:
                self.directory.mkdir(parents=True,exist_ok=True)
                path=self._path(sid);temp=path.with_suffix('.tmp')
                temp.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
                temp.replace(path)
            else:self.data[sid]=json.loads(json.dumps(record))
