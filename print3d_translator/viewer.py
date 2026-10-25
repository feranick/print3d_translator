"""viewer.py — a self-contained 3D view of a study (<name>.view.html): the placed parts on the bed and the
toolpaths our own G-code reader found, with a layer slider and colours by feature or by filament.
Version: 2026.10.24.10

One HTML file, no internet needed: plain WebGL2 (no libraries), the geometry and toolpaths embedded as
zlib-compressed binary (base64) and inflated by the browser (DecompressionStream). Open it in any recent
browser (Safari 16.4+, Chrome, Firefox).
Payload: meshes as float32 vertices + uint32 indices; toolpaths (print region only) as polylines of
uint16 points (0.01 mm, +20 mm offset) with per-polyline layer, feature and tool.
"""
from __future__ import annotations

import base64
import html
import json
import zlib

import numpy as np

from . import gcode as G

# feature groups and colours (close to OrcaSlicer's preview)
FEATURES = [
    ("Outer wall", "#ff8000"), ("Inner wall", "#ffe64d"), ("Overhang wall", "#4d80ff"),
    ("Sparse infill", "#b03029"), ("Solid infill", "#9654cc"), ("Top surface", "#f04040"),
    ("Bottom surface", "#665cc7"), ("Bridge", "#4d80ba"), ("Gap infill", "#e6e6e6"),
    ("Support", "#00c000"), ("Support interface", "#008000"), ("Brim / skirt", "#00876e"),
    ("Prime tower", "#b3e3ab"), ("Ironing", "#ff8c69"), ("Other", "#909090"),
]
FEATURE_INDEX = {n: i for i, (n, _) in enumerate(FEATURES)}


def feature_group(name: str) -> int:
    f = (name or "").lower()
    if "prime tower" in f or "wipe tower" in f:
        return FEATURE_INDEX["Prime tower"]
    if "support" in f:
        return FEATURE_INDEX["Support interface" if "interface" in f or "transition" in f else "Support"]
    if "brim" in f or "skirt" in f:
        return FEATURE_INDEX["Brim / skirt"]
    if "outer wall" in f:
        return FEATURE_INDEX["Outer wall"]
    if "overhang" in f:
        return FEATURE_INDEX["Overhang wall"]
    if "wall" in f:
        return FEATURE_INDEX["Inner wall"]
    if "sparse" in f:
        return FEATURE_INDEX["Sparse infill"]
    if "top" in f:
        return FEATURE_INDEX["Top surface"]
    if "bottom" in f:
        return FEATURE_INDEX["Bottom surface"]
    if "bridge" in f:
        return FEATURE_INDEX["Bridge"]
    if "gap" in f:
        return FEATURE_INDEX["Gap infill"]
    if "solid" in f:
        return FEATURE_INDEX["Solid infill"]
    if "ironing" in f:
        return FEATURE_INDEX["Ironing"]
    return FEATURE_INDEX["Other"]


