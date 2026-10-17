"""抓取层 —— 把 URL 变成 (bytes, 最终URL, 头信息)。

设计取向：
  - 只用 urllib，零依赖
  - 编码检测不靠第三方库：BOM → HTTP 头 → meta charset → 启发式 chardet 替代
  - 403/超时等失败不抛异常给上层，返回结构化结果，让调用方决定怎么办
  - 支持 gzip/deflate 自动解压
  - 重定向自己跟，便于记录跳转链
"""

from __future__ import annotations

import gzip
import io
import re
import socket
import ssl
import time
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .util import human_size

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
    "Upgrade-Insecure-Requests": "1",
}

# 常见编码别名
ENCODING_ALIASES = {
    "gb2312": "gbk",
    "gb-2312": "gbk",
    "gb_2312": "gbk",
    "chinese": "gbk",
    "csgb2312": "gbk",
    "x-sjis": "shift_jis",
    "sjis": "shift_jis",
    "utf8": "utf-8",
    "utf-8-sig": "utf-8-sig",
    "latin1": "latin-1",
    "iso8859-1": "latin-1",
    "ascii": "ascii",
    "unicode": "utf-16",
    "big5-hkscs": "big5",
}


@dataclass
class FetchResult:
    """一次抓取的结果。ok=False 时 error 有值，body 可能为空。"""

    url: str
    final_url: str = ""
    status: int = 0
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)
    encoding: str = "utf-8"
    content_type: str = ""
    error: str = ""
    elapsed: float = 0.0
    redirects: List[str] = field(default_factory=list)
    ok: bool = False

    @property
    def text(self) -> str:
        return decode_bytes(self.body, self.encoding)

    @property
    def size(self) -> int:
        return len(self.body)

    def size_h(self) -> str:
        return human_size(self.size)


