"""通过 HTTP 完整验证本地 UI：起服务、提交任务、轮询、检查产物。

用法：先启动服务，再运行本脚本。
    python webvault-ui.py --port 8770 --no-browser --out <目录>
    python tests/test_ui.py 8770
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
PORT = sys.argv[1] if len(sys.argv) > 1 else "8770"
BASE = "http://127.0.0.1:%s" % PORT
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


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def post(path, obj):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 400/403 也是合法响应，交给调用方断言
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw}


print("=== 静态页面 ===")
st, ct, body = get("/")
ck("GET / 返回 200", st == 200, str(st))
ck("HTML 内容类型带 utf-8", "utf-8" in (ct or ""), str(ct))
page = body.decode("utf-8")
for needle in ("WebVault", "api/vault", "opt_nocss", "Ctrl"):
    ck("页面含 %r" % needle, needle in page)

print()
print("=== info / jobs ===")
st, ct, body = get("/api/info")
info = json.loads(body.decode("utf-8"))
ck("/api/info 是 JSON+utf-8", "utf-8" in (ct or ""), str(ct))
ck("info 有 out_dir", bool(info.get("out_dir")), str(info))

# 守卫：服务端的输出目录是全局的（不能按请求改），所以如果它指向仓库里的
# examples/，下面"提交任务"的测试就会把产物写进交付给用户看的样例集。
# 曾经就这样把 Example-Domain-2..5.html 写进去过，自包含审计的分母从 6 变 10。
_SERVER_OUT = os.path.abspath(info.get("out_dir") or "")
_EXAMPLES = os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"))
_WRITES_INTO_EXAMPLES = bool(_SERVER_OUT) and (
    _SERVER_OUT == _EXAMPLES or _SERVER_OUT.startswith(_EXAMPLES + os.sep))
if _WRITES_INTO_EXAMPLES:
    print()
    print("  !! 服务端输出目录指向了 examples/：%s" % _SERVER_OUT)
    print("     跳过会写文件的用例，避免污染交付样例集。")
    print("     正确用法：把服务起在临时目录上，例如")
    print("       python webvault-ui.py --port 8774 --no-browser --out %%TEMP%%\\wv-uitest")
    ck("服务端输出目录不应指向 examples/", False, _SERVER_OUT)

st, ct, body = get("/api/jobs")
ck("/api/jobs 是 JSON", st == 200 and isinstance(json.loads(body), list))

print()
print("=== 提交任务（含 keep_site_css 转发）===")
st, res = post("/api/vault", {
    "urls": ["https://example.com"],
    "mode": "reader",
    "max_total": 24, "max_asset": 4,
    "images": True, "css": True, "compress": True,
    "local_only": False, "keep_site_css": True, "timeout": 20,
})
ck("POST /api/vault 返回 ids", st == 200 and res.get("ids"), str(res))
jid = (res.get("ids") or [""])[0]

final = None
for _ in range(40):
    time.sleep(0.5)
    st, ct, body = get("/api/jobs")
    for j in json.loads(body.decode("utf-8")):
        if j["id"] == jid and j["status"] != "running":
            final = j
            break
    if final:
        break

ck("任务在合理时间内结束", final is not None)
if final:
    ck("任务成功", final["status"] == "ok", str(final.get("error")))
    ck("有标题", bool(final["title"]), str(final))
    ck("有产物路径", bool(final["path"]), str(final))
    ck("体积 > 0", final["size"] > 0, str(final["size"]))
    ck("有日志", bool(final["log"]), str(final["log"])[:120])
    print("     标题:", final["title"])
    print("     路径:", final["path"])
    print("     体积:", final["size"], "字节, 耗时 %.2fs" % final["elapsed"])

    print()
    print("=== 预览接口与路径限制 ===")
    import urllib.parse
    q = urllib.parse.quote(final["path"])
    st, ct, body = get("/api/file?p=" + q)
    ck("能取回自己的产物", st == 200 and len(body) > 0, str(st))
    ck("产物是 HTML", "html" in (ct or ""), str(ct))

    # 越权读取必须被拒绝
    try:
        st, ct, body = get("/api/file?p=" + urllib.parse.quote("C:\\Windows\\win.ini"))
        denied = (st == 403) or (b"error" in body[:200])
    except Exception:
        denied = True
    ck("拒绝读取输出目录之外的文件", denied)

print()
print("=== 参数校验 ===")
st, res = post("/api/vault", {"urls": [], "mode": "reader"})
ck("空 urls 被拒（400）", st == 400 and "error" in res, "%s %s" % (st, res))

st, res = post("/api/vault", {"urls": ["https://example.com"], "mode": "不存在的模式"})
ck("非法 mode 被纠正而非报错", st == 200 and "ids" in res, "%s %s" % (st, res))

st, res = post("/api/vault", {"urls": ["x"], "mode": "reader", "max_total": "abc"})
ck("非法 max_total 不炸", st in (200, 400), "%s %s" % (st, str(res)[:80]))

print()
print("=== 书库接口 /api/library ===")
st, ct, body = get("/api/library")
lib = json.loads(body.decode("utf-8"))
ck("/api/library 返回 200", st == 200, str(st))
ck("有 dir", bool(lib.get("dir")), str(lib)[:120])
ck("有 items 列表", isinstance(lib.get("items"), list), str(lib)[:120])
ck("count >= items 长度",
   lib.get("count", 0) >= len(lib.get("items", [])), str(lib.get("count")))
ck("total 是数字", isinstance(lib.get("total"), int), str(lib.get("total")))
if lib.get("items"):
    it = lib["items"][0]
    for k in ("path", "title", "size_h", "mode", "host", "captured"):
        ck("条目含字段 %s" % k, k in it, str(sorted(it.keys())))

print()
print("=== 生成索引 /api/index ===")
st, res = post("/api/index", {})
ck("/api/index 成功", st == 200 and res.get("ok"), "%s %s" % (st, str(res)[:120]))
ck("返回索引路径", bool(res.get("path")), str(res)[:120])
if res.get("path"):
    ck("索引文件真的存在", os.path.isfile(res["path"]), res["path"])
    ck("索引有内容", os.path.getsize(res["path"]) > 500, str(res))

print()
print("=== 发现链接 /api/discover ===")
st, res = post("/api/discover", {"url": "", "limit": 10})
ck("空 url 被拒（400）", st == 400 and "error" in res, "%s %s" % (st, res))

st, res = post("/api/discover", {"url": "http://127.0.0.1:1/nope", "limit": 10})
ck("连不上的地址报错而不是崩", st in (500, 502) and "error" in res,
   "%s %s" % (st, str(res)[:120]))

# 起一个本地小站，用真链接验证发现逻辑
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = ("<html><head><title>list</title></head><body><nav>"
        "<a href='/'>首页</a><a href='/login'>登录</a></nav><main>"
        "<a href='/blog/2024/01/alpha-post.html'>这是第一篇比较长的文章标题</a>"
        "<a href='/blog/2024/02/beta-post.html'>这是第二篇比较长的文章标题</a>"
        "<a href='/tags/x'>标签</a></main></body></html>").encode("utf-8")


class LH(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


srv = ThreadingHTTPServer(("127.0.0.1", 0), LH)
threading.Thread(target=srv.serve_forever, daemon=True).start()
try:
    st, res = post("/api/discover", {
        "url": "http://127.0.0.1:%d/list" % srv.server_address[1], "limit": 20})
    ck("发现请求成功", st == 200 and res.get("ok"), "%s %s" % (st, str(res)[:160]))
    links = res.get("links") or []
    urs = [l["url"] for l in links]
    ck("发现了文章", len(links) >= 2, str(urs))
    ck("第一条是文章", any("alpha-post" in u for u in urs), str(urs))
    ck("排除了导航/标签", not any("/login" in u or "/tags/" in u for u in urs), str(urs))
    ck("链接带分数字段", all("score" in l for l in links), str(links[:1]))
finally:
    srv.shutdown()
    srv.server_close()

print()
print("=== 前端页面含新功能 ===")
st, ct, body = get("/")
page = body.decode("utf-8")
for needle in ("pane-find", "pane-lib", "api/discover", "api/library",
               "api/index", "发现链接", "我的书库"):
    ck("页面含 %r" % needle, needle in page)

print()
print("=== CLI 帮助（子命令不能藏起来）===")
import subprocess
_py = sys.executable
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cli(*a):
    return subprocess.run([_py, "webvault.py"] + list(a), cwd=_root,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


_r0 = _cli()
_out0 = (_r0.stdout or "") + (_r0.stderr or "")
ck("无参数打印帮助", "usage: webvault" in _out0, _out0[:120])
ck("无参数退出码是 2（用法问题，不是崩溃）", _r0.returncode == 2, str(_r0.returncode))
for _sub in ("discover", "index", "diff"):
    ck("顶层帮助提到子命令 %s" % _sub, ("webvault %s" % _sub) in _out0, "未提到")
    _rs = _cli(_sub, "-h")
    ck("%s -h 退出码是 0" % _sub, _rs.returncode == 0, str(_rs.returncode))
    ck("%s -h 有用法说明" % _sub, "usage:" in (_rs.stdout or ""), "")
_rv = _cli("-V")
ck("-V 输出版本号", "1.0" in ((_rv.stdout or "") + (_rv.stderr or "")),
   (_rv.stdout or "")[:80])

print()
print("=" * 50)
print("通过 %d / 失败 %d" % (ok, bad))
sys.exit(0 if bad == 0 else 1)
