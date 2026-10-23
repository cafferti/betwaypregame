"""
Trading-style dashboard written to a plain HTML file (standard library only).

  - Profit and ROI curves: built from placed_bets_log.csv, plotted against bet date,
    with a time-range selector (hours -> days) and drag-to-zoom
  - Sidebar tracker: every Pinnacle alert the streamer receives is recorded as a price tick

The bot rewrites  dashboard.html  (next to this script) every few seconds.
Open that file in your browser; it reloads itself every 15 seconds.
Odds history lives in memory, so it starts empty after a restart.
"""

import csv
import json
import os
import threading
import time
from collections import deque
from datetime import datetime

import controls

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BET_LOG_CSV = os.path.join(SCRIPT_DIR, "placed_bets_log.csv")
DASHBOARD_HTML = os.path.join(SCRIPT_DIR, "dashboard.html")
DATA_JSON = os.path.join(
    SCRIPT_DIR, "dashboard_data.json"
)  # live feed read by the controller
PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))

WRITE_INTERVAL_SEC = 5
PAGE_REFRESH_SEC = 15
TICKS_PER_MARKET = 600
MAX_MARKETS = 20000  # effectively "every game"; oldest-updated is dropped beyond this

_lock = threading.Lock()
_markets = {}  # key -> {"meta": {...}, "pts": deque[(ts_ms, price)]}
_writer_started = False
_fields_printed = False

# If the Pinnacle alert carries the price it moved FROM, the first alert for a market
# already shows its drop. (Field name isn't known for sure, so a few are tried.)
_PREV_KEYS = (
    "changeFrom",
    "changedFrom",
    "previousPrice",
    "prevPrice",
    "oldPrice",
    "priceFrom",
)


def _prev_price(e: dict):
    for k in _PREV_KEYS:
        try:
            v = float(e.get(k))
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return None


# ============================================================
# ODDS TICKS (fed by pinnacleretriever)
# ============================================================


def record_tick(e: dict):
    """Store one Pinnacle alert as a price tick. Never raises."""
    global _fields_printed
    if not _fields_printed:
        _fields_printed = True
        try:
            print(f"[dashboard] Pinnacle alert fields: {sorted(e.keys())}")
        except Exception:
            pass
    try:
        price = float(e.get("changeTo"))
    except (TypeError, ValueError):
        return
    try:
        ts = int(e.get("timestamp") or 0) or int(time.time() * 1000)
    except (TypeError, ValueError):
        ts = int(time.time() * 1000)

    key = "|".join(
        str(e.get(k))
        for k in ("eventId", "lineType", "points", "outcome", "periodNumber")
    )
    prev = _prev_price(e)
    with _lock:
        m = _markets.get(key)
        is_new = m is None
        if m is None:
            if len(_markets) >= MAX_MARKETS:
                oldest = min(
                    _markets,
                    key=lambda k: (
                        _markets[k]["pts"][-1][0] if _markets[k]["pts"] else 0
                    ),
                )
                _markets.pop(oldest, None)
            m = {
                "meta": {
                    "key": key,
                    "home": e.get("home"),
                    "away": e.get("away"),
                    "league": e.get("leagueName"),
                    "lineType": e.get("lineType"),
                    "points": e.get("points"),
                    "outcome": e.get("outcome"),
                    "period": e.get("periodNumber"),
                },
                "pts": deque(maxlen=TICKS_PER_MARKET),
            }
            _markets[key] = m
        if is_new and prev is not None and abs(prev - price) > 1e-9:
            m["pts"].append((ts - 1, prev))
        m["pts"].append((ts, price))
        m["meta"]["seen"] = int(time.time() * 1000)  # when the bot received it


def _odds_payload():
    with _lock:
        items = sorted(
            (m for m in _markets.values() if m["pts"]),
            key=lambda m: m["pts"][-1][0],
            reverse=True,
        )
        out = []
        for m in items:
            d = dict(m["meta"])
            d["pts"] = [[t, p] for t, p in m["pts"]]
            out.append(d)
    return {"markets": out}