class _NoRedirect(HTTPRedirectHandler):
    """不用 urllib 自带的重定向，改由我们自己跟，方便记录链条。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _ssl_context(insecure: bool = True):
    """默认宽松：抓公开网页时证书问题很常见，不值得为此失败。"""
    try:
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return ssl.create_default_context()
    except Exception:
        return None


def _decompress(body: bytes, headers: Dict[str, str]) -> bytes:
    enc = (headers.get("content-encoding") or "").lower()
    if not body:
        return body
    try:
        if "gzip" in enc:
            return gzip.decompress(body)
        if "deflate" in enc:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception:
        # 解压失败：原样返回，让上层去猜
        return body
    return body


def _looks_like_html(body: bytes) -> bool:
    head = body[:1024].lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html") or b"<head" in head


def sniff_encoding(body: bytes, headers: Optional[Dict[str, str]] = None,
                   declared: str = "") -> str:
    """猜编码。优先级：显式声明 → BOM → HTTP 头 → meta → 试解。

    这里不依赖 chardet，用一套实用的启发式，对中文网页尤其重要
    （大量老站点是 GBK 但没声明）。
    """
    headers = headers or {}

    def norm(name: str) -> str:
        name = (name or "").strip().strip('"').strip("'").lower()
        return ENCODING_ALIASES.get(name, name)

    if declared:
        d = norm(declared)
        if d:
            return d

    # BOM
    if body[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"
    if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"

    # HTTP 头
    ct = headers.get("content-type", "")
    m = re.search(r"charset\s*=\s*([\w\-]+)", ct, re.I)
    if m:
        c = norm(m.group(1))
        if c:
            return c

    # meta charset（只看前 4KB）
    head = body[:4096]
    m = re.search(rb'<meta[^>]+charset\s*=\s*["\']?\s*([\w\-]+)', head, re.I)
    if m:
        try:
            c = norm(m.group(1).decode("ascii", "ignore"))
            if c:
                return c
        except Exception:
            pass
    m = re.search(
        rb'<meta[^>]+content\s*=\s*["\'][^"\']*charset\s*=\s*([\w\-]+)', head, re.I
    )
    if m:
        try:
            c = norm(m.group(1).decode("ascii", "ignore"))
            if c:
                return c
        except Exception:
            pass

    # 试解：先 UTF-8 严格，成功就是它
    try:
        body.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    # UTF-8 失败 —— 中/日/韩网页大概率是 GBK 或 Big5
    for cand in ("gbk", "big5", "shift_jis", "euc-kr"):
        try:
            body.decode(cand)
            return cand
        except Exception:
            continue

    return "utf-8"


def decode_bytes(body: bytes, encoding: str = "utf-8", errors: str = "replace") -> str:
    """按指定编码解码，失败时逐级降级，绝不抛异常。"""
    if not body:
        return ""
    enc = ENCODING_ALIASES.get((encoding or "").lower(), encoding or "utf-8")
    for cand in (enc, "utf-8", "gbk", "latin-1"):
        if not cand:
            continue
        try:
            return body.decode(cand)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors)


# 这些状态码表示"暂时性失败"，重试有意义。
# 429 是限流，5xx 是服务端抖动，408 是请求超时。
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509, 520, 521, 522, 524})


def _friendly_error(exc: BaseException) -> str:
    """把底层网络异常翻译成用户看得懂的一句话。

    `URLError: <urlopen error timed out>` 对用户没有任何帮助 ——
    他需要知道是网址写错了、被墙了，还是对方服务器挂了。

    注意：不能只靠字符串匹配。Windows 的 socket 错误消息是本地化的
    （"由于目标计算机积极拒绝，无法连接。"），英文关键词一个都不出现，
    所以先按异常类型和 errno 判断，最后才退回文本匹配。
    """
    # Windows 的 WSA* 错误码和 POSIX 的 errno 不是一套，两边都要认
    ERRNO = {
        111: "连接被拒绝：对方服务器的这个端口没有响应",
        10061: "连接被拒绝：对方服务器的这个端口没有响应",
        104: "连接被重置：对方中断了这次请求",
        10054: "连接被重置：对方中断了这次请求",
        110: "连接超时：服务器在限定时间内没有响应（可能不在线、被防火墙拦截，或网址不可达）",
        10060: "连接超时：服务器在限定时间内没有响应（可能不在线、被防火墙拦截，或网址不可达）",
        113: "网络不可达：本机到对方主机没有路由",
        10065: "网络可达但主机没有响应：对方主机不在线",
        101: "网络不可达：本机到对方主机没有路由",
        10051: "网络不可达：本机到对方主机没有路由",
        11001: "域名解析失败：找不到这个主机名，检查网址是否拼错",
        8: "域名解析失败：找不到这个主机名，检查网址是否拼错",
    }
    TIMEOUT_T = "连接超时：服务器在限定时间内没有响应（可能不在线、被防火墙拦截，或网址不可达）"
    REFUSED_T = "连接被拒绝：对方服务器的这个端口没有响应"
    RESET_T = "连接被重置：对方中断了这次请求"
    DNS_T = "域名解析失败：找不到这个主机名，检查网址是否拼错"

    # 1) 按异常类型判定（最可靠）
    def classify(e: BaseException) -> str:
        if isinstance(e, TimeoutError):
            return TIMEOUT_T
        if isinstance(e, ConnectionRefusedError):
            return REFUSED_T
        if isinstance(e, ConnectionResetError):
            return RESET_T
        if isinstance(e, socket.gaierror):
            return DNS_T
        if isinstance(e, socket.timeout):
            return TIMEOUT_T
        no = getattr(e, "errno", None)
        if no in ERRNO:
            return ERRNO[no]
        if isinstance(e, OSError) and getattr(e, "winerror", None) in ERRNO:
            return ERRNO[e.winerror]
        return ""

    # 2) 逐层往里看：URLError.reason、__cause__、__context__
    seen = set()
    queue = [exc]
    while queue:
        cur = queue.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        hit = classify(cur)
        if hit:
            return hit
        for attr in ("reason", "__cause__", "__context__"):
            nxt = getattr(cur, attr, None)
            if isinstance(nxt, BaseException):
                queue.append(nxt)
            elif isinstance(nxt, str):
                # URLError("timed out") 这类字符串 reason
                low = nxt.lower()
                if "timed out" in low or "timeout" in low:
                    return TIMEOUT_T
                if "refused" in low:
                    return REFUSED_T
                if "reset" in low:
                    return RESET_T

    # 3) 最后退回文本匹配（有些平台的错误消息是英文的）
    raw = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    low = raw.lower()
    if "timed out" in low or "timeout" in low:
        return TIMEOUT_T
    if "getaddrinfo" in low or "name or service not known" in low or "nodename" in low:
        return DNS_T
    if "connection refused" in low:
        return REFUSED_T
    if "connection reset" in low or "reset by peer" in low:
        return RESET_T
    if "certificate" in low or "ssl" in low:
        return "TLS 证书校验失败：对方证书有问题（默认已跳过校验，出现这条说明情况更严重）"
    if "too many redirects" in low:
        return "重定向次数过多"
    if "no route to host" in low or "network is unreachable" in low:
        return "网络不可达：本机到对方主机没有路由"
    # 中文系统消息也认一下
    if "积极拒绝" in raw or "拒绝" in raw:
        return REFUSED_T
    if "无法连接" in raw:
        return REFUSED_T
    return raw


def _sleep_retry(attempt: int, retry_after: Optional[str],
                 budget: float = 99.0) -> None:
    """重试前的等待。尊重 Retry-After，但不超过 5 秒，也不超过剩余预算。"""
    delay = 0.4 * (attempt + 1)
    if retry_after:
        try:
            delay = max(delay, min(5.0, float(str(retry_after).strip())))
        except (TypeError, ValueError):
            pass
    # 退避里加一点抖动，避免多个线程同时重试又同时被限流
    try:
        import random
        delay += random.uniform(0, 0.25)
    except Exception:
        pass
    # 别把最后的时间浪费在等待上：至少给下一次尝试留点余地
    delay = min(delay, max(0.0, budget - 0.3))
    if delay > 0:
        time.sleep(delay)


# --------------------------------------------------------------------------
# 反爬 / 挑战页识别
# --------------------------------------------------------------------------
# 抓到的"页面"有时候根本不是内容，而是一张拦截页：
# Cloudflare 的 "Just a moment..."、登录墙、验证码页。
# 这类页面往往返回 200，正文提取会给出一个又短又没意义的产物。
# 与其让用户拿到一个空壳，不如直接告诉他"你被拦了，原因是什么"。
_CHALLENGE_SIGNATURES = [
    # (判定用的特征, 人类可读的原因, 建议)
    (("just a moment", "challenges.cloudflare.com", "cf-chl", "__cf_chl"),
     "Cloudflare 人机验证", "站点启用了 Cloudflare 挑战，需要真实浏览器通过验证后才能访问"),
    (("checking your browser", "enable javascript and cookies"),
     "浏览器检查页", "站点要求执行 JavaScript 才放行"),
    (("attention required", "cf-error-details"),
     "Cloudflare 拦截", "请求被 Cloudflare 防火墙拦截"),
    (("captcha", "recaptcha", "hcaptcha", "g-recaptcha"),
     "验证码页", "站点要求完成验证码"),
    (("请开启 javascript", "请启用 javascript", "您的浏览器未启用"),
     "需要 JavaScript", "站点要求执行 JavaScript 才放行"),
    (("sign in to continue", "log in to continue", "请先登录", "登录后查看",
      "please log in", "you must be logged in"),
     "登录墙", "需要登录才能查看内容"),
    (("access denied", "403 forbidden", "you have been blocked"),
     "访问被拒绝", "站点拒绝了这次请求（可能需要 Cookie 或代理）"),
    (("ddos protection", "rate limit exceeded", "too many requests"),
     "限流/防护页", "请求触发了站点的频率限制"),
]


def detect_challenge(text: str, status: int = 0) -> Tuple[str, str]:
    """判断这是不是一张拦截页。

    返回 (原因, 建议)；不是拦截页则返回 ("", "")。

    只在页面**很短**的时候才判定 —— 正文页里偶尔出现 "captcha" 这个词
    很正常，不能因此误判。
    """
    if not text:
        return "", ""
    head = text[:6000].lower()
    # 长页面基本是真正文，不做拦截页判定
    if len(text) > 20000 and status in (200, 0):
        return "", ""
    for sigs, reason, advice in _CHALLENGE_SIGNATURES:
        for sig in sigs:
            if sig in head:
                return reason, advice
    return "", ""


def fetch(
    url: str,
    *,
    timeout: float = 20.0,
    headers: Optional[Dict[str, str]] = None,
    max_redirects: int = 8,
    retries: int = 2,
    insecure: bool = True,
    max_bytes: int = 32 * 1024 * 1024,
    referer: str = "",
) -> FetchResult:
    """抓取一个 URL。永不抛异常 —— 失败信息在结果的 error 里。

    `timeout` 是**这次抓取总共愿意等多久**（含所有重试），不是一个连接的
    超时。以前每个 attempt 各自计时，一个连不上的地址会卡 3×timeout，
    用户以为程序死了。现在每次重试只拿剩余时间，到点就放弃。
    """
    if not url:
        return FetchResult(url=url, error="空 URL")

    started = time.time()
    deadline = started + max(1.0, timeout)
    hdrs = dict(DEFAULT_HEADERS)
    if referer:
        hdrs["Referer"] = referer
    if headers:
        hdrs.update(headers)

    ctx = _ssl_context(insecure)
    opener = build_opener(_NoRedirect(), HTTPSHandler(context=ctx)) if ctx else build_opener(_NoRedirect())

    current = url
    redirects: List[str] = []
    last_error = ""

    def _remaining() -> float:
        return deadline - time.time()

    for attempt in range(max(1, retries + 1)):
        left = _remaining()
        if left <= 0.5:
            if not last_error:
                last_error = "超时（%.0fs 内未完成）" % (time.time() - started)
            break
        try:
            for _hop in range(max_redirects + 1):
                req = Request(current, headers=hdrs)
                try:
                    resp = opener.open(req, timeout=max(0.5, _remaining()))
                except HTTPError as e:
                    if e.code in (301, 302, 303, 307, 308):
                        loc = e.headers.get("Location", "")
                        if not loc:
                            return FetchResult(url=url, final_url=current, status=e.code,
                                               error="重定向缺少 Location",
                                               elapsed=time.time() - started)
                        from .util import safe_urljoin
                        current = safe_urljoin(current, loc)
                        redirects.append(current)
                        if len(redirects) > max_redirects:
                            return FetchResult(url=url, final_url=current, error="重定向次数过多",
                                               redirects=redirects, elapsed=time.time() - started)
                        continue
                    # 5xx / 429 / 408 是"暂时性"失败，值得重试。
                    # 不加这一段的话，并发抓资源时被限流的图片会被永久丢弃，
                    # 产物里留下一堆指向原站的外链（离线打开就是破图）。
                    if e.code in RETRYABLE_STATUS and attempt < retries:
                        last_error = "HTTP %s %s" % (e.code, e.reason or "")
                        try:
                            ra = e.headers.get("Retry-After") if e.headers else None
                        except Exception:
                            ra = None
                        _sleep_retry(attempt, ra)
                        break  # 跳出重定向循环，走下一轮 attempt
                    # 其余 4xx/5xx：仍然把 body 读出来（有些站点的 403 页面有用）
                    try:
                        raw = e.read(max_bytes)
                    except Exception:
                        raw = b""
                    h = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
                    raw = _decompress(raw, h)
                    return FetchResult(
                        url=url, final_url=current, status=e.code, body=raw,
                        headers=h, content_type=h.get("content-type", ""),
                        encoding=sniff_encoding(raw, h),
                        error="HTTP %s %s" % (e.code, e.reason or ""),
                        elapsed=time.time() - started, redirects=redirects,
                        ok=False,
                    )
                except (URLError, socket.timeout, ssl.SSLError, OSError) as e:
                    last_error = _friendly_error(e)
                    break  # 跳出重定向循环，去重试
                else:
                    raw = resp.read(max_bytes)
                    h = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
                    raw = _decompress(raw, h)
                    status = getattr(resp, "status", 200) or 200
                    return FetchResult(
                        url=url, final_url=current, status=status, body=raw, headers=h,
                        content_type=h.get("content-type", ""),
                        encoding=sniff_encoding(raw, h),
                        elapsed=time.time() - started, redirects=redirects, ok=True,
                    )
            if last_error:
                # 重试前稍等，但别等到超过总预算
                if attempt < retries and _remaining() > 1.0:
                    _sleep_retry(attempt, None, budget=_remaining())
                    continue
                return FetchResult(url=url, final_url=current, error=last_error,
                                   elapsed=time.time() - started, redirects=redirects)
        except Exception as e:
            last_error = _friendly_error(e)
            if attempt < retries and _remaining() > 1.0:
                _sleep_retry(attempt, None, budget=_remaining())
                continue
            break

    return FetchResult(url=url, final_url=current, error=last_error or "未知错误",
                       elapsed=time.time() - started, redirects=redirects)


def fetch_bytes(url: str, *, timeout: float = 20.0, headers: Optional[Dict[str, str]] = None,
                max_bytes: int = 16 * 1024 * 1024, referer: str = "") -> Optional[bytes]:
    """只要二进制内容（图片、字体、CSS）。失败返回 None。"""
    r = fetch(url, timeout=timeout, headers=headers, max_bytes=max_bytes,
              retries=1, referer=referer)
    if r.ok and r.body:
        return r.body
    # 有些图床对 HEAD/GET 挑剔，403 也把 body 给出来试试
    if r.body and r.status in (403, 404):
        return None
    return None


def guess_mime_from_url(url: str) -> str:
    """从 URL 后缀猜 MIME。"""
    import mimetypes
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    ext = ""
    if "." in path.rsplit("/", 1)[-1]:
        ext = "." + path.rsplit(".", 1)[-1]
    mime, _ = mimetypes.guess_type("x" + ext)
    return mime or ""


def guess_mime(data: bytes, url: str = "", declared: str = "") -> str:
    """猜 MIME：声明 → 魔数 → 后缀。"""
    declared = (declared or "").split(";")[0].strip().lower()
    if declared and declared not in ("application/octet-stream", "binary/octet-stream"):
        return declared

    if data:
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[:5] == b"<?xml" or data[:4] == b"<svg":
            return "image/svg+xml"
        if data[:4] in (b"\x00\x00\x01\x00",):
            return "image/x-icon"
        if data[:4] == b"wOFF":
            return "font/woff"
        if data[:4] == b"wOF2":
            return "font/woff2"
        if data[:4] == b"OTTO":
            return "font/otf"
        if data[:4] == b"\x00\x01\x00\x00":
            return "font/ttf"

    by_ext = guess_mime_from_url(url)
    return by_ext or "application/octet-stream"