def _mesh_arrays(tri: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    q = np.round(tri.reshape(-1, 3) / 1e-4).astype(np.int64)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    verts = tri.reshape(-1, 3)[first].astype(np.float32)
    idx = inv.reshape(-1).astype(np.uint32)
    return verts, idx


def _toolpaths(P: G.Parsed):
    """Polylines of the print region: points (n, 2) uint16, records (m, 5): start, count, layer, feature, tool."""
    if P is None or not len(P.segments):
        return np.zeros((0, 2), np.uint16), np.zeros((0, 5), np.uint32), []
    m = P.seg_info[:, 0] == 1
    s, info = P.segments[m], P.seg_info[m]
    feats = np.array([feature_group(f) for f in P.features], dtype=np.int64)
    fg = feats[info[:, 2]]
    q = np.clip(np.round((s[:, :4] + 20.0) / 0.01), 0, 65535).astype(np.int64)
    pts, recs = [], []
    n = len(s)
    start_new = np.ones(n, dtype=bool)
    if n > 1:
        same_pt = (q[1:, 0] == q[:-1, 2]) & (q[1:, 1] == q[:-1, 3])
        same_attr = (info[1:, 4] == info[:-1, 4]) & (fg[1:] == fg[:-1]) & (info[1:, 1] == info[:-1, 1])
        start_new[1:] = ~(same_pt & same_attr)
    starts = np.nonzero(start_new)[0]
    ends = np.r_[starts[1:], n]
    total = 0
    for a, b in zip(starts, ends):
        poly = np.vstack([q[a, 0:2], q[a:b, 2:4]])
        pts.append(poly)
        recs.append((total, len(poly), max(int(info[a, 4]), 0), int(fg[a]), max(int(info[a, 1]), 0)))
        total += len(poly)
    layer_z = [l["z_height"] if l["z_height"] is not None else 0.0 for l in P.layers]
    return np.vstack(pts).astype(np.uint16), np.array(recs, dtype=np.uint32), layer_z


def build(objects: list[dict], bed: np.ndarray, height: float, P: G.Parsed | None = None,
          filament_colors: list[str] | None = None, title: str = "", summary: dict | None = None) -> str:
    """objects: [{name, tri (placed), color}]. Returns the HTML text."""
    blobs, meta_objs, off = [], [], 0
    for o in objects:
        v, i = _mesh_arrays(np.asarray(o["tri"], dtype=float))
        meta_objs.append({"name": o["name"], "color": o.get("color") or "#c8c8c8",
                          "v": [off, int(v.size)], "i": [off + v.nbytes, int(i.size)]})
        blobs += [v.tobytes(), i.tobytes()]
        off += v.nbytes + i.nbytes
    pts, recs, layer_z = _toolpaths(P)
    tp = {"pts": [off, int(pts.size)], "recs": [off + pts.nbytes, int(recs.size)]}
    blobs += [pts.tobytes(), recs.tobytes()]
    raw = b"".join(blobs)
    payload = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    lo, hi = bed.min(axis=0), bed.max(axis=0)
    meta = {"title": title, "bed": [float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])], "height": float(height),
            "objects": meta_objs, "toolpaths": tp, "layer_z": layer_z, "features": FEATURES,
            "filament_colors": filament_colors or [], "summary": summary or {}, "raw_bytes": len(raw)}
    return _HTML.replace("__TITLE__", html.escape(title or "print3d_translator view")) \
        .replace("__META__", json.dumps(meta)).replace("__DATA__", payload)


_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f7f7f5;--fg:#1f1f1d;--muted:#6b6a65;--card:#fff;--line:#d9d8d2}
@media (prefers-color-scheme: dark){:root{--bg:#1c1c1b;--fg:#ecebe6;--muted:#a3a29c;--card:#262624;--line:#3a3a37}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px -apple-system,system-ui,sans-serif}
header{padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px 18px;align-items:baseline}
header h1{font-size:17px;font-weight:500;margin:0}
.k{color:var(--muted)} .ok{color:#2e7d32} .bad{color:#c62828}
.bar{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;padding:6px 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--card)}
.bar label{display:flex;gap:6px;align-items:center} input[type=range]{width:min(420px,50vw)}
#c{display:block;width:100%;height:calc(100vh - 150px);min-height:360px;touch-action:none}
#legend{display:flex;flex-wrap:wrap;gap:4px 12px;padding:6px 16px;font-size:12px;color:var(--muted)}
#legend span i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
#err{padding:16px;color:#c62828}
</style></head><body>
<header><h1 id="t"></h1><span id="s" class="k"></span></header>
<div class="bar">
<label><input type="checkbox" id="model" checked> Model</label>
<label><input type="checkbox" id="paths" checked> Toolpaths</label>
<label>Colour <select id="mode"><option value="f">by feature</option><option value="t">by filament</option></select></label>
<label>Layer <input type="range" id="layer" min="0" max="0" value="0"><span id="ln" class="k"></span></label>
<label><input type="checkbox" id="single"> Only this layer</label>
<button id="reset">Reset view</button>
</div>
<canvas id="c"></canvas><div id="legend"></div><div id="err"></div>
<script id="meta" type="application/json">__META__</script>
<script>
const META=JSON.parse(document.getElementById('meta').textContent), DATA="__DATA__";
const $=id=>document.getElementById(id);
$('t').textContent=META.title;
const S=META.summary||{};
$('s').innerHTML=[S.printer,S.time,S.filament,S.reliable===undefined?'':(S.reliable?'<span class="ok">reliable</span>':'<span class="bad">not reliable</span>')].filter(Boolean).join(' &middot; ');
function hex(c){c=(c||'#888888').replace('#','');return[0,2,4].map(i=>parseInt(c.substr(i,2),16)/255);}
async function inflate(b64){const bin=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));
 const ds=new Blob([bin]).stream().pipeThrough(new DecompressionStream('deflate'));return await new Response(ds).arrayBuffer();}
