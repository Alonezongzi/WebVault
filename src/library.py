"""离线书库 —— 把一堆保存下来的页面变成可浏览、可搜索的索引页。

解决的问题：
  存了 50 篇文章，然后找不到任何一篇。文件夹里全是文件名，
  没有标题、没有来源、没有日期、没有预览。

做法：
  扫描输出目录里 WebVault 产出的 .html / .json，
  从产物内嵌的元信息里读出 标题/来源/抓取时间/正文摘要，
  生成一个**同样自包含**的 index.html —— 双击就能开，能搜索、能筛选、能排序。

产物本身也是零外部请求的，和它索引的页面一样。
"""

from __future__ import annotations

import html as _html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .util import escape, human_size, url_host

# 从产物 HTML 里读元信息（比重新解析整棵树快得多，产物有几十 MB）
_META_RE = re.compile(
    r'<meta\s+name="(wv-source|wv-captured|wv-mode|wv-title)"\s+content="([^"]*)"',
    re.I,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# reader 模式的摘要来自 wv-meta 里的描述块
_EXCERPT_RE = re.compile(
    r'<div class="wv-meta">.*?</div>\s*</div>', re.I | re.S
)
_BADGE_RE = re.compile(r'<span class="wv-badge">([^<]*)</span>', re.I)


@dataclass
class Entry:
    """书库里的一条记录。"""

    path: str = ""
    rel_path: str = ""
    title: str = ""
    source: str = ""
    host: str = ""
    captured: str = ""
    mode: str = ""
    size: int = 0
    kind: str = "html"          # html | json
    excerpt: str = ""
    word_count: int = 0
    image_count: int = 0
    mtime: float = 0.0
    error: str = ""

    def to_dict(self) -> Dict:
        return {
            "path": self.rel_path,
            "title": self.title,
            "source": self.source,
            "host": self.host,
            "captured": self.captured,
            "mode": self.mode,
            "size": self.size,
            "size_h": human_size(self.size),
            "kind": self.kind,
            "excerpt": self.excerpt,
            "word_count": self.word_count,
            "image_count": self.image_count,
        }


def _read_meta_html(path: str, max_head: int = 200 * 1024) -> Dict[str, str]:
    """只读文件开头的一段来抽取 meta。

    产物可能有一二十 MB（图片全是 data URI），整读再正则太浪费。
    元信息都在 <head> 和顶部，读前 200KB 足够。
    """
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(max_head)
    except Exception:
        return out
    for m in _META_RE.finditer(head):
        out[m.group(1).lower()] = _html.unescape(m.group(2))
    tm = _TITLE_RE.search(head)
    if tm:
        out["_title"] = _html.unescape(re.sub(r"\s+", " ", tm.group(1))).strip()
    # 摘要：wv-meta 里的斜体描述
    em = re.search(r'<div style="margin-top:\.5rem;font-style:italic;opacity:\.85">(.*?)</div>',
                   head, re.S)
    if em:
        out["_excerpt"] = _html.unescape(re.sub(r"<[^>]+>", "", em.group(1))).strip()
    # 徽章里的字数/图数
    for b in _BADGE_RE.finditer(head):
        t = b.group(1)
        mm = re.match(r"正文\s*(\d+)\s*字", t)
        if mm:
            out["_words"] = mm.group(1)
        mm = re.match(r"图\s*(\d+)", t)
        if mm:
            out["_imgs"] = mm.group(1)
    return out


def _read_meta_json(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return out
        out["wv-captured"] = str(data.get("captured") or "")
        out["wv-meta"] = str(data.get("generator") or "")
        out["_title"] = str(data.get("title") or "")
        out["wv-source"] = str(data.get("url") or data.get("final_url") or "")
        out["_excerpt"] = str(data.get("excerpt") or "")
        out["_words"] = str(data.get("word_count") or "")
        imgs = data.get("images")
        if isinstance(imgs, list):
            out["_imgs"] = str(len(imgs))
        out["wv-mode"] = "data"
    except Exception:
        pass
    return out


def scan(directory: str, *, recursive: bool = True,
         include_json: bool = True) -> List[Entry]:
    """扫描目录，返回所有 WebVault 产物。"""
    entries: List[Entry] = []
    if not directory or not os.path.isdir(directory):
        return entries

    exts = (".html", ".htm")
    if include_json:
        exts = exts + (".json",)

    walker = os.walk(directory) if recursive else [(directory, [], os.listdir(directory))]
    for root, _dirs, files in walker:
        # 跳过隐藏目录（.webvault 存的是变更历史，不是产物）
        if os.path.basename(root).startswith("."):
            _dirs[:] = []
            continue
        _dirs[:] = [d for d in _dirs if not d.startswith(".")]
        for name in files:
            low = name.lower()
            if not low.endswith(exts):
                continue
            if low == "index.html" and root == directory:
                # 别把书库自己的索引收进去
                continue
            full = os.path.join(root, name)
            try:
                size = os.path.getsize(full)
                mtime = os.path.getmtime(full)
            except Exception:
                continue

            rel = os.path.relpath(full, directory)
            is_json = low.endswith(".json")
            meta = _read_meta_json(full) if is_json else _read_meta_html(full)

            # 只收 WebVault 自己产出的东西（有 wv-source 或 wv-captured），
            # 除非是 json（json 可能是 data 模式产物）
            if not is_json:
                if not (meta.get("wv-source") or meta.get("wv-captured")):
                    continue

            src = meta.get("wv-source", "")
            title = (meta.get("_title") or "").strip()
            if not title:
                title = os.path.splitext(name)[0]

            e = Entry(
                path=full,
                rel_path=rel.replace("\\", "/"),
                title=title,
                source=src,
                host=url_host(src) or "",
                captured=meta.get("wv-captured", ""),
                mode=meta.get("wv-mode", "") or ("data" if is_json else "reader"),
                size=size,
                kind="json" if is_json else "html",
                excerpt=(meta.get("_excerpt") or "")[:200],
                word_count=int(meta.get("_words") or 0),
                image_count=int(meta.get("_imgs") or 0),
                mtime=mtime,
            )
            if not e.captured:
                try:
                    e.captured = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    e.captured = ""
            entries.append(e)

    entries.sort(key=lambda x: x.mtime, reverse=True)
    return entries


# --------------------------------------------------------------------------
# 索引页渲染
# --------------------------------------------------------------------------

INDEX_CSS = """
:root{
  --bg:#0e0f13; --panel:#16181f; --panel2:#1d2029; --fg:#e9e9ec; --soft:#8f96a6;
  --line:#282c38; --accent:#5b8cff; --accent2:#7aa2f7; --ok:#3ddc97; --json:#ffc857;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--fg); min-height:100vh;
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Hiragino Sans GB","Microsoft YaHei",sans-serif;
}
header{
  position:sticky; top:0; z-index:10; backdrop-filter:blur(10px);
  background:linear-gradient(180deg,rgba(22,24,31,.97),rgba(18,19,25,.94));
  border-bottom:1px solid var(--line); padding:14px 20px;
}
.hrow{display:flex; align-items:center; gap:12px; flex-wrap:wrap; max-width:1200px;
  margin:0 auto}
h1{margin:0; font-size:16px; font-weight:650; letter-spacing:.3px}
.stat{color:var(--soft); font-size:12px}
.stat b{color:var(--fg); font-weight:600}
.sp{flex:1}
input[type=search],select{
  background:var(--panel2); border:1px solid var(--line); color:var(--fg);
  border-radius:8px; padding:8px 12px; font:inherit; outline:none; font-size:13px;
}
input[type=search]{width:min(340px,46vw)}
input:focus,select:focus{border-color:var(--accent)}
main{max-width:1200px; margin:0 auto; padding:18px 20px 60px}
.grid{display:grid; gap:12px;
  grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.card{
  background:var(--panel); border:1px solid var(--line); border-radius:11px;
  padding:13px 15px; display:flex; flex-direction:column; gap:7px;
  transition:border-color .15s, transform .1s;
}
.card:hover{border-color:var(--accent); transform:translateY(-1px)}
.card a.t{color:var(--fg); text-decoration:none; font-weight:600; font-size:14.5px;
  line-height:1.4; display:block}
.card a.t:hover{color:var(--accent2)}
.card .u{color:var(--soft); font-size:11.5px; word-break:break-all}
.card .u a{color:var(--soft); text-decoration:none}
.card .u a:hover{color:var(--accent2); text-decoration:underline}
.ex{color:var(--soft); font-size:12px; line-height:1.55;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
  overflow:hidden}
.meta{display:flex; gap:10px; flex-wrap:wrap; align-items:center;
  color:var(--soft); font-size:11.5px; margin-top:auto; padding-top:4px}
.tag{border:1px solid var(--line); border-radius:99px; padding:1px 8px; font-size:11px}
.tag.json{color:var(--json); border-color:rgba(255,200,87,.35)}
.tag.html{color:var(--accent2); border-color:rgba(122,162,247,.3)}
.empty{text-align:center; color:var(--soft); padding:60px 20px; font-size:13px}
footer{text-align:center; color:var(--soft); font-size:11.5px; padding:30px 20px}
mark{background:rgba(91,140,255,.3); color:inherit; border-radius:2px}
"""

INDEX_JS = """
(function(){
  var q = document.getElementById('q');
  var sort = document.getElementById('sort');
  var mode = document.getElementById('mode');
  var grid = document.getElementById('grid');
  var countEl = document.getElementById('shown');
  var cards = Array.prototype.slice.call(grid.querySelectorAll('.card'));
  if(!cards.length) return;

  function norm(s){ return (s||'').toLowerCase(); }

  function apply(){
    var term = norm(q.value.trim());
    var m = mode.value;
    var shown = 0;
    cards.forEach(function(c){
      var hay = c.getAttribute('data-search') || '';
      var cm = c.getAttribute('data-mode') || '';
      var okTerm = !term || hay.indexOf(term) >= 0;
      var okMode = !m || cm === m;
      var vis = okTerm && okMode;
      c.style.display = vis ? '' : 'none';
      if(vis) shown++;
    });
    countEl.textContent = shown;
  }

  function sortCards(){
    var by = sort.value;
    var sorted = cards.slice().sort(function(a,b){
      if(by === 'title'){
        return (a.getAttribute('data-title')||'').localeCompare(
               (b.getAttribute('data-title')||''), 'zh');
      }
      if(by === 'size'){
        return (+b.getAttribute('data-size')||0) - (+a.getAttribute('data-size')||0);
      }
      if(by === 'host'){
        return (a.getAttribute('data-host')||'').localeCompare(
               (b.getAttribute('data-host')||''));
      }
      return (+b.getAttribute('data-mtime')||0) - (+a.getAttribute('data-mtime')||0);
    });
    sorted.forEach(function(c){ grid.appendChild(c); });
  }

  q.addEventListener('input', apply);
  mode.addEventListener('change', apply);
  sort.addEventListener('change', function(){ sortCards(); });
  apply();
})();
"""


def _card_html(e: Entry) -> str:
    """单个卡片。"""
    src_disp = e.source or "(未知来源)"
    host = e.host or ""
    search = " ".join([e.title, e.source, host, e.excerpt]).lower()
    search_attr = escape(search)

    excerpt = ('<div class="ex">%s</div>' % escape(e.excerpt)) if e.excerpt else ""
    words = ('<span>%d 字</span>' % e.word_count) if e.word_count else ""
    imgs = ('<span>%d 图</span>' % e.image_count) if e.image_count else ""
    src_link = (
        '<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>'
        % (escape(e.source), escape(host or src_disp))
    ) if e.source else escape(src_disp)

    return (
        '<div class="card" data-search="{search}" data-mode="{mode}" '
        'data-title="{t}" data-size="{size}" data-host="{host}" data-mtime="{mt}">'
        '<a class="t" href="{href}">{title}</a>'
        '<div class="u">{src}</div>'
        "{excerpt}"
        '<div class="meta">'
        '<span class="tag {kindcls}">{kindlabel}</span>'
        "<span>{size_h}</span>"
        "{words}{imgs}"
        "<span style=\"margin-left:auto\">{captured}</span>"
        "</div></div>"
    ).format(
        search=search_attr,
        mode=escape(e.mode or "reader"),
        t=escape(e.title),
        size=e.size,
        host=escape(e.host),
        mt=e.mtime,
        href=escape(e.rel_path),
        title=escape(e.title),
        src=src_link,
        excerpt=excerpt,
        kindcls="json" if e.kind == "json" else "html",
        kindlabel="数据" if e.kind == "json" else "网页",
        size_h=escape(human_size(e.size)),
        words=words,
        imgs=imgs,
        captured=escape(e.captured or ""),
    )


def render_index(entries: List[Entry], *, title: str = "WebVault 书库",
                 out_dir: str = "") -> str:
    """渲染索引页。这个页面本身也是零外部依赖的。"""
    total = sum(e.size for e in entries)
    hosts = sorted({e.host for e in entries if e.host})
    if entries:
        newest = entries[0].captured or ""
        oldest = entries[-1].captured or ""
    else:
        newest = oldest = ""

    cards = "\n".join(_card_html(e) for e in entries)
    if not cards:
        cards = ('<div class="empty">书库还是空的 —— 先用 '
                 '<code>python webvault.py &lt;网址&gt;</code> 存几个页面试试。</div>')

    host_opts = "".join(
        '<option value="%s">%s</option>' % (escape(h), escape(h)) for h in hosts[:200]
    )

    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="generator" content="WebVault">
<style>
{css}
</style>
</head>
<body>
<header>
  <div class="hrow">
    <h1>{title}</h1>
    <span class="stat"><b id="shown">{n}</b> / {n} 项 · <b>{total}</b> · {hosts} 个来源</span>
    <span class="sp"></span>
    <input type="search" id="q" placeholder="搜索标题 / 网址 / 摘要…" autocomplete="off">
    <select id="mode" title="按类型筛选">
      <option value="">全部类型</option>
      <option value="reader">reader</option>
      <option value="full">full</option>
      <option value="data">data</option>
    </select>
    <select id="sort" title="排序方式">
      <option value="date">按时间</option>
      <option value="title">按标题</option>
      <option value="size">按体积</option>
      <option value="host">按来源站</option>
    </select>
  </div>
</header>
<main>
  <div class="grid" id="grid">
{cards}
  </div>
</main>
<footer>
  由 WebVault 生成 · {generated} · 最早 {oldest} · 最新 {newest}
</footer>
<script>
{js}
</script>
</body>
</html>
""".format(
        title=escape(title),
        css=INDEX_CSS,
        js=INDEX_JS,
        n=len(entries),
        total=escape(human_size(total)),
        hosts=len(hosts),
        cards=cards,
        generated=escape(datetime.now().strftime("%Y-%m-%d %H:%M")),
        oldest=escape(oldest[:16]),
        newest=escape(newest[:16]),
    )


def build_index(directory: str, *, out_name: str = "index.html",
                out_path: str = "", title: str = "WebVault 书库",
                recursive: bool = True, write: bool = True) -> Tuple[str, List[Entry]]:
    """扫描目录并生成索引页。返回 (索引文件路径, 条目列表)。

    out_path 可以是一个具体文件，也可以是一个目录（此时用 out_name 作为文件名）。
    """
    entries = scan(directory, recursive=recursive)
    html = render_index(entries, title=title, out_dir=directory)

    path = ""
    if write and directory:
        target = out_path or os.path.join(directory, out_name)
        # -o 给了目录（已存在，或以分隔符结尾）→ 在里面放 out_name
        if os.path.isdir(target) or target.endswith(("/", "\\")):
            target = os.path.join(target, out_name)
        parent = os.path.dirname(os.path.abspath(target))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(html)
        path = target
    return path, entries
