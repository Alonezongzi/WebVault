#!/usr/bin/env python3
"""WebVault 本地界面 —— 零依赖的网页版操作台。

启动一个本地 HTTP 服务，在浏览器里粘贴网址、选模式、点保存。
后端就是核心流水线，前端是一个自带样式的单页。

为什么做这个：
  命令行适合批量和脚本，但"随手存一篇"这种事在浏览器里做最顺手。
  整个界面零依赖（没有 CDN、没有框架），断网也能用。

用法：
    python webvault-ui.py                 # 打开浏览器，监听 127.0.0.1:8770
    python webvault-ui.py --port 9000
    python webvault-ui.py --no-browser
    python webvault-ui.py --out D:\\saves
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from src import __version__  # noqa: E402
from src.inline import InlineOptions  # noqa: E402
from src.pipeline import vault, vault_many  # noqa: E402
from src.util import human_size  # noqa: E402

# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------

STATE = {
    "out_dir": "",
    "jobs": {},          # id -> dict
    "lock": threading.Lock(),
    "counter": 0,
}
JOB_HISTORY_MAX = 200


def new_job(url: str, mode: str) -> str:
    with STATE["lock"]:
        STATE["counter"] += 1
        jid = "j%d" % STATE["counter"]
        STATE["jobs"][jid] = {
            "id": jid, "url": url, "mode": mode, "status": "running",
            "log": [], "title": "", "path": "", "size": 0, "error": "",
            "started": time.time(), "elapsed": 0.0, "stats": "",
        }
        # 清理历史
        if len(STATE["jobs"]) > JOB_HISTORY_MAX:
            done = sorted(
                (j for j in STATE["jobs"].values() if j["status"] != "running"),
                key=lambda j: j["started"],
            )
            for j in done[: max(0, len(STATE["jobs"]) - JOB_HISTORY_MAX)]:
                STATE["jobs"].pop(j["id"], None)
        return jid


def log_job(jid: str, msg: str) -> None:
    with STATE["lock"]:
        j = STATE["jobs"].get(jid)
        if j is not None:
            j["log"].append(msg)
            if len(j["log"]) > 400:
                del j["log"][:-200]


def finish_job(jid: str, result) -> None:
    with STATE["lock"]:
        j = STATE["jobs"].get(jid)
        if j is None:
            return
        j["status"] = "ok" if result.ok else "fail"
        j["title"] = result.title
        j["path"] = result.out_path
        j["size"] = result.size()
        j["error"] = result.error
        j["elapsed"] = result.elapsed
        if result.stats:
            j["stats"] = result.stats.summary()


def run_job(jid: str, url: str, mode: str, opts: InlineOptions, out_dir: str,
            timeout: float, keep_site_css: bool = False) -> None:
    try:
        def prog(m: str) -> None:
            log_job(jid, m)

        r = vault(url, mode=mode, inline_options=opts, out_dir=out_dir,
                  progress=prog, timeout=timeout, write=True,
                  keep_site_css=keep_site_css)
        finish_job(jid, r)
    except Exception as e:
        import traceback
        log_job(jid, "内部错误: %s" % e)
        with STATE["lock"]:
            j = STATE["jobs"].get(jid)
            if j:
                j["status"] = "fail"
                j["error"] = "%s: %s" % (type(e).__name__, e)


# --------------------------------------------------------------------------
# 界面 HTML
# --------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WebVault · 网页金库</title>
<style>
:root{
  --bg:#0f1014; --panel:#171922; --panel2:#1e212c; --fg:#e8e8ea; --soft:#9aa0ae;
  --line:#2a2e3c; --accent:#5b8cff; --accent2:#7aa2f7; --ok:#3ddc97; --err:#ff6b6b;
  --warn:#ffc857;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--fg); min-height:100vh;
  font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Microsoft YaHei",sans-serif;
}
header{
  border-bottom:1px solid var(--line); padding:16px 22px;
  display:flex; align-items:baseline; gap:12px; flex-wrap:wrap;
  background:linear-gradient(180deg,#1a1d28,#14161d);
}
header h1{margin:0; font-size:17px; letter-spacing:.3px; font-weight:650}
header .sub{color:var(--soft); font-size:12px}
header .sp{flex:1}
header .ver{color:var(--soft); font-size:11px; border:1px solid var(--line);
  padding:2px 7px; border-radius:99px}
main{max-width:1080px; margin:0 auto; padding:22px}
.card{background:var(--panel); border:1px solid var(--line); border-radius:12px;
  padding:18px; margin-bottom:16px}
label{display:block; font-size:12px; color:var(--soft); margin-bottom:6px;
  text-transform:uppercase; letter-spacing:.6px}
textarea,input,select{
  width:100%; background:var(--panel2); border:1px solid var(--line); color:var(--fg);
  border-radius:8px; padding:10px 12px; font:inherit; outline:none;
}
textarea{resize:vertical; min-height:76px; font-family:ui-monospace,Consolas,monospace;
  font-size:13px}
input:focus,textarea:focus,select:focus{border-color:var(--accent)}
.row{display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end}
.row > div{flex:1; min-width:150px}
button{
  background:var(--accent); color:#fff; border:0; border-radius:8px;
  padding:10px 20px; font:inherit; font-weight:600; cursor:pointer;
  transition:filter .15s;
}
button:hover{filter:brightness(1.12)}
button:disabled{opacity:.5; cursor:not-allowed}
button.ghost{background:transparent; border:1px solid var(--line); color:var(--soft);
  font-weight:500; padding:8px 14px}
button.ghost:hover{color:var(--fg); border-color:var(--accent)}
.opts{display:flex; gap:18px; flex-wrap:wrap; margin-top:14px; padding-top:14px;
  border-top:1px solid var(--line); font-size:13px; color:var(--soft)}
.opts label{display:flex; align-items:center; gap:6px; text-transform:none;
  letter-spacing:0; font-size:13px; color:var(--soft); cursor:pointer; margin:0}
.opts input[type=checkbox]{width:auto}
h2{font-size:13px; text-transform:uppercase; letter-spacing:.8px; color:var(--soft);
  margin:0 0 12px; font-weight:600}
.job{border:1px solid var(--line); border-radius:10px; padding:12px 14px;
  margin-bottom:10px; background:var(--panel2)}
.job .top{display:flex; gap:10px; align-items:center; flex-wrap:wrap}
.job .u{flex:1; word-break:break-all; font-size:13px; min-width:200px}
.job .t{font-weight:600}
.pill{font-size:11px; padding:2px 8px; border-radius:99px; border:1px solid var(--line);
  color:var(--soft); white-space:nowrap}
.pill.ok{color:var(--ok); border-color:rgba(61,220,151,.4)}
.pill.fail{color:var(--err); border-color:rgba(255,107,107,.4)}
.pill.run{color:var(--warn); border-color:rgba(255,200,87,.4)}
.pill.mode{color:var(--accent2); border-color:rgba(122,162,247,.35)}
.log{margin-top:8px; font-family:ui-monospace,Consolas,monospace; font-size:12px;
  color:var(--soft); background:#12141b; border-radius:6px; padding:8px 10px;
  max-height:150px; overflow:auto; white-space:pre-wrap; word-break:break-all;
  display:none}
.log.show{display:block}
.meta{margin-top:7px; font-size:12px; color:var(--soft); display:flex; gap:14px;
  flex-wrap:wrap}
.meta b{color:var(--fg); font-weight:600}
.empty{color:var(--soft); text-align:center; padding:30px; font-size:13px}
.err{color:var(--err)}
.hint{font-size:12px; color:var(--soft); margin-top:8px; line-height:1.6}
kbd{background:var(--panel2); border:1px solid var(--line); border-bottom-width:2px;
  border-radius:4px; padding:1px 5px; font-size:11px; font-family:inherit}
a{color:var(--accent2)}
.spin{display:inline-block; width:10px; height:10px; border:2px solid var(--warn);
  border-right-color:transparent; border-radius:50%; animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.tabs{display:flex; gap:4px; margin-bottom:16px; border-bottom:1px solid var(--line)}
.tabs button{background:transparent; border:0; border-bottom:2px solid transparent;
  color:var(--soft); font-weight:500; padding:9px 16px; border-radius:0; font-size:13px}
.tabs button.on{color:var(--fg); border-bottom-color:var(--accent)}
.pane{display:none}
.pane.on{display:block}
.link{display:flex; gap:10px; align-items:center; padding:8px 10px; border-radius:8px;
  border:1px solid var(--line); margin-bottom:6px; background:var(--panel2)}
.link .txt{flex:1; min-width:0}
.link .txt b{display:block; font-weight:600; font-size:13px; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis}
.link .txt span{color:var(--soft); font-size:11.5px; word-break:break-all}
.link .sc{color:var(--ok); font-size:11px; white-space:nowrap}
.link input{width:auto}
.lib{display:grid; gap:10px; grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
.lib a{display:block; padding:11px 13px; border:1px solid var(--line); border-radius:9px;
  background:var(--panel2); text-decoration:none; color:var(--fg); font-size:13px}
.lib a:hover{border-color:var(--accent)}
.lib .m{color:var(--soft); font-size:11.5px; margin-top:4px; display:flex; gap:10px}
.toolbar{display:flex; gap:10px; align-items:center; flex-wrap:wrap;
  margin-bottom:14px}
.toolbar .sp{flex:1}
</style>
</head>
<body>
<header>
  <h1>WebVault</h1>
  <span class="sub">网页 → 单文件离线副本</span>
  <span class="sp"></span>
  <span class="ver">v__VERSION__</span>
</header>
<main>
  <div class="tabs">
    <button data-tab="save" class="on">保存网页</button>
    <button data-tab="find">发现链接</button>
    <button data-tab="lib">我的书库</button>
  </div>

  <!-- ============ 保存 ============ -->
  <div class="pane on" id="pane-save">
  <div class="card">
    <label>网址（一行一个，支持批量）</label>
    <textarea id="urls" placeholder="https://example.com&#10;https://www.ruanyifeng.com/blog/2024/01/weekly-issue-286.html"></textarea>
    <div class="row" style="margin-top:14px">
      <div style="max-width:190px">
        <label>模式</label>
        <select id="mode">
          <option value="reader">reader · 正文阅读</option>
          <option value="full">full · 完整快照</option>
          <option value="data">data · 结构化 JSON</option>
        </select>
      </div>
      <div style="max-width:150px">
        <label>总大小上限 (MB)</label>
        <input id="maxtotal" type="number" value="24" min="1">
      </div>
      <div style="max-width:150px">
        <label>单资源上限 (MB)</label>
        <input id="maxasset" type="number" value="4" min="1">
      </div>
      <div style="flex:0 0 auto">
        <button id="go">保存</button>
      </div>
      <div style="flex:0 0 auto">
        <button class="ghost" id="clear">清空记录</button>
      </div>
    </div>
    <div class="opts">
      <label><input type="checkbox" id="opt_img" checked> 内联图片</label>
      <label><input type="checkbox" id="opt_css" checked> 内联 CSS</label>
      <label><input type="checkbox" id="opt_compress" checked> 压缩大图</label>
      <label><input type="checkbox" id="opt_local"> 仅同域资源</label>
      <label><input type="checkbox" id="opt_nocss"> 干净排版（丢原站样式）</label>
    </div>
    <div class="hint">
      保存目录：<b id="outdir">-</b> · <kbd>Ctrl</kbd>+<kbd>Enter</kbd> 开始
    </div>
  </div>

  <h2>任务</h2>
  <div id="jobs"><div class="empty">还没有任务</div></div>
  </div>

  <!-- ============ 发现 ============ -->
  <div class="pane" id="pane-find">
    <div class="card">
      <label>列表页网址</label>
      <input id="durl" placeholder="https://www.ruanyifeng.com/blog/">
      <div class="row" style="margin-top:14px">
        <div style="max-width:130px">
          <label>最多几条</label>
          <input id="dlimit" type="number" value="30" min="1" max="400">
        </div>
        <div style="flex:0 0 auto">
          <button id="dgo">发现链接</button>
        </div>
      </div>
      <div class="opts">
        <label><input type="checkbox" id="dany"> 允许跨域链接</label>
      </div>
      <div class="hint">
        从这一页里挑出像文章的链接。导航、登录、标签页、资源文件会被自动排除。
      </div>
    </div>
    <div id="dresult"></div>
  </div>

  <!-- ============ 书库 ============ -->
  <div class="pane" id="pane-lib">
    <div class="toolbar">
      <button id="lscan" class="ghost">重新扫描</button>
      <button id="lbuild">生成索引页</button>
      <span class="sp"></span>
      <span class="hint" id="lstat">-</span>
    </div>
    <div id="lresult"><div class="empty">点「重新扫描」看看存了什么</div></div>
  </div>
</main>

<script>
const $ = s => document.querySelector(s);
let jobs = {};
let timer = null;

fetch('api/info').then(r=>r.json()).then(d=>{
  $('#outdir').textContent = d.out_dir;
  if (d.clean_css_default) $('#opt_nocss').checked = true;
});

function modeLabel(m){
  return {reader:'正文', full:'快照', data:'数据'}[m] || m;
}

function esc(s){
  return String(s==null?'':s).replace(/[&<>"']/g, c=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function render(){
  const ids = Object.keys(jobs).sort((a,b)=> jobs[b].started - jobs[a].started);
  const box = $('#jobs');
  if(!ids.length){ box.innerHTML = '<div class="empty">还没有任务</div>'; return; }
  box.innerHTML = ids.map(id=>{
    const j = jobs[id];
    const st = j.status;
    const pill = st==='running'
      ? '<span class="pill run"><span class="spin"></span> 进行中</span>'
      : st==='ok' ? '<span class="pill ok">完成</span>'
                  : '<span class="pill fail">失败</span>';
    const log = (j.log||[]).length
      ? '<div class="log show">'+ esc(j.log.slice(-8).join('\n')) +'</div>' : '';
    const size = j.size ? '<span>大小 <b>'+ j.size_h +'</b></span>' : '';
    const el = j.elapsed ? '<span>耗时 <b>'+ j.elapsed.toFixed(1) +'s</b></span>' : '';
    const stats = j.stats ? '<span>'+ esc(j.stats) +'</span>' : '';
    const err = j.error ? '<div class="meta err">'+ esc(j.error) +'</div>' : '';
    const title = j.title ? '<div class="t">'+ esc(j.title) +'</div>' : '';
    return '<div class="job">'
      + '<div class="top">'
      +   '<span class="pill mode">'+ esc(modeLabel(j.mode)) +'</span>'
      +   pill
      +   '<span class="u">'+ esc(j.url) +'</span>'
      + '</div>'
      + title
      + '<div class="meta">'+ size + el + stats +'</div>'
      + err + log
      + '</div>';
  }).join('');
}

function poll(){
  fetch('api/jobs').then(r=>r.json()).then(list=>{
    let anyRun = false;
    list.forEach(j=>{
      j.size_h = fmtSize(j.size);
      jobs[j.id] = j;
      if(j.status==='running') anyRun = true;
    });
    render();
    if(anyRun && !timer) timer = setInterval(poll, 900);
    if(!anyRun && timer){ clearInterval(timer); timer = null; }
  }).catch(()=>{});
}

function fmtSize(n){
  if(!n) return '-';
  const u=['B','KB','MB','GB']; let i=0, v=n;
  while(v>=1024 && i<u.length-1){ v/=1024; i++; }
  return (i===0? v : v.toFixed(i===1?1:2)) + ' ' + u[i];
}

function submit(){
  const raw = $('#urls').value.trim();
  if(!raw) return;
  const urls = raw.split(/\n+/).map(s=>s.trim()).filter(s=>s && !s.startsWith('#'));
  if(!urls.length) return;
  const payload = {
    urls: urls,
    mode: $('#mode').value,
    max_total: parseFloat($('#maxtotal').value)||24,
    max_asset: parseFloat($('#maxasset').value)||4,
    images: $('#opt_img').checked,
    css: $('#opt_css').checked,
    compress: $('#opt_compress').checked,
    local_only: $('#opt_local').checked,
    keep_site_css: !$('#opt_nocss').checked,
  };
  $('#go').disabled = true;
  fetch('api/vault', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  }).then(r=>r.json()).then(d=>{
    $('#go').disabled = false;
    (d.ids||[]).forEach(id=>{
      jobs[id] = {id:id, url:'…', mode:payload.mode, status:'running',
                  log:[], started:Date.now()/1000, size:0, elapsed:0};
    });
    render();
    poll();
  }).catch(e=>{
    $('#go').disabled = false;
    alert('请求失败: '+e);
  });
}

$('#go').onclick = submit;
$('#clear').onclick = ()=>{ jobs={}; render(); };
document.addEventListener('keydown', e=>{
  if((e.ctrlKey||e.metaKey) && e.key==='Enter') submit();
});
poll();

/* ---------------- 标签页 ---------------- */
document.querySelectorAll('.tabs button').forEach(b=>{
  b.onclick = ()=>{
    document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));
    document.querySelectorAll('.pane').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
    $('#pane-'+b.dataset.tab).classList.add('on');
    if(b.dataset.tab==='lib') scanLib();
  };
});

/* ---------------- 发现链接 ---------------- */
let found = [];

$('#dgo').onclick = ()=>{
  const url = $('#durl').value.trim();
  if(!url) return;
  const btn = $('#dgo');
  btn.disabled = true;
  btn.textContent = '发现中…';
  $('#dresult').innerHTML = '<div class="empty"><span class="spin"></span> 正在抓取并分析…</div>';
  fetch('api/discover', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      url: url,
      limit: parseInt($('#dlimit').value)||30,
      any_host: $('#dany').checked
    })
  }).then(r=>r.json()).then(d=>{
    btn.disabled = false; btn.textContent = '发现链接';
    if(d.error){ $('#dresult').innerHTML = '<div class="empty err">'+esc(d.error)+'</div>'; return; }
    found = d.links || [];
    renderFound(d);
  }).catch(e=>{
    btn.disabled = false; btn.textContent = '发现链接';
    $('#dresult').innerHTML = '<div class="empty err">请求失败：'+esc(e)+'</div>';
  });
};

function renderFound(d){
  const box = $('#dresult');
  if(!found.length){
    box.innerHTML = '<div class="empty">没有发现符合条件的链接。可以试试勾上「允许跨域链接」。</div>';
    return;
  }
  const rows = found.map((l,i)=>
    '<div class="link">'
    + '<input type="checkbox" class="ck" data-i="'+i+'" checked>'
    + '<div class="txt"><b>'+esc(l.text||'(无标题)')+'</b><span>'+esc(l.url)+'</span></div>'
    + '<span class="sc">'+l.score.toFixed(0)+'</span>'
    + '</div>').join('');
  box.innerHTML = '<div class="toolbar">'
    + '<span class="hint">共 <b>'+found.length+'</b> 条 · 来源 '+esc(d.source||'')+'</span>'
    + '<span class="sp"></span>'
    + '<button class="ghost" id="dall">全选/全不选</button>'
    + '<button id="dsave">保存选中的</button>'
    + '</div><div>'+rows+'</div>';

  $('#dall').onclick = ()=>{
    const cks = box.querySelectorAll('.ck');
    const all = Array.from(cks).every(c=>c.checked);
    cks.forEach(c=>c.checked = !all);
  };
  $('#dsave').onclick = ()=>{
    const sel = Array.from(box.querySelectorAll('.ck'))
      .filter(c=>c.checked).map(c=>found[+c.dataset.i].url);
    if(!sel.length){ alert('没有选中任何链接'); return; }
    $('#urls').value = sel.join('\n');
    document.querySelector('.tabs button[data-tab=save]').click();
    submit();
  };
}

/* ---------------- 书库 ---------------- */
function scanLib(){
  const box = $('#lresult');
  box.innerHTML = '<div class="empty"><span class="spin"></span> 扫描中…</div>';
  fetch('api/library').then(r=>r.json()).then(d=>{
    if(d.error){ box.innerHTML = '<div class="empty err">'+esc(d.error)+'</div>'; return; }
    $('#lstat').innerHTML = '共 <b>'+d.count+'</b> 项 · '+fmtSize(d.total)+' · '+esc(d.dir);
    if(!d.items.length){
      box.innerHTML = '<div class="empty">书库还是空的，先去「保存网页」存几个。</div>';
      return;
    }
    box.innerHTML = '<div class="lib">' + d.items.map(it=>
      '<a href="/api/file?p='+encodeURIComponent(d.dir.replace(/[\\/]+$/,'')+'/'+it.path)+'" target="_blank">'
      + esc(it.title)
      + '<div class="m"><span>'+esc(it.mode||'')+'</span><span>'+esc(it.size_h)+'</span>'
      + (it.image_count?'<span>'+it.image_count+' 图</span>':'')
      + '<span>'+esc((it.host||'').slice(0,26))+'</span></div>'
      + '<div class="m"><span>'+esc(it.captured||'')+'</span></div>'
      + '</a>').join('') + '</div>';
  }).catch(e=>{
    box.innerHTML = '<div class="empty err">扫描失败：'+esc(e)+'</div>';
  });
}

$('#lscan').onclick = scanLib;
$('#lbuild').onclick = ()=>{
  const btn = $('#lbuild');
  btn.disabled = true; btn.textContent = '生成中…';
  fetch('api/index', {method:'POST', headers:{'Content-Type':'application/json'},
                      body:'{}'})
    .then(r=>r.json()).then(d=>{
      btn.disabled = false; btn.textContent = '生成索引页';
      if(d.error){ alert('生成失败：'+d.error); return; }
      alert('索引页已生成：\n'+d.path+'\n\n收录 '+d.count+' 项。\n\n'
            +'可以在浏览器里直接打开这个文件，或者用命令行：\n'
            +'python webvault.py index');
    }).catch(e=>{
      btn.disabled = false; btn.textContent = '生成索引页';
      alert('请求失败：'+e);
    });
};
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------

def _num(payload: dict, key: str, default: float,
         lo: float = 0.0, hi: float = 1e9) -> float:
    """从请求里取一个数字，容错：非法值退回默认值并夹到合理区间。

    前端可能传来 ""、"abc"、null —— 这些都不该让服务端 500。
    """
    try:
        v = payload.get(key, default)
        if v is None or v == "":
            return default
        v = float(v)
        if v != v:  # NaN
            return default
        return max(lo, min(hi, v))
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "WebVault/%s" % __version__

    def log_message(self, fmt, *args):
        # 安静一点，不刷屏
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 本地工具，避免被浏览器当作跨站资源
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                body = PAGE.replace("__VERSION__", __version__).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
            elif path == "/api/info":
                self._json({
                    "version": __version__,
                    "out_dir": STATE["out_dir"],
                    "clean_css_default": False,
                })
            elif path == "/api/jobs":
                with STATE["lock"]:
                    items = sorted(STATE["jobs"].values(),
                                   key=lambda j: j["started"], reverse=True)
                    # 只发最近的，避免界面卡
                    out = []
                    for j in items[:60]:
                        out.append({
                            "id": j["id"], "url": j["url"], "mode": j["mode"],
                            "status": j["status"], "log": j["log"][-40:],
                            "title": j["title"], "path": j["path"],
                            "size": j["size"], "error": j["error"],
                            "elapsed": j["elapsed"], "stats": j["stats"],
                            "started": j["started"],
                        })
                self._json(out)
            elif path == "/api/library":
                # 扫描输出目录，返回书库条目（不在界面上直接建索引文件）
                from src.library import scan
                out_dir = STATE["out_dir"]
                entries = scan(out_dir)
                self._json({
                    "dir": out_dir,
                    "index_url": "/api/file?p=" + quote(
                        os.path.join(os.path.abspath(out_dir), "index.html")),
                    "index_exists": os.path.isfile(
                        os.path.join(os.path.abspath(out_dir), "index.html")),
                    "count": len(entries),
                    "total": sum(e.size for e in entries),
                    "items": [e.to_dict() for e in entries[:400]],
                })
            elif path == "/api/file":
                # 预览：把刚保存的文件发回去
                qs = parse_qs(urlparse(self.path).query)
                target = unquote((qs.get("p") or [""])[0])
                out_dir = os.path.abspath(STATE["out_dir"])
                ap = os.path.abspath(target) if target else ""
                # 只允许读输出目录里的文件
                if not ap or not ap.startswith(out_dir) or not os.path.isfile(ap):
                    self._json({"error": "不允许访问该路径"}, 403)
                    return
                with open(ap, "rb") as f:
                    data = f.read()
                ctype = ("text/html; charset=utf-8"
                         if ap.lower().endswith(".html")
                         else "application/json; charset=utf-8"
                         if ap.lower().endswith(".json")
                         else "application/octet-stream")
                self._send(200, data, ctype)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path == "/api/discover":
                self._handle_discover()
                return
            if path == "/api/index":
                self._handle_index()
                return
            if path != "/api/vault":
                self._json({"error": "not found"}, 404)
                return

            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1024 * 1024:
                self._json({"error": "请求体无效"}, 400)
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self._json({"error": "JSON 解析失败"}, 400)
                return

            urls = payload.get("urls") or []
            if isinstance(urls, str):
                urls = [urls]
            urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
            # 粗暴但必要的上限，防止误粘贴几千行
            urls = urls[:60]
            if not urls:
                self._json({"error": "没有有效网址"}, 400)
                return

            mode = payload.get("mode") or "reader"
            if mode not in ("reader", "full", "data"):
                mode = "reader"

            opts = InlineOptions(
                images=bool(payload.get("images", True)),
                css=bool(payload.get("css", True)),
                fonts=bool(payload.get("fonts", True)),
                scripts=("drop" if mode == "reader" else "keep"),
                max_total_bytes=int(_num(payload, "max_total", 24, 1, 4096) * 1024 * 1024),
                max_asset_bytes=int(_num(payload, "max_asset", 4, 1, 1024) * 1024 * 1024),
                compress_images=bool(payload.get("compress", True)),
                allow_external=not bool(payload.get("local_only", False)),
            )
            out_dir = STATE["out_dir"]
            keep_site_css = bool(payload.get("keep_site_css", False))
            timeout = _num(payload, "timeout", 25.0, 5.0, 120.0)

            ids = []
            for u in urls:
                jid = new_job(u, mode)
                ids.append(jid)
                t = threading.Thread(
                    target=run_job,
                    args=(jid, u, mode, opts, out_dir, timeout, keep_site_css),
                    daemon=True,
                )
                t.start()
                # 轻微错开，避免瞬间几十个连接把网络打满
                if len(urls) > 1:
                    time.sleep(0.12)
            self._json({"ids": ids, "count": len(urls)})
        except (ValueError, TypeError, KeyError) as e:
            # 请求内容的问题 → 400，不是服务端故障
            try:
                self._json({"error": "请求参数有误: %s" % e}, 400)
            except Exception:
                pass
        except Exception as e:
            try:
                self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 书库 / 链接发现
    # ------------------------------------------------------------------
    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1024 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def _handle_index(self):
        """把输出目录扫一遍，生成书库索引页。"""
        from src.library import build_index
        out_dir = STATE["out_dir"]
        try:
            os.makedirs(out_dir, exist_ok=True)
            path, entries = build_index(out_dir, title="WebVault 书库")
        except Exception as e:
            self._json({"error": "生成索引失败: %s" % e}, 500)
            return
        self._json({
            "ok": True,
            "path": path,
            "count": len(entries),
            "total": sum(e.size for e in entries),
        })

    def _handle_discover(self):
        """从一个页面里发现候选文章链接。"""
        from src.discover import discover
        from src.fetch import fetch

        payload = self._read_json()
        if payload is None:
            self._json({"error": "请求体无效"}, 400)
            return
        url = (payload.get("url") or "").strip()
        if not url:
            self._json({"error": "缺少 url"}, 400)
            return
        if "://" not in url:
            url = "http://" + url
        timeout = _num(payload, "timeout", 25.0, 5.0, 120.0)
        limit = int(_num(payload, "limit", 50, 1, 400))
        same_host = not bool(payload.get("any_host", False))

        fr = fetch(url, timeout=timeout, retries=2)
        if not fr.body:
            self._json({"error": "抓取失败：%s" % (fr.error or "空响应")}, 502)
            return
        links = discover(fr.text, fr.final_url or url, same_host_only=same_host,
                         limit=limit)
        self._json({
            "ok": True,
            "source": fr.final_url or url,
            "count": len(links),
            "links": [l.to_dict() for l in links],
        })


def _setup_console() -> None:
    try:
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main(argv=None) -> int:
    _setup_console()
    p = argparse.ArgumentParser(
        prog="webvault-ui",
        description="WebVault 本地操作台（零依赖）",
    )
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p.add_argument("--port", type=int, default=8770, help="监听端口（默认 8770）")
    p.add_argument("--out", default="", help="保存目录（默认 桌面/WebVault）")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = p.parse_args(argv)

    if args.out:
        out_dir = os.path.abspath(args.out)
    else:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        base = desktop if os.path.isdir(desktop) else os.path.expanduser("~")
        out_dir = os.path.join(base, "WebVault")
    os.makedirs(out_dir, exist_ok=True)
    STATE["out_dir"] = out_dir

    port = args.port
    httpd = None
    for attempt in range(12):
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            port += 1
    if httpd is None:
        print("无法绑定端口，请用 --port 指定一个空闲端口", file=sys.stderr)
        return 1

    url = "http://%s:%d/" % (args.host, port)
    print("WebVault v%s" % __version__)
    print("  界面   %s" % url)
    print("  保存   %s" % out_dir)
    print("  停止   Ctrl+C")
    print()

    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