function persp(f,a,n,fr){const t=1/Math.tan(f/2);return[t/a,0,0,0,0,t,0,0,0,0,(fr+n)/(n-fr),-1,0,0,2*fr*n/(n-fr),0];}
function sub(a,b){return[a[0]-b[0],a[1]-b[1],a[2]-b[2]]}function cr(a,b){return[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]}
function nz(a){const l=Math.hypot(...a)||1;return a.map(x=>x/l)}function dt(a,b){return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]}
function look(e,c,u){const z=nz(sub(e,c)),x=nz(cr(u,z)),y=cr(z,x);return[x[0],y[0],z[0],0,x[1],y[1],z[1],0,x[2],y[2],z[2],0,-dt(x,e),-dt(y,e),-dt(z,e),1];}
function mul(a,b){const r=new Array(16).fill(0);for(let i=0;i<4;i++)for(let j=0;j<4;j++)for(let k=0;k<4;k++)r[i+4*j]+=a[i+4*k]*b[k+4*j];return r;}
(async()=>{
const cv=$('c'),gl=cv.getContext('webgl2',{antialias:true});
if(!gl){$('err').textContent='This browser has no WebGL2.';return;}
let buf;try{buf=await inflate(DATA);}catch(e){$('err').textContent='Could not read the embedded data: '+e;return;}
function sh(vs,fs){const p=gl.createProgram();for(const[t,s]of[[gl.VERTEX_SHADER,vs],[gl.FRAGMENT_SHADER,fs]]){const o=gl.createShader(t);gl.shaderSource(o,s);gl.compileShader(o);gl.attachShader(p,o);}gl.linkProgram(p);return p;}
const PM=sh(`#version 300 es
in vec3 p;in vec3 n;uniform mat4 m;out vec3 vn;void main(){vn=n;gl_Position=m*vec4(p,1.);}`,`#version 300 es
precision mediump float;in vec3 vn;uniform vec3 c;uniform vec3 L;uniform float a;out vec4 o;
void main(){float d=abs(dot(normalize(vn),L));o=vec4(c*(.35+.65*d),a);}`);
const PL=sh(`#version 300 es
in vec3 p;in vec3 c;uniform mat4 m;out vec3 vc;void main(){vc=c;gl_Position=m*vec4(p,1.);}`,`#version 300 es
precision mediump float;in vec3 vc;out vec4 o;void main(){o=vec4(vc,1.);}`);
function vbo(prog,arrs){const va=gl.createVertexArray();gl.bindVertexArray(va);for(const[name,data,sz]of arrs){const b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.bufferData(gl.ARRAY_BUFFER,data,gl.STATIC_DRAW);const l=gl.getAttribLocation(prog,name);gl.enableVertexAttribArray(l);gl.vertexAttribPointer(l,sz,gl.FLOAT,false,0,0);}gl.bindVertexArray(null);return va;}
const meshes=[];const OB=[1e9,1e9,1e9,-1e9,-1e9,-1e9];
for(const o of META.objects){const V=new Float32Array(buf,o.v[0],o.v[1]);for(let q=0;q<V.length;q+=3)for(let d=0;d<3;d++){OB[d]=Math.min(OB[d],V[q+d]);OB[d+3]=Math.max(OB[d+3],V[q+d]);}const I=new Uint32Array(buf.slice(o.i[0],o.i[0]+4*o.i[1]));
 const P=new Float32Array(I.length*3),N=new Float32Array(I.length*3);
 for(let t=0;t<I.length;t+=3){const a=I[t]*3,b=I[t+1]*3,c=I[t+2]*3;const A=[V[a],V[a+1],V[a+2]],B=[V[b],V[b+1],V[b+2]],C=[V[c],V[c+1],V[c+2]];
  const n=nz(cr(sub(B,A),sub(C,A)));[A,B,C].forEach((q,k)=>{P.set(q,(t+k)*3);N.set(n,(t+k)*3);});}
 meshes.push({va:vbo(PM,[['p',P,3],['n',N,3]]),n:I.length,c:hex(o.color)});}
const lo=META.bed,W=lo[2]-lo[0],H=lo[3]-lo[1];
const bedVA=vbo(PM,[['p',new Float32Array([lo[0],lo[1],-.05,lo[2],lo[1],-.05,lo[2],lo[3],-.05,lo[0],lo[1],-.05,lo[2],lo[3],-.05,lo[0],lo[3],-.05]),3],['n',new Float32Array(18).map((_,i)=>i%3==2?1:0),3]]);
const gp=[],gc=[];const step=W>300?25:16;for(let x=lo[0];x<=lo[2]+1e-6;x+=step){gp.push(x,lo[1],0,x,lo[3],0);}for(let y=lo[1];y<=lo[3]+1e-6;y+=step){gp.push(lo[0],y,0,lo[2],y,0);}
for(let i=0;i<gp.length/3;i++)gc.push(.42,.40,.36);
const gridVA=vbo(PL,[['p',new Float32Array(gp),3],['c',new Float32Array(gc),3]]);const gridN=gp.length/3;
const tp=META.toolpaths,PTS=new Uint16Array(buf.slice(tp.pts[0],tp.pts[0]+2*tp.pts[1])),R=new Uint32Array(buf.slice(tp.recs[0],tp.recs[0]+4*tp.recs[1]));
const LZ=META.layer_z,NL=Math.max(LZ.length,1);
const recs=[];for(let i=0;i<R.length;i+=5)recs.push(R.subarray(i,i+5));
recs.sort((a,b)=>a[2]-b[2]);
let nseg=0;for(const r of recs)nseg+=r[1]-1;
const LP=new Float32Array(nseg*6),CF=new Float32Array(nseg*6),CT=new Float32Array(nseg*6),off=new Int32Array(NL+1);
const fcol=META.features.map(f=>hex(f[1])),tcol=(META.filament_colors.length?META.filament_colors:['#3b82f6','#ef4444','#22c55e','#eab308']).map(hex);
let k=0,cur=0;
for(const r of recs){const[s,c,l,f,t]=r;while(cur<=l&&cur<=NL){off[cur]=k;cur++;}const z=LZ[l]||0;
 for(let j=0;j<c-1;j++){const a=(s+j)*2,b=(s+j+1)*2;LP.set([PTS[a]*.01-20,PTS[a+1]*.01-20,z,PTS[b]*.01-20,PTS[b+1]*.01-20,z],k*6);
  const cf=fcol[f]||fcol[fcol.length-1],ct=tcol[t%tcol.length];CF.set([...cf,...cf],k*6);CT.set([...ct,...ct],k*6);k++;}}
while(cur<=NL){off[cur]=k;cur++;}
const pathVA={f:vbo(PL,[['p',LP,3],['c',CF,3]]),t:vbo(PL,[['p',LP,3],['c',CT,3]])};
const sl=$('layer');sl.max=NL-1;sl.value=NL-1;
const leg=()=>{const m=$('mode').value;$('legend').innerHTML=(m=='f'?META.features.map(f=>[f[0],f[1]]):META.filament_colors.map((c,i)=>['Filament '+(i+1),c])).map(([n,c])=>`<span><i style="background:${c}"></i>${n}</span>`).join('');};
leg();
const has=OB[0]<OB[3],ext=has?Math.max(OB[3]-OB[0],OB[4]-OB[1],OB[5]-OB[2],20):Math.max(W,H);
let az,el,dist,tg;const reset=()=>{az=-2.2;el=.55;dist=has?ext*2.6:Math.max(W,H)*1.35;tg=has?[(OB[0]+OB[3])/2,(OB[1]+OB[4])/2,(OB[2]+OB[5])/3]:[(lo[0]+lo[2])/2,(lo[1]+lo[3])/2,0];draw();};
$('reset').onclick=reset;
function draw(){const dpr=window.devicePixelRatio||1;const w=cv.clientWidth*dpr,h=cv.clientHeight*dpr;if(cv.width!=w||cv.height!=h){cv.width=w;cv.height=h;}
 gl.viewport(0,0,w,h);gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.enable(gl.DEPTH_TEST);
 const eye=[tg[0]+dist*Math.cos(el)*Math.cos(az),tg[1]+dist*Math.cos(el)*Math.sin(az),tg[2]+dist*Math.sin(el)];
 const M=mul(persp(.7,w/h,1,dist*6+2000),look(eye,tg,[0,0,1]));const L=nz([-.35,-.55,.76]);
 const L1=+sl.value;$('ln').textContent=`${L1+1} / ${NL}  (z ${(LZ[L1]||0).toFixed(2)} mm)`;
 gl.useProgram(PM);gl.uniformMatrix4fv(gl.getUniformLocation(PM,'m'),false,M);gl.uniform3fv(gl.getUniformLocation(PM,'L'),L);
 gl.uniform1f(gl.getUniformLocation(PM,'a'),1);gl.uniform3fv(gl.getUniformLocation(PM,'c'),[.55,.5,.42]);gl.bindVertexArray(bedVA);gl.drawArrays(gl.TRIANGLES,0,6);
 gl.useProgram(PL);gl.uniformMatrix4fv(gl.getUniformLocation(PL,'m'),false,M);gl.bindVertexArray(gridVA);gl.drawArrays(gl.LINES,0,gridN);
 if($('paths').checked&&nseg){gl.bindVertexArray(pathVA[$('mode').value]);const a=$('single').checked?off[L1]:0,b=off[L1+1];if(b>a)gl.drawArrays(gl.LINES,a*2,(b-a)*2);}
 if($('model').checked){gl.useProgram(PM);const ghost=$('paths').checked&&nseg;gl.uniform1f(gl.getUniformLocation(PM,'a'),ghost?.25:1);
  if(ghost){gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);gl.depthMask(false);}
  for(const m of meshes){gl.uniform3fv(gl.getUniformLocation(PM,'c'),m.c);gl.bindVertexArray(m.va);gl.drawArrays(gl.TRIANGLES,0,m.n);}
  gl.depthMask(true);gl.disable(gl.BLEND);}
 gl.bindVertexArray(null);}
let drag=null;cv.addEventListener('pointerdown',e=>{drag={x:e.clientX,y:e.clientY,pan:e.shiftKey||e.button==2};cv.setPointerCapture(e.pointerId);});
cv.addEventListener('pointermove',e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;drag.x=e.clientX;drag.y=e.clientY;
 if(drag.pan){const s=dist*.0015,r=[-Math.sin(az),Math.cos(az),0];tg=[tg[0]-r[0]*dx*s,tg[1]-r[1]*dx*s,tg[2]+dy*s];}
 else{az-=dx*.008;el=Math.min(1.55,Math.max(-.2,el+dy*.008));}draw();});
