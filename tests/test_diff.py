"""端到端验证变更检测：起一个内容可变的本地站点，改动后看 diff 是否正确。"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = r"C:\Users\Administrator\Desktop\gift"
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

PY = r"C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

PARAS = ["这是第一段正文内容，讲述某件事情的来龙去脉。",
         "这是第二段正文内容，补充说明前面提到的细节。"]


def page(paras):
    body = "".join("<p>%s</p>" % p for p in paras)
    return ("<html><head><title>公告页面</title></head><body><article>"
            "<h1>公告</h1>%s</article></body></html>" % body).encode("utf-8")


STATE = {"paras": list(PARAS)}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = page(STATE["paras"])
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d/notice" % port
# 测试产物一律写到临时目录。绝不能写进 examples/ —— 那是交付给用户看的样例集，
# 被测试污染之后自包含审计的分母都会变（曾经从 6 个变成 10 个）。
SCRATCH = tempfile.mkdtemp(prefix="wv-difftest-")
out = os.path.join(SCRATCH, "_diff_test")
shutil.rmtree(out, ignore_errors=True)
os.makedirs(out, exist_ok=True)

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


def run(*a):
    r = subprocess.run([PY, "webvault.py"] + list(a), cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.stdout + r.stderr


try:
    print("=== 1) 首次保存（建立基线）===")
    o1 = run(url, "-o", out, "-q")
    ck("首次保存成功", "完成" in o1, o1[-300:])
    ck("产生了历史目录", os.path.isdir(os.path.join(out, ".webvault")),
       str(os.listdir(out)))

    print()
    print("=== 2) 内容没变时 diff ===")
    o2 = run("diff", url, "-o", out)
    ck("报告没有变化", "没有变化" in o2, o2[-400:])

    print()
    print("=== 3) 内容改变后 diff ===")
    STATE["paras"] = PARAS + ["这是新增的第三段，之前没有的内容。"]
    o3 = run("diff", url, "-o", out)
    ck("报告有变化", "内容有变化" in o3, o3[-600:])
    ck("显示了新增行", "+ " in o3 and "新增的第三段" in o3, o3[-600:])
    ck("统计了新增行数", "新增 1 行" in o3 or "新增 2 行" in o3, o3[-300:])

    print()
    print("=== 4) 内容被删除后 diff ===")
    STATE["paras"] = [PARAS[0]]
    o4 = run("diff", url, "-o", out)
    ck("报告有变化", "内容有变化" in o4, o4[-500:])
    ck("显示了删除行", "- " in o4, o4[-500:])

    print()
    print("=== 5) --list 列出追踪的页面 ===")
    o5 = run("diff", "--list", "-o", out)
    ck("列出页面", "已追踪" in o5, o5[-300:])
    ck("包含标题", "公告页面" in o5, o5[-300:])
    ck("包含 url", "/notice" in o5, o5[-400:])

    print()
    print("=== 6) 历史版本数在增长 ===")
    from src import watch
    hist = watch.history_for(out, url)
    vers = hist.get("versions") or []
    ck("记录了多个版本", len(vers) >= 2, str(len(vers)))
    ck("每个版本有 hash 和时间",
       all(v.get("hash") and v.get("captured") for v in vers),
       str(vers[-1] if vers else None))

    print()
    print("=== 7) 空目录 / 未知 URL 不崩 ===")
    fresh = os.path.join(SCRATCH, "_diff_fresh")
    shutil.rmtree(fresh, ignore_errors=True)
    os.makedirs(fresh, exist_ok=True)
    o7 = run("diff", url, "-o", fresh)
    ck("首次 diff 记录基线而不崩溃", "第一次" in o7 or "基线" in o7, o7[-300:])
    o8 = run("diff", "--list", "-o", os.path.join(fresh, "nope"))
    ck("--list 空目录友好提示", "还没有追踪" in o8 or "没有" in o8, o8[-200:])

    print()
    print("=== 8) 追踪目录不污染书库扫描 ===")
    from src.library import scan
    entries = scan(out)
    ck("书库不收录 .webvault 里的 json",
       not any(".webvault" in e.rel_path for e in entries),
       str([e.rel_path for e in entries]))

finally:
    srv.shutdown()
    srv.server_close()
    shutil.rmtree(SCRATCH, ignore_errors=True)

print()
print("=" * 50)
print("通过 %d / 失败 %d" % (ok, bad))
sys.exit(0 if bad == 0 else 1)
