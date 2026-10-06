"""Read-only, sanitized public competition demo for SIRA."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from .desktop_app import DesktopControl

DEFAULT_HOST='127.0.0.1'
DEFAULT_PORT=8877
SCHEMA='sira.competition_demo.v1'

def _map(v): return v if isinstance(v, Mapping) else {}
def _clean(v,limit=100):
    if not isinstance(v,str): return None
    v=' '.join(v.split())
    return v[:limit] if v else None
def _count(v): return v if type(v) is int and v>=0 else 0

def build_public_snapshot(root: Path) -> dict[str, object]:
    view=DesktopControl(Path(root).resolve()).overview()
    core=_map(_map(view).get('core'))
    runtime=_map(core.get('runtime') or _map(view).get('runtime'))
    release=_map(_map(view).get('release'))
    if not release: release=_map(core.get('release'))
    knowledge=_map(core.get('knowledge'))
    learning=_map(core.get('learning'))
    providers=_map(core.get('providers'))
    identity=_map(core.get('identity'))
    caps=[]
    rows=core.get('capabilities')
    if isinstance(rows,list):
        for row in rows[:12]:
            if not isinstance(row,Mapping): continue
            name=_clean(row.get('name'),64); state=_clean(row.get('state'),48); scope=_clean(row.get('scope'),96)
            if name and state: caps.append({'name':name,'state':state,'scope':scope})
    research_state='unverified'
    for row in caps:
        if str(row['name']).casefold()=='research': research_state=str(row['state']); break
    task=_map(core.get('last_task'))
    public_task=None
    if task:
        route=_clean(task.get('route'),70)
        verification=('not_applicable_operational_status' if route=='self_status' else _clean(task.get('verification_status'),70))
        public_task={'status':_clean(task.get('status'),40),'route':route,'verification_status':verification}
    required=release.get('checks_required',release.get('required_check_count'))
    passed=release.get('checks_passed',release.get('required_checks_passed'))
    counts_valid=(type(required) is int and type(passed) is int
                  and required>0 and passed==required)
    ready=(release.get('release_ready') is True and counts_valid)
    return {
      'schema':SCHEMA,'generated_at':datetime.now(timezone.utc).isoformat(),
      'identity':{'name':_clean(identity.get('name'),40) or 'SIRA','description':'Persistent agentic AI for verified research, memory, orchestration and evidence-gated improvement.'},
      'runtime':{'state':_clean(runtime.get('effective_state'),24) or 'unknown','health':_clean(runtime.get('state_health'),24) or 'unknown','generation':_count(runtime.get('generation')),'worker_state':_clean(runtime.get('worker_state'),32) or 'unknown','recent_outcome':_clean(core.get('recent_outcome'),80)},
      'release':{'ready':ready,'required_checks':required if ready else None,'passed_checks':passed if ready else None},
      'knowledge':{'verified_count':_count(knowledge.get('verified_count')),'conflicted_count':_count(knowledge.get('conflicted_count')),'stale_sample_count':_count(knowledge.get('stale_sample_count')),'private_content_exposed':False},
      'learning':{'active_goal_count':_count(learning.get('active_goal_count')),'topics_publicly_hidden':True},
      'providers':{'known_count':_count(providers.get('known_count')),'available_count':_count(providers.get('available_count')),'cooling_count':_count(providers.get('cooling_count')),'credentials_publicly_hidden':True},
      'capabilities':caps,
      'research':{'capability_state':research_state,'discovery_is_not_verification':True,'private_evidence_details_hidden':True},
      'last_task':public_task,
      'authority':{'runtime_control_exposed':False,'public_mutation_endpoints':False,'promotion_authorized':bool(core.get('promotion_authorized',False)),'paid_spending_authorized':bool(core.get('paid_spending_authorized',False)),'skill_activated':bool(core.get('skill_activated',False))},
      'privacy':{'api_keys_exposed':False,'filesystem_paths_exposed':False,'source_code_browser_exposed':False,'terminal_or_tool_execution_exposed':False},
    }

def render_html(data):
    r=_map(data.get('runtime')); k=_map(data.get('knowledge')); rel=_map(data.get('release')); p=_map(data.get('providers')); l=_map(data.get('learning')); research=_map(data.get('research')); a=_map(data.get('authority')); privacy=_map(data.get('privacy'))
    caps=data.get('capabilities') if isinstance(data.get('capabilities'),list) else []
    cap_html=''.join('<article class=card><b>'+escape(str(x.get('name','')))+'</b><span>'+escape(str(x.get('state','')).replace('_',' '))+'</span><p>'+escape(str(x.get('scope') or 'Evidence-backed capability state'))+'</p></article>' for x in caps if isinstance(x,Mapping)) or '<article class=card>No public capability rows.</article>'
    task=_map(data.get('last_task'))
    task_html='<p>No public task summary available.</p>' if not task else ''.join('<div class=kv><span>'+escape(label)+'</span><b>'+escape(str(val or '—'))+'</b></div>' for label,val in [('Status',task.get('status')),('Route',task.get('route')),('Verification',task.get('verification_status'))])
    passed=rel.get('passed_checks',0)
    total=rel.get('required_checks',0)
    checks=(f"{passed}/{total} required checks" if total is not None and passed is not None
            else "No acceptance evidence for current revision")
    safety=[('Public mutation endpoints',a.get('public_mutation_endpoints')),('Runtime control exposed',a.get('runtime_control_exposed')),('Promotion authorized',a.get('promotion_authorized')),('Paid spending authorized',a.get('paid_spending_authorized')),('Private memory content exposed',k.get('private_content_exposed')),('API keys exposed',privacy.get('api_keys_exposed')),('Terminal execution exposed',privacy.get('terminal_or_tool_execution_exposed')),('Source-code browser exposed',privacy.get('source_code_browser_exposed'))]
    safety_html=''.join('<div class=safe><span>'+escape(n)+'</span><b class='+('yes' if bool(v) else 'no')+'>'+('YES' if bool(v) else 'NO')+'</b></div>' for n,v in safety)
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><meta name=robots content='noindex,nofollow'><title>SIRA Competition Demo</title><style>
:root{{--bg:#06101a;--panel:#0d1b28;--line:#19384e;--text:#eef8ff;--muted:#93aabd;--cyan:#36e7ff;--good:#67efb3;--bad:#ff728b}}*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 15% 0%,#0b3545,transparent 32%),var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif}}.w{{max-width:1180px;margin:auto;padding:0 24px}}header{{height:80px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #173247}}.brand{{font-weight:900;letter-spacing:.12em}}.brand i{{color:var(--cyan);font-style:normal}}.live{{color:var(--muted);font-size:12px}}.hero{{padding:84px 0 62px;max-width:900px}}.k{{color:var(--cyan);font-size:12px;letter-spacing:.17em;font-weight:800}}h1{{font-size:clamp(42px,6vw,72px);line-height:1.03;letter-spacing:-.045em;margin:14px 0}}h2{{margin:7px 0 0}}p{{color:var(--muted);line-height:1.6}}.notice{{border:1px solid #245b54;background:#0b2928;color:#c0ecda;padding:14px 16px;border-radius:13px;margin-top:24px}}section{{padding:36px 0;border-top:1px solid #132d3e}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:20px}}.metric,.card,.panel,.safe{{background:linear-gradient(150deg,#0f2030,#091722);border:1px solid var(--line);border-radius:16px}}.metric{{padding:18px}}.metric span{{color:var(--muted);font-size:11px;text-transform:uppercase}}.metric b{{display:block;font-size:29px;margin:8px 0;text-transform:capitalize}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:20px}}.card{{padding:17px}}.card b{{display:block}}.card>span{{display:inline-block;color:var(--cyan);font-size:11px;margin-top:7px}}.split{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:20px}}.panel{{padding:20px}}.kv{{display:flex;justify-content:space-between;gap:15px;padding:10px 0;border-top:1px solid #173247}}.kv span{{color:var(--muted)}}.pipe{{display:flex;gap:6px;flex-wrap:wrap;margin-top:16px}}.pipe span{{font-size:12px;padding:7px 9px;border:1px solid #28506a;border-radius:9px}}.sgrid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:20px}}.safe{{padding:14px}}.safe span{{display:block;color:var(--muted);font-size:12px;min-height:30px}}.no{{color:var(--good)}}.yes{{color:var(--bad)}}footer{{padding:32px 0 48px;border-top:1px solid #132d3e;color:var(--muted);font-size:13px}}@media(max-width:850px){{.metrics,.sgrid{{grid-template-columns:repeat(2,1fr)}}.grid{{grid-template-columns:1fr 1fr}}.split{{grid-template-columns:1fr}}}}@media(max-width:560px){{.metrics,.grid,.sgrid{{grid-template-columns:1fr}}}}
</style></head><body><div class=w><header><div class=brand>SIRA <i>LABS</i></div><div class=live>Read-only live snapshot · {escape(str(data.get('generated_at','')))}</div></header><main><div class=hero><div class=k>VERIFIABLE AGENTIC AI</div><h1>Research. Verify. Remember.<br>Improve with evidence.</h1><p>SIRA is a persistent agentic AI system that unifies verified research, persistent memory, bounded orchestration and guarded self-improvement under one core.</p><div class=notice>Sanitized competition surface: no source code, secrets, private memory, terminal access, runtime controls or promotion authority are exposed.</div></div>
<section><div class=k>LIVE SIRA STATE</div><h2>Current bounded snapshot</h2><div class=metrics><div class=metric><span>Runtime</span><b>{escape(str(r.get('state','—')))}</b><small>Health: {escape(str(r.get('health','—')))} · Worker: {escape(str(r.get('worker_state','—')))}</small></div><div class=metric><span>Generation</span><b>{r.get('generation',0)}</b><small>Persistent runtime generation</small></div><div class=metric><span>Verified knowledge</span><b>{k.get('verified_count',0)}</b><small>Conflicted: {k.get('conflicted_count',0)} · stale sample: {k.get('stale_sample_count',0)}</small></div><div class=metric><span>Release evidence</span><b>{'Ready' if rel.get('ready') else 'Not ready'}</b><small>{checks}</small></div></div></section>
<section><div class=k>EVIDENCE-FIRST</div><h2>Capability evidence map</h2><div class=grid>{cap_html}</div></section>
<section><div class=k>ONE SIRA CORE</div><h2>Architecture at a glance</h2><div class=split><div class=panel><b>Research & memory</b><p>Discovery stays separate from verified long-term knowledge. Public view exposes only bounded state, not private claims or evidence.</p><div class=kv><span>Research capability</span><b>{escape(str(research.get('capability_state','unverified')))}</b></div><div class=kv><span>Active learning goals</span><b>{l.get('active_goal_count',0)}</b></div><div class=kv><span>Providers</span><b>{p.get('available_count',0)} available / {p.get('known_count',0)} known</b></div></div><div class=panel><b>Guarded improvement</b><p>Candidate generation is separated from checks, independent evaluation, protected authorization, transactional promotion and rollback.</p><div class=pipe><span>Candidate</span><span>→ Tests</span><span>→ Evaluate</span><span>→ Authorize</span><span>→ Promote</span><span>→ Verify / Roll back</span></div></div></div></section>
<section><div class=k>PUBLIC AUTHORITY</div><h2>Intentionally non-operational</h2><div class=sgrid>{safety_html}</div></section><section><div class=k>RECENT BOUNDED STATE</div><h2>Latest safe-to-show task evidence</h2><div class=panel>{task_html}</div></section></main><footer><b>SIRA Labs</b> · International AI Builders Congress · sanitized competition demo</footer></div></body></html>"""