cv.addEventListener('pointerup',()=>drag=null);cv.addEventListener('contextmenu',e=>e.preventDefault());
cv.addEventListener('wheel',e=>{e.preventDefault();dist*=Math.exp(e.deltaY*.001);dist=Math.min(5000,Math.max(10,dist));draw();},{passive:false});
for(const id of['model','paths','single','layer'])$(id).addEventListener('input',draw);$('mode').addEventListener('input',()=>{leg();draw();});
window.addEventListener('resize',draw);
reset();
})();
</script></body></html>
"""


def write(path, objects, bed, height, P=None, filament_colors=None, title="", summary=None) -> str:
    text = build(objects, bed, height, P, filament_colors, title, summary)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return str(path)


def for_study(folder, res: dict | None = None) -> str:
    """Writes <folder>/<name>.view.html for a finished study (from its result, profiles, placed STLs and
    G-code); returns the path."""
    from pathlib import Path
    from . import check as C, mesh as M
    d = Path(folder)
    name = d.name
    res = res if res is not None else json.loads((d / f"{name}.json").read_text())
    if not res.get("ok"):
        raise ValueError(f"{name} did not slice ({res.get('why')}): nothing to show")
    machine = json.loads((d / "profiles" / "machine.json").read_text())
    fil_files = sorted((d / "profiles").glob("filament_*.json"), key=lambda p: int(p.stem.split("_")[1]))
    colors = [C._first(json.loads(f.read_text()).get("filament_colour"), "#c8c8c8") for f in fil_files]
    asm = json.loads((d / "assemble.json").read_text())["plates"][0]["objects"]
    objs = [{"name": Path(o["path"]).stem, "tri": M.read_stl(d / "objects" / Path(o["path"]).name),
             "color": colors[o["filaments"][0] - 1] if o["filaments"][0] - 1 < len(colors) else "#c8c8c8"} for o in asm]
    gpath = d / f"{name}.gcode"
    P = G.parse(gpath.read_text(errors="replace")) if gpath.exists() else None
    est = res.get("estimate") or {}
    pr = res.get("printer") or {}
    summary = {"printer": f"{pr.get('name', '')} {pr.get('nozzle', '')} mm".strip(),
               "time": est.get("time"), "filament": f"{est.get('filament_g')} g" if est.get("filament_g") else None,
               "reliable": res.get("reliable")}
    out = d / f"{name}.view.html"
    return write(out, objs, C._poly(machine.get("printable_area")), C._f(machine.get("printable_height"), 250), P,
                 colors, title=name, summary=summary)
