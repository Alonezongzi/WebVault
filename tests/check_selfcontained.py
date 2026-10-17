"""严格检查产物是否真的自包含。

用真解析器（不是正则跨标签匹配）列出所有"会发起网络请求"的属性，
确认它们要么是 data:，要么是页面内锚点，要么是相对链接。

之前用宽松正则统计过一次，结果 RFC 正文里的 `<URL:http://...>` 这种
纯文本把正则带偏了，误报成"有外链"。这里改成按标签+属性精确判断。
"""
import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from src.parse import parse

# 真正会触发网络请求的属性
NET_ATTRS = {
    "img": ["src", "srcset", "data-src", "data-original", "longdesc"],
    "script": ["src"],
    "link": ["href"],
    "iframe": ["src"],
    "frame": ["src"],
    "embed": ["src"],
    "source": ["src", "srcset"],
    "video": ["src", "poster"],
    "audio": ["src"],
    "track": ["src"],
    "object": ["data"],
    "input": ["src"],
    "use": ["href", "xlink:href"],
    "image": ["href", "xlink:href"],
}

IGNORE_SCHEMES = ("data:", "#", "javascript:", "mailto:", "about:", "blob:")


def external_refs(path):
    """返回 (标签, 属性, 值) 的列表 —— 只含会发起网络请求的外部地址。"""
    html = open(path, encoding="utf-8", errors="replace").read()
    root = parse(html)
    out = []
    for tag, attrs in NET_ATTRS.items():
        for node in root.find_all(tag):
            for a in attrs:
                v = (node.get(a) or "").strip()
                if not v:
                    continue
                if v.startswith(IGNORE_SCHEMES):
                    continue
                if "://" in v.split("/")[0] or v.startswith("//"):
                    # 只报 http(s) 和协议相对地址
                    if v.startswith("http://") or v.startswith("https://") or v.startswith("//"):
                        out.append((tag, a, v))
    return out


targets = []
for d in ("examples",):
    targets.extend(sorted(glob.glob(os.path.join(d, "*.html"))))
    targets.extend(sorted(glob.glob(os.path.join(d, "*.json"))))

print("%-58s %8s %s" % ("文件", "大小KB", "外部请求"))
print("-" * 100)
bad = 0
for p in targets:
    if os.path.basename(p) == "index.html":
        # 索引页自己也要自包含，但它的 <a> 指向本地文件，不在 NET_ATTRS 里
        pass
    size = os.path.getsize(p) / 1024
    if p.endswith(".json"):
        print("%-58s %8.1f %s" % (os.path.basename(p)[:56], size, "(JSON)"))
        continue
    refs = external_refs(p)
    status = "0" if not refs else "%d !!" % len(refs)
    print("%-58s %8.1f %s" % (os.path.basename(p)[:56], size, status))
    for tag, a, v in refs[:5]:
        print("      <%s %s=%s>" % (tag, a, v[:110]))
    if refs:
        bad += 1

print("-" * 100)
print("含真实外部请求的文件数: %d / %d" % (bad, len(targets)))
sys.exit(0 if bad == 0 else 1)