def _json_bytes(x): return json.dumps(x,ensure_ascii=False,separators=(',',':')).encode()
class _Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,handler,*,root): super().__init__(address,handler); self.root=Path(root).resolve()
class Handler(BaseHTTPRequestHandler):
    server:_Server
    def _send(self,status,body,ctype):
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('X-Frame-Options','DENY'); self.send_header('Referrer-Policy','no-referrer'); self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"); self.end_headers()
        if self.command!='HEAD': self.wfile.write(body)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/healthz': self._send(200,_json_bytes({'status':'ok','schema':SCHEMA}),'application/json'); return
        if path not in {'/','/api/demo/overview'}: self._send(404,_json_bytes({'error':'not_found'}),'application/json'); return
        try: data=build_public_snapshot(self.server.root)
        except (OSError,RuntimeError,ValueError) as exc: self._send(503,_json_bytes({'error':'snapshot_unavailable','detail':type(exc).__name__}),'application/json'); return
        self._send(200,_json_bytes(data),'application/json; charset=utf-8') if path=='/api/demo/overview' else self._send(200,render_html(data).encode(),'text/html; charset=utf-8')
    def do_HEAD(self): self.do_GET()
    def do_POST(self): self._send(405,_json_bytes({'error':'read_only_demo'}),'application/json')
    do_PUT=do_POST; do_PATCH=do_POST; do_DELETE=do_POST
    def log_message(self,fmt,*args): print('[competition-demo] '+(fmt%args))

def run_demo_server(root:Path,*,host=DEFAULT_HOST,port=DEFAULT_PORT):
    if host!=DEFAULT_HOST: raise ValueError('competition demo may bind only to 127.0.0.1')
    with _Server((host,port),Handler,root=Path(root).resolve()) as server:
        print(f'SIRA Competition Demo: http://{host}:{server.server_port}'); print('Read-only sanitized surface. Ctrl+C to stop.'); server.serve_forever(poll_interval=.5)
def main(argv=None):
    p=argparse.ArgumentParser(description='Run SIRA competition demo'); p.add_argument('--root',type=Path,default=Path.cwd()); p.add_argument('--host',default=DEFAULT_HOST); p.add_argument('--port',type=int,default=DEFAULT_PORT); a=p.parse_args(argv); run_demo_server(a.root,host=a.host,port=a.port); return 0
if __name__=='__main__': raise SystemExit(main())