# ============================================================
# BETS (raw rows; the page builds the curves for whatever time range is chosen)
# ============================================================


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pnl_payload():
    bets = []
    pending = 0
    pending_uncat = 0
    pending_by = {k: 0 for k in controls.KEYS}
    try:
        with open(BET_LOG_CSV, "r", newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        rows = []

    for r in rows:
        # Rows with extra/missing cells give a None key or a list/None value; ignore those.
        r = {
            k.strip(): (v.strip() if isinstance(v, str) else "")
            for k, v in r.items()
            if isinstance(k, str)
        }
        if r.get("Date", "").upper() == "TOTAL":
            continue
        cat = controls.category_of(
            r.get("Sport", "").lower(), r.get("Market Type", ""), r.get("Outcome", "")
        )
        if r.get("Status", "").lower() == "pending":
            pending += 1
            if cat:
                pending_by[cat] += 1
            else:
                pending_uncat += 1
            continue
        profit = _f(r.get("Profit"))
        if profit is None:
            continue
        try:
            ts = (
                datetime.strptime(r.get("Date", ""), "%Y-%m-%d %H:%M:%S").timestamp()
                * 1000
            )
        except ValueError:
            continue  # can't place it on a time axis
        kind = "reversed" if r.get("EV %", "").lower().startswith("r") else "normal"
        bets.append(
            {
                "t": ts,
                "p": profit,
                "s": _f(r.get("Stake")) or 0.0,
                "o": _f(r.get("Bookie Odds")) or 0.0,
                "c": cat,
                "k": kind,
            }
        )

    bets.sort(key=lambda b: b["t"])
    return {
        "bets": bets,
        "pending": pending,
        "pending_by": pending_by,
        "pending_uncat": pending_uncat,
    }


# ============================================================
# HTML PAGE (data is embedded, so it works by double-clicking the file)
# ============================================================

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bot dashboard</title>
<style>
:root{--bg:#0b0e14;--panel:#131722;--line:#232a3a;--txt:#d1d4dc;--mut:#7d8597;--up:#26a69a;--down:#ef5350}
*{box-sizing:border-box}
html,body{height:100%;margin:0;overflow:hidden}
body{display:flex;flex-direction:column;background:var(--bg);color:var(--txt);font:13px/1.4 Segoe UI,Arial,sans-serif}
header{flex:none;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:15px;margin:0;font-weight:700}
.dot{width:8px;height:8px;border-radius:50%;background:var(--up);display:inline-block;margin-right:6px}
.dot.off{background:var(--down)}
.rng{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.rng button{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:4px;padding:3px 9px;cursor:pointer;font:inherit}
.rng button.act{background:#1e2740;border-color:#4fc3f7;color:#fff}
.rng input{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:4px;padding:2px 6px;font:inherit;color-scheme:dark}
.hint{color:var(--mut);font-size:11px}
.content{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) 330px}
.main{min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:8px;padding:8px}
.panel{flex:none;display:flex;flex-direction:column;background:var(--panel);border:1px solid var(--line);border-radius:6px}
.ph{flex:none;display:flex;flex-wrap:wrap;align-items:center;gap:4px 16px;padding:6px 12px;border-bottom:1px solid var(--line)}
.ph h2{font-size:13px;margin:0;font-weight:700}
.cw{flex:none;height:270px;position:relative}
.cw canvas{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:crosshair}
.cards{display:flex;flex-wrap:wrap;gap:4px 20px}
.card small{color:var(--mut);font-size:10px;text-transform:uppercase;letter-spacing:.4px;margin-right:5px}
.card b{font-size:14px}
.pos{color:var(--up)}.neg{color:var(--down)}
.tog label{margin-right:10px;cursor:pointer;color:var(--mut)}
.tog input{vertical-align:middle}
.side{min-height:0;display:flex;flex-direction:column;border-left:1px solid var(--line);background:var(--panel)}
.sh{flex:none;display:flex;justify-content:space-between;align-items:baseline;padding:8px 12px;border-bottom:1px solid var(--line)}
.sh h2{font-size:13px;margin:0;font-weight:700}
.sctl{flex:none;display:flex;gap:6px;padding:8px 10px;border-bottom:1px solid var(--line)}
.sctl input,.sctl select{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:4px;padding:5px 7px;font:inherit}
.sctl input{flex:1;min-width:0}
.list{flex:1;min-height:0;overflow-y:auto;position:relative}
.row{padding:7px 12px;border-bottom:1px solid var(--line);cursor:pointer;display:flex;justify-content:space-between;gap:8px;overflow:hidden}
.row.new{animation:rowin .55s cubic-bezier(.2,.8,.2,1) both,rowflash 4s ease-out both}
.row.new.still{animation:rowflash 4s ease-out both}
.row.new.up{--flash:#17403a}.row.new.dn{--flash:#4a2227}.row.new{--flash:#243252}
@keyframes rowin{from{opacity:0;max-height:0;padding-top:0;padding-bottom:0;transform:translateY(-16px)}to{opacity:1;max-height:80px;padding-top:7px;padding-bottom:7px;transform:none}}
@keyframes rowflash{from{background:var(--flash)}to{background:transparent}}
.arr{font-size:13px;font-weight:700}
.dropf{flex:none;display:flex;align-items:center;gap:5px;padding:6px 10px;border-bottom:1px solid var(--line)}
.dropf button{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:4px;padding:3px 8px;cursor:pointer;font:inherit}
.dropf button.act{background:#4a2227;border-color:var(--down);color:#fff}
.dropf input{width:54px;background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:4px;padding:3px 5px;font:inherit}
.row:hover{background:#1a2030}.row.sel{background:#1e2740}
.row .t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:190px}
.row .s{color:var(--mut);font-size:11px}
.row .r{text-align:right;white-space:nowrap}
.hd .t{font-size:14px;font-weight:700}.hd .s{color:var(--mut);font-size:12px}
.row1,.row2{display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px;padding:7px 14px}
.row2{border-top:1px solid var(--line);gap:8px 26px}
.botbtn{margin-left:auto;font:inherit;font-weight:700;padding:6px 16px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--txt);cursor:pointer}
.botbtn.stop{background:#4a2227;border-color:var(--down);color:#fff}
.botbtn.start{background:#17403a;border-color:var(--up);color:#fff}
.botbtn:disabled{opacity:.5;cursor:not-allowed}
.bstat{font-weight:700;font-size:12px;letter-spacing:.4px}
.bstat.run{color:var(--up)}.bstat.stop{color:var(--down)}.bstat.off{color:var(--mut)}.bstat.mid{color:#ffd54f}
.grp{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.grp>b{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.4px;margin-right:2px}
.mchip{display:inline-flex;align-items:center;gap:6px;padding:3px 11px 3px 8px;border:1px solid var(--line);border-radius:14px;cursor:pointer;background:var(--bg);user-select:none}
.mchip i{width:10px;height:10px;border-radius:50%;background:var(--c)}
.mchip input{margin:0;cursor:pointer}
.mchip.on{background:#161c2b}
.mchip:not(.on){opacity:.6}
.mchip.dis,.mchip.dis input{cursor:not-allowed}.mchip.dis{opacity:.4}
.sbody{display:grid;grid-template-columns:minmax(250px,26%) minmax(0,1fr)}
.bars{border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:330px}
.pills{flex:none;display:flex;flex-wrap:wrap;gap:5px;padding:6px 10px}
.pills button{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:12px;padding:2px 10px;cursor:pointer;font:inherit}
.pills button.act{background:#1e2740;border-color:#4fc3f7;color:#fff}
.cw2{flex:1;min-height:0;position:relative}
.cw2 canvas{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:pointer}
.twrap{overflow:hidden;min-width:0}
table.st{border-collapse:collapse;width:100%;font-size:12px}
.st th{background:#1a2030;text-align:right;padding:6px 6px;cursor:pointer;white-space:nowrap;user-select:none}
.st th:first-child,.st td:first-child{text-align:left}
.st th.sorted::after{content:attr(data-arrow);margin-left:3px;color:#4fc3f7}
.st td{padding:5px 6px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap;position:relative}
.st tr.srow{cursor:pointer}.st tr.srow:hover td{background:#1a2030}
.st tr.foc td{background:#1e2740}
.st tr.off td{color:var(--mut)}
.st tr.tot td{font-weight:700;border-bottom:2px solid var(--line)}
.st .nm{display:flex;align-items:center;gap:7px}
.st .nm i{width:9px;height:9px;border-radius:50%;flex:none}
.sw{padding:0 7px;border-radius:9px;font-size:10px;border:1px solid var(--line);cursor:pointer;margin-left:4px}
.sw.on{background:#17403a;border-color:var(--up);color:#fff}.sw.offb{background:#2a2f3d}
.cellbar::before{content:"";position:absolute;left:0;top:3px;bottom:3px;width:var(--w);background:var(--bc);opacity:.22;border-radius:2px}
.cellbar span{position:relative}
select.sel{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:4px;padding:3px 7px;font:inherit}
.toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#4a2227;border:1px solid var(--down);padding:7px 16px;border-radius:6px;z-index:9;display:none}
@media(max-width:1650px){.sbody{grid-template-columns:1fr}.bars{border-right:0;border-bottom:1px solid var(--line);min-height:0;height:250px}}
@media(max-width:900px){
  html,body{overflow:auto;height:auto}
  .content{grid-template-columns:1fr}
  .main{grid-template-rows:none}
  .cw{height:260px;flex:none}
  .side{height:420px;border-left:0;border-top:1px solid var(--line)}
}
</style></head><body>
<header>
  <div class="row1">
    <h1>Bot dashboard</h1>
    <span id="bstat" class="bstat off">○ CONTROLS OFFLINE</span>
    <span><span id="dot" class="dot"></span><span id="status"></span></span>
    <div class="rng" id="rng">
      <button data-r="1h">1H</button><button data-r="6h">6H</button><button data-r="24h">24H</button>
      <button data-r="3d">3D</button><button data-r="7d">7D</button><button data-r="all">ALL</button>
      <input type="datetime-local" id="rfrom"><span>→</span><input type="datetime-local" id="rto">
      <button id="rapply">Apply</button>
      <span class="hint">tip: drag across a chart to zoom</span>
    </div>
    <button id="botbtn" class="botbtn" disabled>Start / Stop</button>
  </div>
  <div class="row2" id="ctl"></div>
</header>
<div class="content">
  <div class="main">
    <section class="panel">
      <div class="ph"><h2>Profit (₦) by bet date</h2>
        <label class="tog"><input type="checkbox" id="t_all"> <span style="color:#4fc3f7">All bets</span></label>
        <span class="hint">one line per switched-on market</span>
        <div class="cards" id="cards"></div></div>
      <div class="cw"><canvas id="pnl"></canvas></div>
    </section>
    <section class="panel">
      <div class="ph"><h2>Market statistics</h2>
        <span class="hint">click a column to sort · click a row or bar to highlight its line · hover a bar for detail</span>
        <span class="hint" id="srange"></span></div>
      <div class="sbody">
        <div class="bars"><div class="pills" id="pills"></div><div class="cw2"><canvas id="bars"></canvas></div></div>
        <div class="twrap"><table class="st" id="stt"></table></div>
      </div>
    </section>
    <section class="panel">
      <div class="ph"><h2>ROI % by bet date</h2>
        <select id="roisel" class="sel"></select>
        <span class="hint" id="roihint"></span>
        <div class="cards" id="roicards"></div></div>
      <div class="cw"><canvas id="roi"></canvas></div>
    </section>
    <section class="panel">
      <div class="ph hd" id="ohead"></div>
      <div class="cw"><canvas id="odds"></canvas></div>
    </section>
  </div>
  <aside class="side">
    <div class="sh"><h2>Pinnacle odds tracker</h2><span class="hint" id="ocount"></span></div>
    <div class="sctl">
      <input id="filter" placeholder="Filter team / league / market">
      <select id="sort"><option value="recent">Recent</option><option value="drop">Biggest drop</option><option value="move">Biggest move</option><option value="ticks">Most ticks</option></select>
    </div>
    <div class="dropf">
      <button id="dtog" title="Show only alerts whose latest move was a drop of at least this much">Drops ≥</button>
      <input id="dmin" type="number" min="0" step="0.5" value="2"><span>%</span>
      <button class="q" data-d="2">2</button><button class="q" data-d="3">3</button>
      <button class="q" data-d="4">4</button><button class="q" data-d="5">5</button>
    </div>
    <div class="list" id="mlist"></div>
  </aside>
</div>
<div class="toast" id="toast"></div>
<script>
let DATA=__DATA__;
const $=id=>document.getElementById(id);
const UP='#26a69a',DOWN='#ef5350';
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function store(k,v){try{localStorage.setItem('bd_'+k,v);}catch(e){}}
function load(k,d){try{const v=localStorage.getItem('bd_'+k);return v===null?d:v;}catch(e){return d;}}
function toLocalInput(ms){const d=new Date(ms);return new Date(ms-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
function niceTime(ms){return new Date(ms).toTimeString().slice(0,8);}
function niceDate(ms){const d=new Date(ms);
  return d.toLocaleDateString(undefined,{month:'short',day:'numeric'})+' '+d.toTimeString().slice(0,5);}
function fitCanvas(c){const r=c.getBoundingClientRect(),d=window.devicePixelRatio||1;
  c.width=Math.max(1,r.width*d);c.height=Math.max(1,r.height*d);
  const x=c.getContext('2d');x.setTransform(d,0,0,d,0,0);return {ctx:x,w:r.width,h:r.height};}

// ---------- generic trading-style line chart ----------
function draw(c){
  const st=c._st; const {ctx,w,h}=fitCanvas(c);
  ctx.clearRect(0,0,w,h);
  ctx.font='11px Segoe UI,Arial';
  const L=10,R=66,T=8,B=20,pw=w-L-R,ph=h-T-B;
  const series=(st&&st.series||[]).filter(s=>s.pts.length);
  if(!series.length){ctx.fillStyle='#7d8597';ctx.fillText((st&&st.opts&&st.opts.empty)||'No data yet',L+10,T+20);return;}
  const o=st.opts;
  let xs=[],ys=[];
  series.forEach(s=>s.pts.forEach(p=>{xs.push(p[0]);ys.push(p[1]);}));
  let x0=o.xmin!=null?o.xmin:Math.min(...xs),x1=o.xmax!=null?o.xmax:Math.max(...xs);
  if(x0>=x1)x1=x0+1;
  let y0=Math.min(...ys),y1=Math.max(...ys); if(o.zero){y0=Math.min(y0,0);y1=Math.max(y1,0);}
  if(y0===y1){const a=Math.abs(y0)*0.05||1;y0-=a;y1+=a;}
  const pad=(y1-y0)*0.08;y0-=pad;y1+=pad;
  const X=v=>L+(v-x0)/(x1-x0)*pw, Y=v=>T+(1-(v-y0)/(y1-y0))*ph;
  st.geom={L,pw,x0,x1};
  ctx.strokeStyle='#232a3a';ctx.fillStyle='#7d8597';ctx.lineWidth=1;
  for(let i=0;i<=4;i++){const v=y0+(y1-y0)*i/4,y=Y(v);
    ctx.beginPath();ctx.moveTo(L,y);ctx.lineTo(L+pw,y);ctx.stroke();
    ctx.fillText(o.yfmt(v),L+pw+6,y+4);}
  for(let i=0;i<=4;i++){const v=x0+(x1-x0)*i/4,x=X(v);
    ctx.beginPath();ctx.moveTo(x,T);ctx.lineTo(x,T+ph);ctx.stroke();
    const t=o.xfmt(v,x1-x0),tw=ctx.measureText(t).width;
    ctx.fillText(t,Math.min(Math.max(x-tw/2,L),L+pw-tw),h-5);}
  if(o.zero){ctx.setLineDash([4,4]);ctx.strokeStyle='#4a5470';
    ctx.beginPath();ctx.moveTo(L,Y(0));ctx.lineTo(L+pw,Y(0));ctx.stroke();ctx.setLineDash([]);}
  series.forEach(s=>{
    ctx.globalAlpha=s.dim?0.22:1;
    const n=s.pts.length;
    const col=(o.seg&&n>1)?(s.pts[n-1][1]>=s.pts[n-2][1]?o.seg.up:o.seg.down):s.color;
    const path=()=>{ctx.beginPath();
      s.pts.forEach((p,i)=>{const x=X(p[0]),y=Y(p[1]);
        if(i===0)ctx.moveTo(x,y);
        else{ if(o.step)ctx.lineTo(x,Y(s.pts[i-1][1])); ctx.lineTo(x,y);} });
      if(o.step){const lp=s.pts[n-1];ctx.lineTo(X(x1),Y(lp[1]));}};
    if(s.area){path();
      const lx=o.step?X(x1):X(s.pts[n-1][0]);
      const base=Y(o.zero?0:y0);
      ctx.lineTo(lx,base);ctx.lineTo(X(s.pts[0][0]),base);ctx.closePath();
      const g=ctx.createLinearGradient(0,T,0,T+ph);
      g.addColorStop(0,col+'55');g.addColorStop(1,col+'05');ctx.fillStyle=g;ctx.fill();}
    if(o.seg){ctx.lineWidth=1.8;ctx.lineJoin='round';
      for(let i=1;i<n;i++){ctx.beginPath();
        ctx.moveTo(X(s.pts[i-1][0]),Y(s.pts[i-1][1]));ctx.lineTo(X(s.pts[i][0]),Y(s.pts[i][1]));
        ctx.strokeStyle=s.pts[i][1]>=s.pts[i-1][1]?o.seg.up:o.seg.down;ctx.stroke();}
    }else{path();ctx.strokeStyle=col;ctx.lineWidth=1.8;ctx.lineJoin='round';ctx.stroke();}
    const lp=s.pts[n-1],yy=Y(lp[1]),txt=o.yfmt(lp[1]);
    ctx.setLineDash([2,3]);ctx.strokeStyle=col;ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(X(lp[0]),yy);ctx.lineTo(L+pw,yy);ctx.stroke();ctx.setLineDash([]);
    ctx.fillStyle=col;ctx.fillRect(L+pw+1,yy-9,R-4,18);
    ctx.fillStyle='#0b0e14';ctx.font='bold 11px Segoe UI,Arial';ctx.fillText(txt,L+pw+5,yy+4);
    ctx.font='11px Segoe UI,Arial';
    ctx.globalAlpha=1;
  });
  if(st.drag!=null&&st.mx!=null){
    ctx.fillStyle='#4fc3f733';ctx.fillRect(Math.min(st.drag,st.mx),T,Math.abs(st.mx-st.drag),ph);}
  if(st.mx!=null&&st.mx>=L&&st.mx<=L+pw){
    const xv=x0+(st.mx-L)/pw*(x1-x0);
    ctx.strokeStyle='#8a93a8';ctx.setLineDash([3,3]);
    ctx.beginPath();ctx.moveTo(st.mx,T);ctx.lineTo(st.mx,T+ph);ctx.stroke();ctx.setLineDash([]);
    const lines=[o.xfmt(xv,0)];
    series.forEach(s=>{let best=s.pts[0];
      for(const p of s.pts){if(p[0]<=xv)best=p;else break;}
      ctx.fillStyle=s.color;ctx.beginPath();ctx.arc(X(best[0]),Y(best[1]),3.5,0,7);ctx.fill();
      lines.push(s.name+': '+o.yfmt(best[1]));});
    const bw=Math.max(...lines.map(t=>ctx.measureText(t).width))+14,bh=lines.length*15+8;
    const bx=st.mx>L+pw/2?st.mx-bw-10:st.mx+10;
    ctx.fillStyle='#1e2740ee';ctx.fillRect(bx,T+4,bw,bh);
    ctx.fillStyle='#d1d4dc';lines.forEach((t,i)=>ctx.fillText(t,bx+7,T+19+i*15));
  }
}
function setChart(c,series,opts){
  const prev=c._st;
  c._st={series,opts,mx:prev?prev.mx:null,drag:null};
  if(!c._bound){c._bound=true;
    c.addEventListener('mousemove',e=>{c._st.mx=e.offsetX;draw(c);});
    c.addEventListener('mouseleave',()=>{c._st.mx=null;c._st.drag=null;draw(c);});
    c.addEventListener('mousedown',e=>{if(c._st.opts.onZoom)c._st.drag=e.offsetX;});
    c.addEventListener('mouseup',e=>{const s=c._st;
      if(s.drag!=null&&s.geom&&s.opts.onZoom&&Math.abs(e.offsetX-s.drag)>8){
        const g=s.geom,f=px=>g.x0+(Math.min(Math.max(px,g.L),g.L+g.pw)-g.L)/g.pw*(g.x1-g.x0);
        const a=f(Math.min(s.drag,e.offsetX)),b=f(Math.max(s.drag,e.offsetX));
        s.drag=null;s.opts.onZoom(a,b);
      }else{s.drag=null;draw(c);}});}
  draw(c);
}

// ---------- time range (shared by profit + ROI) ----------
const RANGES={'1h':3600e3,'6h':6*3600e3,'24h':24*3600e3,'3d':3*86400e3,'7d':7*86400e3};
let range=load('range','all');
$('rfrom').value=load('rfrom','');$('rto').value=load('rto','');
function getWin(){
  if(range==='all')return [null,null];
  if(range==='custom'){
    const a=$('rfrom').value?new Date($('rfrom').value).getTime():null;
    const b=$('rto').value?new Date($('rto').value).getTime():null;
    return [isNaN(a)?null:a,isNaN(b)?null:b];}
  const now=Date.now();return [now-RANGES[range],now];
}
function markRange(){
  document.querySelectorAll('#rng button[data-r]').forEach(b=>b.classList.toggle('act',b.dataset.r===range));
  $('rapply').classList.toggle('act',range==='custom');
}
document.querySelectorAll('#rng button[data-r]').forEach(b=>b.onclick=()=>{
  range=b.dataset.r;store('range',range);renderAll();});
$('rapply').onclick=()=>{range='custom';store('range',range);
  store('rfrom',$('rfrom').value);store('rto',$('rto').value);renderAll();};
function zoomTo(a,b){range='custom';
  $('rfrom').value=toLocalInput(a);$('rto').value=toLocalInput(b);
  store('range',range);store('rfrom',$('rfrom').value);store('rto',$('rto').value);renderAll();}
function xfmt(v,span){
  const d=new Date(v);
  if(span===0)return niceDate(v)+':'+String(d.getSeconds()).padStart(2,'0');
  return span<36*3600e3?d.toTimeString().slice(0,5):niceDate(v);}

// ---------- bet categories, market switches, bot Start/Stop ----------
const CATS=DATA.cats;
const ALLC='#4fc3f7';
let controls=Object.assign({},DATA.controls.markets);
let botRunning=null, stopping=false, apiOk=false, sig='';
const API=(location.protocol==='file:')?('http://127.0.0.1:'+DATA.api.port):'';
const TOKEN=window.__TOKEN__||DATA.api.token;
async function api(path,body){
  const opt={headers:{'X-Token':TOKEN}};
  if(body){opt.method='POST';opt.headers['Content-Type']='text/plain';opt.body=JSON.stringify(body);}
  const r=await fetch(API+path,opt);
  if(!r.ok)throw new Error('HTTP '+r.status);
  return r.json();
}
function toast(msg){const t=$('toast');t.textContent=msg;t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',3500);}
function renderControls(){
  const groups=[['basketball','Basketball'],['football','Football']];
  const tip=apiOk?'Ticked = the bot places these bets and their line is shown. Unticked = the bot stops placing them and the line is hidden.'
                 :'Controller not reachable — open http://127.0.0.1:8765 while python main.py is running';
  $('ctl').innerHTML=groups.map(([sp,name])=>`<div class="grp"><b>${name}</b>`+
    CATS.filter(c=>c.sport===sp).map(c=>`<label class="mchip${controls[c.key]?' on':''}${apiOk?'':' dis'}" style="--c:${c.color}" data-k="${c.key}" title="${tip}"><input type="checkbox"${controls[c.key]?' checked':''}${apiOk?'':' disabled'}><i></i>${esc(c.label.split(' ').slice(1).join(' '))}</label>`).join('')+'</div>').join('');
  document.querySelectorAll('#ctl .mchip').forEach(l=>{
    const inp=l.querySelector('input');inp.onchange=()=>toggleMarket(l.dataset.k,inp.checked);});
}
async function toggleMarket(k,on){
  if(!apiOk){toast('Controls are offline — start the bot with: python main.py');return;}
  const prev=controls[k];controls[k]=on;sig=JSON.stringify(controls);renderControls();renderAll();
  try{applyState(await api('/api/toggle',{key:k,on:on}));}
  catch(e){controls[k]=prev;sig=JSON.stringify(controls);renderControls();renderAll();toast('Could not change the switch — reload the page');}
}
function updateBotUi(){
  const b=$('botbtn'),s=$('bstat');
  if(!apiOk){s.textContent='○ CONTROLS OFFLINE';s.className='bstat off';b.disabled=true;b.className='botbtn';b.textContent='Start / Stop';
    b.title='Open http://127.0.0.1:8765 while python main.py is running to use the buttons';return;}
  b.title='';
  if(botRunning&&stopping){s.textContent='◌ STOPPING…';s.className='bstat mid';b.disabled=true;b.className='botbtn';b.textContent='Stopping…';}
  else if(botRunning){s.textContent='● BOT RUNNING';s.className='bstat run';b.disabled=false;b.className='botbtn stop';b.textContent='■ Stop bot';}
  else{s.textContent='■ BOT STOPPED';s.className='bstat stop';b.disabled=false;b.className='botbtn start';b.textContent='▶ Start bot';}
}
function applyState(st){
  const was=apiOk;apiOk=true;botRunning=st.running;stopping=st.running&&!st.run_requested;
  const nsig=JSON.stringify(st.markets);
  if(nsig!==sig||!was){sig=nsig;controls=Object.assign({},st.markets);renderControls();renderAll();}
  updateBotUi();
}
async function pollState(){
  try{applyState(await api('/api/state'));}
  catch(e){if(apiOk){apiOk=false;botRunning=null;renderControls();}updateBotUi();}
}
$('botbtn').onclick=async()=>{
  const stop=botRunning;
  if(stop&&!confirm('Stop the bot? It shuts down cleanly, exactly like pressing Ctrl+C in the terminal.'))return;
  $('botbtn').disabled=true;
  try{applyState(await api('/api/bot',{action:stop?'stop':'start'}));}catch(e){toast('Could not reach the controller');}
};

// ---------- statistics ----------
let BETS=DATA.pnl.bets||[];
function money(v){return (v>=0?'+':'-')+'₦'+Math.abs(v).toLocaleString(undefined,{maximumFractionDigits:0});}
function betsFor(key,win){return BETS.filter(b=>(win[0]==null||b.t>=win[0])&&(win[1]==null||b.t<=win[1])&&(key==='all'||(key==='other'?b.c==null:b.c===key)));}
function calc(bets,win){
  let w=0,l=0,v=0,net=0,staked=0,gw=0,gl=0,cum=0,peak=0,dd=0,oS=0,oN=0,iS=0,iN=0,sw=0,sl=0,bw=0,bl=0;
  const profit=[],roi=[];
  bets.forEach(b=>{
    net+=b.p;staked+=b.s;cum+=b.p;peak=Math.max(peak,cum);dd=Math.max(dd,peak-cum);
    if(b.o>1){oS+=b.o;oN++;}
    if(b.p>0){w++;gw+=b.p;sw++;sl=0;bw=Math.max(bw,sw);if(b.o>1){iS+=1/b.o;iN++;}}
    else if(b.p<0){l++;gl+=-b.p;sl++;sw=0;bl=Math.max(bl,sl);if(b.o>1){iS+=1/b.o;iN++;}}
    else v++;
    profit.push([b.t,Math.round(cum*100)/100]);
    if(staked)roi.push([b.t,cum/staked*100]);
  });
  if(bets.length)profit.unshift([win[0]!=null?win[0]:bets[0].t,0]);
  const dec=w+l,winp=dec?w/dec*100:0,be=iN?iS/iN*100:0;
  return {n:bets.length,w,l,v,net,staked,roi:staked?net/staked*100:0,winp,be,edge:dec?winp-be:0,
    avgodds:oN?oS/oN:0,pf:gl?gw/gl:(gw?Infinity:0),dd,bw,bl,profit,roiCurve:roi};
}
let focusKey=null,metric=load('metric','roi'),sortKey=load('sortKey',''),sortDir=+load('sortDir',-1);
let statRows=[];
const dimmed=k=>focusKey!==null&&focusKey!==k;
function buildStatRows(win){
  const pb=DATA.pnl.pending_by||{};
  statRows=[{key:'all',label:'All markets',short:'All',color:ALLC,on:true,pend:DATA.pnl.pending,s:calc(betsFor('all',win),win)}];
  CATS.forEach(c=>statRows.push({key:c.key,label:c.label,short:c.short,color:c.color,on:!!controls[c.key],pend:pb[c.key]||0,s:calc(betsFor(c.key,win),win)}));
  if(BETS.some(b=>b.c==null)||(DATA.pnl.pending_uncat||0)>0)
    statRows.push({key:'other',label:'Other / unrecognised',short:'Other',color:'#9aa4b2',on:true,pend:DATA.pnl.pending_uncat||0,s:calc(betsFor('other',win),win)});
}

// ---------- profit chart: one line per switched-on market ----------
function renderPnl(win){
  const series=[];
  if($('t_all').checked){const r=statRows[0];if(r.s.n)series.push({name:'All',color:ALLC,pts:r.s.profit,dim:dimmed('all')});}
  statRows.slice(1).forEach(r=>{if(r.on&&r.s.n)series.push({name:r.short,color:r.color,pts:r.s.profit,dim:dimmed(r.key)});});
  if(series.length===1)series[0].area=true;
  setChart($('pnl'),series,{zero:true,xmin:win[0],xmax:win[1],xfmt:xfmt,onZoom:zoomTo,empty:'No bets in this range for the switched-on markets',
    yfmt:v=>(v>=0?'+':'')+Math.round(v)});
  const a=statRows[0].s;
  $('cards').innerHTML=
   `<span class="card"><small>NET</small><b class="${a.net>=0?'pos':'neg'}">${money(a.net)}</b></span>`+
   `<span class="card"><small>ROI</small><b class="${a.roi>=0?'pos':'neg'}">${a.roi.toFixed(1)}%</b></span>`+
   `<span class="card"><small>W/L</small><b>${a.w}/${a.l}</b></span>`+
   `<span class="card"><small>MAX DD</small><b class="neg">-₦${Math.round(a.dd).toLocaleString()}</b></span>`+
   `<span class="card"><small>PENDING</small><b>${DATA.pnl.pending}</b></span>`;
}
['t_all'].forEach(id=>{$(id).checked=load(id,'1')==='1';$(id).onchange=()=>{store(id,$(id).checked?'1':'0');renderCharts();};});

// ---------- statistics panel (interactive table + bar chart) ----------
const TCOLS=[['n','Bets'],['pend','Open'],['w','W'],['l','L'],['winp','Win %'],['avgodds','Avg odds'],['be','B/E %'],['edge','Edge'],
  ['staked','Staked'],['net','Net ₦'],['roi','ROI %'],['pf','Profit factor'],['dd','Max DD'],['streak','Streak W/L']];
const TIPS={n:'Settled bets (won or lost) in the selected time range',pend:'Placed but not settled yet — these are not counted in the other columns',
  edge:'Win % minus the win % the odds break even at',pf:'Total won ÷ total lost',dd:'Largest fall from a previous profit peak'};
const colVal=(r,k)=>k==='pend'?r.pend:(k==='streak'?r.s.bw:r.s[k]);
function fmtCell(r,k){
  const s=r.s;
  switch(k){
    case 'n':case 'w':case 'l':return s[k];
    case 'winp':return s.n?s.winp.toFixed(1)+'%':'–';
    case 'avgodds':return s.avgodds?s.avgodds.toFixed(2):'–';
    case 'be':return s.be?s.be.toFixed(1)+'%':'–';
    case 'edge':return s.n?`<b class="${s.edge>=0?'pos':'neg'}">${(s.edge>=0?'+':'')+s.edge.toFixed(1)}</b>`:'–';
    case 'staked':return '₦'+Math.round(s.staked).toLocaleString();
    case 'net':return `<b class="${s.net>=0?'pos':'neg'}">${money(s.net)}</b>`;
    case 'roi':return s.n?`<b class="${s.roi>=0?'pos':'neg'}">${(s.roi>=0?'+':'')+s.roi.toFixed(1)}%</b>`:'–';
    case 'pf':return s.pf===Infinity?'∞':(s.n?s.pf.toFixed(2):'–');
    case 'dd':return s.dd?'-₦'+Math.round(s.dd).toLocaleString():'–';
    case 'streak':return s.n?`${s.bw} / ${s.bl}`:'–';
    case 'pend':return r.pend;
  }
}
function renderStats(){
  const tbl=$('stt');
  let rows=statRows.slice(1);
  if(sortKey){rows=rows.slice().sort((a,b)=>{const x=colVal(a,sortKey),y=colVal(b,sortKey);
    return (x===y?0:(x>y?1:-1))*sortDir;});}
  const all=statRows[0];
  const head='<tr><th data-k="name" style="text-align:left">Market</th>'+TCOLS.map(([k,t])=>
    `<th data-k="${k}" title="${TIPS[k]||''}" class="${sortKey===k?'sorted':''}" data-arrow="${sortDir<0?'▼':'▲'}">${t}</th>`).join('')+'</tr>';
  const rowHtml=(r,tot)=>{
    const sw=(r.key==='all'||r.key==='other')?'':`<span class="sw ${r.on?'on':'offb'}" data-sw="${r.key}" title="Switch this market ${r.on?'off':'on'}">${r.on?'ON':'OFF'}</span>`;
    const cells=TCOLS.map(([k])=>{
      let cls='',st='';
      if(k==='winp'&&r.s.n){cls='cellbar';st=` style="--w:${Math.min(100,r.s.winp)}%;--bc:${r.color}"`;}
      if(k==='roi'&&r.s.n){cls='cellbar';st=` style="--w:${Math.min(100,Math.abs(r.s.roi)*2)}%;--bc:${r.s.roi>=0?UP:DOWN}"`;}
      return `<td class="${cls}"${st}><span>${fmtCell(r,k)}</span></td>`;}).join('');
    return `<tr class="srow${tot?' tot':''}${focusKey===r.key?' foc':''}${r.on?'':' off'}" data-key="${r.key}">`+
      `<td><span class="nm"><i style="background:${r.color}"></i>${esc(r.label)}${sw}</span></td>${cells}</tr>`;};
  tbl.innerHTML='<thead>'+head+'</thead><tbody>'+rowHtml(all,true)+rows.map(r=>rowHtml(r,false)).join('')+'</tbody>';
  tbl.querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(k==='name'){sortKey='';}
    else if(sortKey===k)sortDir=-sortDir;else{sortKey=k;sortDir=-1;}
    store('sortKey',sortKey);store('sortDir',sortDir);renderStats();});
  tbl.querySelectorAll('tr.srow').forEach(tr=>tr.onclick=()=>setFocus(tr.dataset.key));
  tbl.querySelectorAll('.sw').forEach(sp=>sp.onclick=e=>{e.stopPropagation();toggleMarket(sp.dataset.sw,!controls[sp.dataset.sw]);});
  const w=getWin(),unrec=BETS.filter(b=>b.c==null).length+(DATA.pnl.pending_uncat||0);
  $('srange').textContent=((w[0]!=null||w[1]!=null)?'time range: selected':'time range: ALL')+
    ` · ${statRows[0].s.n} of ${BETS.length} settled bets counted · ${DATA.pnl.pending} open · ${unrec} unrecognised`;
}
function setFocus(k){focusKey=(focusKey===k)?null:k;renderCharts();}

// bar chart
const METRICS=[['roi','ROI %'],['net','Net ₦'],['winp','Win %'],['edge','Edge'],['n','Bets']];
const mval=(r,m)=>r.s[m];
function mfmt(v,m){return m==='roi'?(v>=0?'+':'')+v.toFixed(1)+'%':m==='net'?money(v):m==='winp'?v.toFixed(1)+'%':m==='edge'?(v>=0?'+':'')+v.toFixed(1)+' pts':String(v);}
let barHover=-1;
function renderPills(){
  $('pills').innerHTML=METRICS.map(([k,t])=>`<button data-m="${k}" class="${metric===k?'act':''}">${t}</button>`).join('');
  $('pills').querySelectorAll('button').forEach(b=>b.onclick=()=>{metric=b.dataset.m;store('metric',metric);renderPills();drawBars();});
}
function drawBars(){
  const c=$('bars'),{ctx,w,h}=fitCanvas(c);ctx.clearRect(0,0,w,h);
  const rows=statRows;if(!rows.length)return;
  ctx.font='11px Segoe UI,Arial';
  const L=92,R=70,T=4,B=4,pw=w-L-R,rh=(h-T-B)/rows.length;
  const vals=rows.map(r=>mval(r,metric));
  let mn=Math.min(0,...vals),mx=Math.max(0,...vals);if(mx===mn)mx=mn+1;
  const X=v=>L+(v-mn)/(mx-mn)*pw,x0=X(0);
  rows.forEach((r,i)=>{
    const y=T+i*rh,v=vals[i];
    ctx.globalAlpha=(focusKey!==null&&focusKey!==r.key)?0.3:(r.on?1:0.5);
    if(i===barHover||focusKey===r.key){ctx.fillStyle='#1e2740';ctx.fillRect(0,y,w,rh);}
    ctx.fillStyle='#d1d4dc';ctx.textAlign='right';ctx.fillText(r.short,L-6,y+rh/2+4);
    const x1=X(v),bx=Math.min(x0,x1),bw=Math.max(1,Math.abs(x1-x0));
    ctx.fillStyle=(metric==='roi'||metric==='net'||metric==='edge')?(v>=0?UP:DOWN):r.color;
    ctx.fillRect(bx,y+rh*0.2,bw,rh*0.6);
    ctx.fillStyle='#d1d4dc';
    const txt=mfmt(v,metric);
    if(v>=0){ctx.textAlign='left';ctx.fillText(txt,x1+5,y+rh/2+4);}else{ctx.textAlign='left';ctx.fillText(txt,x0+5,y+rh/2+4);}  // negative: label sits right of the zero line, clear of the names
    ctx.globalAlpha=1;
  });
  ctx.strokeStyle='#4a5470';ctx.beginPath();ctx.moveTo(x0,T);ctx.lineTo(x0,h-B);ctx.stroke();
  if(barHover>=0&&c._mx!=null){
    const r=rows[barHover],s=r.s;
    const lines=[r.label+(r.on?'':' (off)'),`Bets ${s.n}  ·  W/L ${s.w}/${s.l}`,`Win ${s.winp.toFixed(1)}%  (break-even ${s.be.toFixed(1)}%)`,`ROI ${(s.roi>=0?'+':'')+s.roi.toFixed(1)}%  ·  Net ${money(s.net)}`];
    const bw=Math.max(...lines.map(t=>ctx.measureText(t).width))+14,bh=lines.length*15+8;
    const bx=Math.min(Math.max(4,c._mx+12),w-bw-4),by=Math.min(Math.max(4,c._my-bh/2),h-bh-4);
    ctx.fillStyle='#1e2740f2';ctx.fillRect(bx,by,bw,bh);ctx.strokeStyle='#4a5470';ctx.strokeRect(bx,by,bw,bh);
    ctx.fillStyle='#d1d4dc';ctx.textAlign='left';lines.forEach((t,i)=>ctx.fillText(t,bx+7,by+17+i*15));
  }
}
(function(){const c=$('bars');
  const idx=e=>{const rh=(c.getBoundingClientRect().height-8)/statRows.length;return Math.max(0,Math.min(statRows.length-1,Math.floor((e.offsetY-4)/rh)));};
  c.addEventListener('mousemove',e=>{c._mx=e.offsetX;c._my=e.offsetY;barHover=idx(e);drawBars();});
  c.addEventListener('mouseleave',()=>{barHover=-1;c._mx=null;drawBars();});
  c.addEventListener('click',e=>setFocus(statRows[idx(e)].key));
})();

// ---------- ROI chart ----------
function buildRoiSel(){
  const cur=load('roimode','all');
  $('roisel').innerHTML='<option value="all">All bets (green when rising, red when falling)</option><option value="compare">Compare switched-on markets</option>'+
    CATS.map(c=>`<option value="${c.key}">${esc(c.label)}</option>`).join('');
  $('roisel').value=cur;if($('roisel').value!==cur)$('roisel').value='all';
  $('roisel').onchange=()=>{store('roimode',$('roisel').value);renderRoi(getWin());};
}
function renderRoi(win){
  const mode=$('roisel').value;
  let series=[],seg=null,card;
  if(mode==='compare'){
    const a=statRows[0].s;if(a.roiCurve.length)series.push({name:'All',color:ALLC,pts:a.roiCurve,dim:dimmed('all')});
    statRows.slice(1).forEach(r=>{if(r.on&&r.s.n)series.push({name:r.short,color:r.color,pts:r.s.roiCurve,dim:dimmed(r.key)});});
    card=a;$('roihint').textContent='one line per switched-on market';
  }else{
    const r=statRows.find(x=>x.key===mode)||statRows[0];card=r.s;
    series=[{name:'ROI',color:'#d1d4dc',pts:r.s.roiCurve}];seg={up:UP,down:DOWN};
    $('roihint').innerHTML='line is <b style="color:#26a69a">green</b> when ROI rises, <b style="color:#ef5350">red</b> when it falls';
  }
  setChart($('roi'),series,{zero:true,xmin:win[0],xmax:win[1],seg:seg,xfmt:xfmt,onZoom:zoomTo,empty:'No bets in this range',
    yfmt:v=>(v>=0?'+':'')+v.toFixed(1)+'%'});
  $('roicards').innerHTML=
   `<span class="card"><small>ROI</small><b class="${card.roi>=0?'pos':'neg'}">${card.roi.toFixed(1)}%</b></span>`+
   `<span class="card"><small>SETTLED BETS</small><b>${card.n}</b></span>`;
}

function renderCharts(){const win=getWin();renderPnl(win);renderStats();drawBars();renderRoi(win);}
function renderAll(){markRange();const win=getWin();buildStatRows(win);renderPills();renderPnl(win);renderStats();drawBars();renderRoi(win);}

// ---------- odds sidebar (every market that has come through; updates live) ----------
let oddsData=DATA.odds.markets;
let selKey=load('sel',null);
$('sort').value=load('sort','recent');
const lastP=m=>m.pts[m.pts.length-1][1], firstP=m=>m.pts[0][1];
const seenAt=m=>m.seen||m.pts[m.pts.length-1][0];
// latest move = last price vs the one before it (arrow + % in the list)
function lastMove(m){const n=m.pts.length;if(n<2)return null;
  const a=m.pts[n-2][1],b=m.pts[n-1][1];return a?{pct:(b/a-1)*100}:null;}
const sorters={recent:(a,b)=>seenAt(b)-seenAt(a),
  move:(a,b)=>{const x=lastMove(a),y=lastMove(b);return (y?Math.abs(y.pct):0)-(x?Math.abs(x.pct):0);},
  drop:(a,b)=>{const x=lastMove(a),y=lastMove(b);return (x?x.pct:0)-(y?y.pct:0);},
  ticks:(a,b)=>b.pts.length-a.pts.length};
function title(m){return (m.home||'?')+' vs '+(m.away||'?');}
function sub(m){const p=(m.points===null||m.points===undefined||m.points==='')?'':' '+m.points;
  return m.lineType+' · '+m.outcome+p;}

let seenMax=Math.max(0,...oddsData.map(seenAt));   // newest alert already on screen
let newKeys=new Set();                             // markets that just got a new alert
let listSig='';

// drop filter
let dropOn=load('dropOn','0')==='1', dropMin=parseFloat(load('dmin','2'));
if(!(dropMin>=0))dropMin=2;
$('dmin').value=dropMin;
function markDrop(){
  $('dtog').classList.toggle('act',dropOn);
  document.querySelectorAll('.dropf .q').forEach(b=>b.classList.toggle('act',dropOn&&+b.dataset.d===dropMin));}
$('dtog').onclick=()=>{dropOn=!dropOn;store('dropOn',dropOn?'1':'0');markDrop();renderList(true);};
$('dmin').oninput=()=>{const v=parseFloat($('dmin').value);
  if(v>=0){dropMin=v;store('dmin',v);markDrop();renderList(true);}};
document.querySelectorAll('.dropf .q').forEach(b=>b.onclick=()=>{
  dropMin=+b.dataset.d;dropOn=true;$('dmin').value=dropMin;
  store('dmin',dropMin);store('dropOn','1');markDrop();renderList(true);});

function renderList(toTop,animate){
  const lst=$('mlist'),st0=lst.scrollTop,atTop=st0<=5;
  const q=$('filter').value.toLowerCase();
  let list=oddsData.filter(m=>(title(m)+' '+m.league+' '+sub(m)).toLowerCase().includes(q));
  if(dropOn)list=list.filter(m=>{const x=lastMove(m);return x&&-x.pct>=dropMin;});
  list.sort(sorters[$('sort').value]||sorters.recent);
  $('ocount').textContent=list.length+' of '+oddsData.length+' markets';
  if((!selKey||!oddsData.some(m=>m.key===selKey))&&oddsData.length)selKey=(list[0]||oddsData[0]).key;
  // nothing changed in what is shown -> leave the list alone (no redraw, no flicker)
  const sig=list.map(m=>m.key+':'+m.pts.length+':'+lastP(m)).join('|')+'#'+selKey;
  if(sig===listSig&&toTop!==true)return;
  listSig=sig;
  // remember which row is at the top of your view so new rows never push you around
  let anchor=null;
  if(!atTop){for(const r of lst.querySelectorAll('.row')){
    if(r.offsetTop+r.offsetHeight>st0){anchor={k:r.dataset.k,off:r.offsetTop-st0};break;}}}
  let ni=0;
  lst.innerHTML=list.map((m,i)=>{
    const l=lastP(m),x=lastMove(m),up=x?x.pct>=0:true;
    const isNew=animate&&newKeys.has(m.key);
    const cls='row'+(m.key===selKey?' sel':'')+(isNew?' new '+(x?(up?'up':'dn'):'')+(atTop?'':' still'):'');
    const st=isNew?` style="animation-delay:${Math.min(ni++,12)*70}ms"`:'';
    const mvTxt=x?`<span class="arr ${up?'pos':'neg'}">${up?'▲':'▼'}</span> <span class="${up?'pos':'neg'}">${up?'+':''}${x.pct.toFixed(1)}%</span>`
                 :'<span class="s">new</span>';
    return `<div class="${cls}"${st} data-i="${i}" data-k="${esc(m.key)}">
      <div><div class="t" title="${esc(title(m))}">${esc(title(m))}</div><div class="s">${esc(sub(m))}</div></div>
      <div class="r"><b>${l.toFixed(3)}</b><div class="s">${mvTxt}</div></div></div>`;}).join('');
  lst.querySelectorAll('.row').forEach(r=>r.onclick=()=>{
    selKey=list[+r.dataset.i].key;store('sel',selKey);renderList();renderOdds();});
  if(toTop===true)lst.scrollTop=0;
  else if(anchor){const r=[...lst.querySelectorAll('.row')].find(x=>x.dataset.k===anchor.k);
    lst.scrollTop=r?Math.max(0,r.offsetTop-anchor.off):st0;}
  else lst.scrollTop=0;
}
function renderOdds(){
  const m=oddsData.find(x=>x.key===selKey);
  if(!m){$('ohead').innerHTML='<span class="s">Waiting for Pinnacle alerts…</span>';setChart($('odds'),[],{empty:'Waiting for Pinnacle alerts…'});return;}
  const f=firstP(m),l=lastP(m),up=l>=f,col=up?UP:DOWN;
  $('ohead').innerHTML=`<span class="t">${esc(title(m))} <span class="${up?'pos':'neg'}">${l.toFixed(3)} ${up?'▲':'▼'} ${((l/f-1)*100).toFixed(2)}%</span></span>
    <span class="s">${esc(m.league||'')} · ${esc(sub(m))} · ${m.pts.length} ticks · open ${f.toFixed(3)}</span>`;
  setChart($('odds'),[{name:'Odds',color:col,area:true,pts:m.pts}],
    {zero:false,step:true,xfmt:(v,span)=>span===0?niceTime(v):niceTime(v),yfmt:v=>v.toFixed(3)});
}
function updateOdds(markets){
  newKeys=new Set(markets.filter(m=>seenAt(m)>seenMax).map(m=>m.key));
  seenMax=Math.max(seenMax,0,...markets.map(seenAt));
  oddsData=markets;
  renderList(false,true);
  renderOdds();
}

// ---------- quiet live feed: the page asks the controller for fresh data (no reloading) ----------
function oddsSig(ms){let n=0,mx=0;ms.forEach(m=>{n+=m.pts.length;const t=seenAt(m);if(t>mx)mx=t;});return ms.length+':'+n+':'+mx;}
let lastPnlSig=JSON.stringify(DATA.pnl),lastOddsSig=oddsSig(DATA.odds.markets),toastedAuth=false;
function updateStatus(){
  const age=Date.now()-DATA.generated,stale=age>60000;
  $('dot').className='dot'+(stale?' off':'');
  $('status').textContent=(stale?'no new data for '+Math.round(age/1000)+'s · ':'live · ')+'updated '+niceTime(DATA.generated);
}
function applyData(d){
  DATA.generated=d.generated;
  const ps=JSON.stringify(d.pnl);
  if(ps!==lastPnlSig){lastPnlSig=ps;DATA.pnl=d.pnl;BETS=d.pnl.bets||[];renderAll();}
  const os=oddsSig(d.odds.markets);
  if(os!==lastOddsSig){lastOddsSig=os;updateOdds(d.odds.markets);}
  updateStatus();
}
async function pollData(){
  try{const d=await api('/api/data?gen='+DATA.generated);if(!d.unchanged)applyData(d);}
  catch(e){if(String(e.message).includes('401')&&!toastedAuth){toastedAuth=true;toast('The controller was restarted — reload this page once.');}}
  updateStatus();
}

$('filter').value=load('filter','');
$('filter').oninput=()=>{store('filter',$('filter').value);renderList(true);};
$('sort').onchange=()=>{store('sort',$('sort').value);renderList(true);};

buildRoiSel();renderControls();updateBotUi();
renderAll();markDrop();renderList();renderOdds();updateStatus();
pollState();pollData();
setInterval(pollState,2000);setInterval(pollData,3000);setInterval(updateStatus,5000);
window.addEventListener('resize',()=>{draw($('pnl'));draw($('roi'));draw($('odds'));drawBars();});
</script></body></html>"""


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for _ in range(6):  # Windows can briefly lock a file that is being read
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def write_dashboard():
    """Writes dashboard.html (first paint / offline copy) and dashboard_data.json
    (the quiet live feed the open page polls). Never raises."""
    try:
        ctl = controls.load_state(0.0)
        data = {
            "generated": int(time.time() * 1000),
            "pnl": _pnl_payload(),
            "odds": _odds_payload(),
            "cats": controls.CATEGORIES,
            "controls": {"markets": ctl["markets"]},
            "api": {"port": PORT, "token": ctl["token"]},
        }
        blob = json.dumps(data).replace("</", "<\\/")
        _atomic_write(DASHBOARD_HTML, PAGE.replace("__DATA__", blob))
        feed = {
            k: v for k, v in data.items() if k != "api"
        }  # the token stays out of the feed
        _atomic_write(DATA_JSON, json.dumps(feed))
    except Exception as e:
        print(f"[dashboard] WARNING: could not update {DASHBOARD_HTML} — {e}")


def _loop():
    while True:
        write_dashboard()
        time.sleep(WRITE_INTERVAL_SEC)


def start_writer():
    """Starts the background thread that keeps dashboard.html fresh."""
    global _writer_started
    if _writer_started:
        return
    _writer_started = True
    threading.Thread(target=_loop, daemon=True).start()
    print(f"[dashboard] writing {DASHBOARD_HTML} every {WRITE_INTERVAL_SEC}s")


# pinnacleretriever.py calls start_server(); keep that name working.
start_server = start_writer


if __name__ == "__main__":
    # Standalone: builds the profit/ROI charts from the CSV (no live odds ticks).
    write_dashboard()
    print(f"[dashboard] wrote {DASHBOARD_HTML}")
