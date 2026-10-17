"""验证书库索引页：JS 语法、data 属性、搜索匹配逻辑。"""
import json
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = r"C:\Users\Administrator\Desktop\gift"
idx = os.path.join(ROOT, "examples", "index.html")
html = open(idx, encoding="utf-8").read()

ok = 0
bad = 0


def ck(label, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print("  OK   %s" % label)
    else:
        bad += 1
        print("  FAIL %s  %s" % (label, detail))


print("=== 结构 ===")
ck("有 doctype", html.lstrip().lower().startswith("<!doctype html"))
ck("有 charset", 'charset="utf-8"' in html)
ck("有 viewport", "viewport" in html)
ck("有搜索框", 'id="q"' in html)
ck("有排序选择", 'id="sort"' in html)
ck("有类型筛选", 'id="mode"' in html)
ck("有计数元素", 'id="shown"' in html)
ck("零外部资源",
   len(re.findall(r'<(?!a\b)(\w+)[^>]*?(?:src|href)="https?://', html)) == 0)

print()
print("=== 卡片与 data 属性 ===")
cards = re.findall(r'<div class="card"([^>]*)>', html)
ck("有卡片", len(cards) > 0, str(len(cards)))

required = ["data-search", "data-mode", "data-title", "data-size", "data-host", "data-mtime"]
all_have = True
for i, attrs in enumerate(cards):
    for req in required:
        if req + "=" not in attrs:
            print("     卡片 %d 缺少 %s" % (i, req))
            all_have = False
ck("每张卡片都有全部 data 属性", all_have)

# 搜索属性必须是小写，否则 JS 的 lowercase 匹配会失效
search_vals = re.findall(r'data-search="([^"]*)"', html)
ck("data-search 已转小写", all(v == v.lower() for v in search_vals),
   str([v[:40] for v in search_vals if v != v.lower()][:2]))

# data-search 应包含标题内容
titles = re.findall(r'data-title="([^"]*)"', html)
if titles:
    ck("data-search 含标题信息",
       any(t.lower()[:6] in search_vals[i] for i, t in enumerate(titles) if i < len(search_vals)),
       str(titles[:1]))

# 相对链接必须真实存在
hrefs = re.findall(r'<a class="t" href="([^"]*)"', html)
missing = []
for h in hrefs:
    p = os.path.join(ROOT, "examples", h.replace("/", os.sep))
    if not os.path.exists(p):
        missing.append(h)
ck("所有卡片链接指向真实文件", not missing, str(missing[:3]))

print()
print("=== JS 语法与逻辑 ===")
m = re.search(r"<script>(.*?)</script>", html, re.S)
ck("有 script 块", m is not None)
if m:
    js = m.group(1)
    # 用 node --check 验证语法
    tmp = os.path.join(ROOT, "examples", "_check.js")
    open(tmp, "w", encoding="utf-8").write(js)
    node = r"C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
    if os.path.exists(node):
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        ck("JS 语法正确（node --check）", r.returncode == 0, r.stderr[:200])

        # 真正跑一遍过滤逻辑：造一个最小 DOM 存根
        harness = """
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const cards = [];
function mkCard(title, host, mode, size, mtime){
  const attrs = {
    'data-search': (title+' '+host).toLowerCase(),
    'data-title': title, 'data-host': host, 'data-mode': mode,
    'data-size': String(size), 'data-mtime': String(mtime),
  };
  const el = {
    _attrs: attrs, style:{},
    getAttribute(k){ return attrs[k] === undefined ? null : attrs[k]; },
  };
  cards.push(el); return el;
}
mkCard('Alpha 文章', 'a.com', 'reader', 100, 3);
mkCard('Beta 文章',  'b.com', 'full',   300, 1);
mkCard('Gamma',      'a.com', 'data',   200, 2);

const grid = { querySelectorAll: () => cards, appendChild: () => {} };
const q = { value: '', addEventListener: (e,f)=>{ q['on'+e]=f; } };
const sort = { value:'date', addEventListener: (e,f)=>{ sort['on'+e]=f; } };
const mode = { value:'', addEventListener: (e,f)=>{ mode['on'+e]=f; } };
const shown = { textContent: '0' };
const document = {
  getElementById: (id) => ({q, sort, mode, grid, shown}[id]),
  querySelectorAll: () => cards,
};

// 打桩后执行索引页里的脚本
eval(src);

function visible(){ return cards.filter(c => c.style.display !== 'none').length; }

// 首次 apply() 应显示全部
console.log('INITIAL=' + visible());

// 搜索 alpha
q.value = 'alpha'; q.oninput();
console.log('SEARCH_ALPHA=' + visible());

// 搜索不存在的词
q.value = 'zzzz'; q.oninput();
console.log('SEARCH_NONE=' + visible());

// 清空 + 按模式筛选
q.value = ''; q.oninput();
mode.value = 'full'; mode.onchange();
console.log('MODE_FULL=' + visible());

mode.value = ''; mode.onchange();
console.log('RESET=' + visible());
"""
        hpath = os.path.join(ROOT, "examples", "_harness.js")
        open(hpath, "w", encoding="utf-8").write(harness)
        r2 = subprocess.run([node, hpath, tmp], capture_output=True, text=True,
                            encoding="utf-8")
        if r2.returncode != 0:
            ck("JS 逻辑可执行", False, (r2.stderr or "")[:300])
        else:
            out = dict(
                line.split("=", 1)
                for line in r2.stdout.strip().splitlines() if "=" in line
            )
            ck("初始显示全部 3 张", out.get("INITIAL") == "3", str(out))
            ck("搜索 alpha → 1 张", out.get("SEARCH_ALPHA") == "1", str(out.get("SEARCH_ALPHA")))
            ck("搜索无结果 → 0 张", out.get("SEARCH_NONE") == "0", str(out.get("SEARCH_NONE")))
            ck("按 full 筛选 → 1 张", out.get("MODE_FULL") == "1", str(out.get("MODE_FULL")))
            ck("清空筛选 → 3 张", out.get("RESET") == "3", str(out.get("RESET")))
        for f in (hpath,):
            try:
                os.remove(f)
            except Exception:
                pass
    else:
        print("  (node 不可用，跳过 JS 逻辑测试)")

    try:
        os.remove(tmp)
    except Exception:
        pass

print()
print("=" * 50)
print("通过 %d / 失败 %d" % (ok, bad))
sys.exit(0 if bad == 0 else 1)
