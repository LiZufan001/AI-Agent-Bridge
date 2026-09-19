#!/usr/bin/env python3
"""Real loopback read-only Dashboard smoke; never starts Worker or Codex."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
ENGINE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ENGINE/'worker'))
import bridge_dashboard
import state_roots
from dashboard_identity import service_instance_id


def hashes(root: Path) -> dict:
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts and '__pycache__' not in p.parts}


def smoke(root: Path) -> dict:
    root=state_roots.resolve_state_root(root)
    before=hashes(root)
    server=bridge_dashboard.create_server(root,port=0,allow_controls=False)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        host,port=server.server_address
        assert host=='127.0.0.1'
        url=f'http://127.0.0.1:{port}'
        def read(route):
            with urllib.request.urlopen(url+route,timeout=5) as response:return response.read()
        service=json.loads(read('/api/service'))
        health=json.loads(read('/api/health'))
        html=read('/')
        request=urllib.request.Request(url+'/api/control',data=b'{}',method='POST',
            headers={'Origin':url,'Content-Type':'application/json','X-Bridge-Owner':'1'})
        rejected=False
        try:urllib.request.urlopen(request,timeout=5).close()
        except urllib.error.HTTPError as exc:rejected=exc.code==405
        changed=before!=hashes(root)
        return {'pass':not changed and rejected and not service['allow_controls'] and bool(html) and service['root_id']==service_instance_id(root, create=False),
            'bound_address':host,'service_root_matches':service['root_id']==service_instance_id(root, create=False),
            'controls_rejected':rejected,'state_files_unchanged':not changed,'projects_observed':len(health['projects']),
            'observed_severity':health['severity'],'worker_started':False,'codex_started':False}
    finally:
        server.shutdown();thread.join(timeout=5);server.server_close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state-root',type=Path);p.add_argument('--output',type=Path)
    a=p.parse_args()
    try:r=smoke(state_roots.resolve_state_root(a.state_root))
    except (OSError,ValueError,RuntimeError,AssertionError) as exc:r={'pass':False,'error':type(exc).__name__+': '+str(exc)}
    text=json.dumps(r,indent=2)+'\n'
    if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(text)
    print(text,end='');return 0 if r['pass'] else 1
if __name__=='__main__':raise SystemExit(main())
