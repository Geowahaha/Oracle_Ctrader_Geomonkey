#!/usr/bin/env python3
"""dpull vs dpull-cs forward A/B — one-command judge. Run on the VM:
    python3 /opt/dexter_pro/ops/dpull_ab.py [YYYY-MM-DD]
Groups broker deals by label, reports realized PnL / N / WR / PF per lane,
plus how the exits split (close_stop vs broker_side vs convex_trail).
Default 'since' = the dpull-cs launch day (2026-07-23); pass a date to widen.
(Owner choice ค 2026-07-23: forward realized PnL is the arbiter of the
vol-gated close-stop edge — promote/kill dpull-cs on these numbers.)"""
import os, sys, datetime
os.environ.setdefault('DEXTER3_TRANSPORT','openapi')
os.environ.setdefault('DEXTER3_OPENAPI_DAEMON_URL','http://127.0.0.1:9877')
os.environ.setdefault('DEXTER3_OPENAPI_DAEMON_TIMEOUT_SEC','90')
sys.path.insert(0,'/opt/dexter_pro')
from dexter3.transport import make_client
from collections import defaultdict
import sqlite3, json

SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-07-23"   # dpull-cs launch day
from_ms = int(datetime.datetime.strptime(SINCE, "%Y-%m-%d")
              .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
mcp = make_client()
deals = mcp.get_deals(count=3000, from_timestamp_ms=from_ms) or []
pos = defaultdict(lambda: {'net':0.0,'label':'?','closed':False})
for d in sorted(deals, key=lambda x: x.get('executionTimestamp',0)):
    pid=d.get('positionId')
    if not pid: continue
    p=pos[pid]; lbl=str(d.get('label') or '')
    if lbl and p['label']=='?': p['label']=lbl
    if d.get('hasCloseDetail'):
        p['closed']=True; p['net']+=float(d.get('netProfit') or 0.0)

def stats(sub):
    ts=[p for p in pos.values() if sub in p['label'] and p['closed']]
    n=len(ts); net=sum(p['net'] for p in ts); w=sum(1 for p in ts if p['net']>0)
    gp=sum(p['net'] for p in ts if p['net']>0); gl=-sum(p['net'] for p in ts if p['net']<0)
    pf=(gp/gl) if gl>0 else (float('inf') if gp>0 else 0.0)
    return n,net,w,pf

print(f'=== dpull FORWARD A/B (deals since {SINCE}) ===')
for name,sub in (('dpull-base ','dexter3:dpull:'),('dpull-cs   ','dexter3:dpull-cs:')):
    n,net,w,pf=stats(sub)
    print(f'  {name}: N={n:3d}  net=${net:+8.2f}  WR={(100*w/n if n else 0):5.1f}%  PF={pf:.2f}')

c=sqlite3.connect('/opt/dexter_pro/data/runtime/dexter3_journal.db')
print(f'=== exit-reason split (journal, since {SINCE}) ===')
for name,sub in (('dpull-base','dexter3:dpull:'),('dpull-cs  ','dexter3:dpull-cs:')):
    cur=c.execute("SELECT payload_json FROM exec_events WHERE event='lane_position_closed' AND ts>=? AND payload_json LIKE ?", (SINCE, '%'+sub+'%'))
    by=defaultdict(int)
    for (pj,) in cur.fetchall():
        d=json.loads(pj or '{}'); by[str(d.get('exit_reason') or '?')]+=1
    print(f'  {name}: {dict(by) or "no closes yet"}')
print('NOTE: the close-stop edge shows as dpull-cs having convex_close_stop /')
print('fewer broker_side_close exits than base, with higher net/PF over time.')
