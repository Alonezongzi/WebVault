"""链接发现 —— 从一个页面里找出值得归档的文章链接。

这是给"我有 20 个想存的页面，但懒得一个个粘"准备的：
给一个列表页（或者任意页面），自动挑出正文链接，然后批量存。

它**不是爬虫框架**：不递归、不遵守 robots、不做去重队列持久化。
它就是一个"把这一页上的文章链接找出来"的工具，配合 vault_many 用。

筛选逻辑：
  - 同域优先（默认只留同域，可用 --any-host 放开）
  - 排除明显的导航/功能链接（登录、注册、标签、分类、翻页、锚点）
  - 排除资源链接（.jpg/.css/.zip 等）
  - 按 URL 形态打分：路径越像文章（有 slug、有数字 ID、层级适中）分越高
  - 可选：只看页面上"看起来像标题"的链接（文字较长、不是纯功能词）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set
from urllib.parse import urlsplit

from .parse import Node, parse
from .util import (
    collapse_ws, normalize_url_for_dedup, safe_urljoin, unescape,
    url_host, url_scheme,
)

# 明显不是文章的后缀
SKIP_EXT = frozenset("""
jpg jpeg png gif webp svg ico bmp tiff avif
css js mjs json xml rss atom txt csv
zip rar 7z tar gz bz2 xz
mp3 mp4 avi mov mkv wav flac webm ogv
pdf doc docx xls xlsx ppt pptx
exe msi dmg apk ipa deb rpm
woff woff2 ttf otf eot
""".split())

# URL 路径里出现这些片段 → 大概率不是文章
SKIP_PATH_HINTS = (
    "/login", "/signin", "/signup", "/register", "/logout", "/auth",
    "/cart", "/checkout", "/pay", "/account", "/profile", "/settings",
    "/search", "/tag/", "/tags/", "/category/", "/categories/", "/cate/",
    "/feed", "/rss", "/sitemap", "/robots", "/wp-json", "/api/",
    "/privacy", "/terms", "/about", "/contact", "/help", "/faq",
    "/cookie", "/legal", "/dmca", "/advertise",
    "/javascript:", "/mailto:",
)

# 链接文字是这些 → 功能性链接，不是文章
SKIP_TEXT = frozenset("""
首页 主页 关于 关于我们 联系 联系我们 登录 注册 登陆 退出 注销 我的
更多 全部 查看全部 更多>> 下一页 上一页 下一页> 上一页< 返回 返回首页
设置 帮助 隐私 隐私政策 服务条款 用户协议 版权 声明 广告 合作 投稿
搜索 提交 评论 分享 收藏 点赞 关注 订阅 下载 APP 客户端 手机版
home about contact login signup register logout more next prev
back search help privacy terms settings download share subscribe
""".split())

_HAN = re.compile(r"[\u4e00-\u9fff]")
_NUM_ID = re.compile(r"/(\d{3,})(?:/|$|\.html?$)")
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+){2,}", re.I)
_DATE_IN_PATH = re.compile(r"/(20\d{2})/(\d{1,2})/")


@dataclass
class Link:
    url: str = ""
    text: str = ""
    score: float = 0.0
    host: str = ""
    same_host: bool = True
    reason: str = ""

    def to_dict(self) -> Dict:
        return {
            "url": self.url, "text": self.text, "score": round(self.score, 2),
            "host": self.host, "same_host": self.same_host,
        }


def _clean_text(node: Node) -> str:
    t = collapse_ws(unescape(node.text_content()))
    # 去掉末尾的箭头/符号
    t = re.sub(r"[>》»→\s]+$", "", t).strip()
    return t


def _is_skippable(url: str, text: str) -> Optional[str]:
    """返回跳过原因，不跳过则返回 None。"""
    if not url:
        return "空"
    s = url_scheme(url)
    if s not in ("http", "https"):
        return "非 http(s)"
    low = url.lower()
    for h in SKIP_PATH_HINTS:
        if h in low:
            return "功能链接"
    path = urlsplit(url).path or ""
    if "." in path.rsplit("/", 1)[-1]:
        ext = path.rsplit(".", 1)[-1].lower()
        if ext in SKIP_EXT:
            return "资源文件"
    if path in ("", "/"):
        return "首页"
    t = (text or "").strip()
    if not t:
        return "无文字"
    if t.lower() in SKIP_TEXT or t in SKIP_TEXT:
        return "功能文字"
    if len(t) < 2:
        return "文字太短"
    return None


def _score(url: str, text: str, same_host: bool, min_text: int) -> float:
    """给一个候选链接打分。分越高越像文章。"""
    score = 0.0
    path = urlsplit(url).path or ""

    # 路径深度：1-3 层最像文章，太深像归档页，太浅像栏目
    depth = len([p for p in path.split("/") if p])
    if depth == 1:
        score += 4
    elif depth == 2:
        score += 6
    elif depth == 3:
        score += 5
    elif depth == 4:
        score += 3
    else:
        score += 1

    # 结尾是 .html / .htm / 无扩展名且不是目录 → 像文章
    if re.search(r"\.html?$", path, re.I):
        score += 5
    elif path.endswith("/"):
        score -= 2
    else:
        score += 2

    # 有 slug 或数字 ID
    if _SLUG.search(path):
        score += 3
    if _NUM_ID.search(path + ("/" if not path.endswith("/") else "")):
        score += 3
    if _DATE_IN_PATH.search(path):
        score += 3

    # 链接文字像标题：越长越像（但太长的多半是摘要块）
    tl = len(text)
    if tl >= min_text:
        score += 4
    if tl >= 12:
        score += 2
    if tl > 120:
        score -= 3
    # 有标点 → 更像标题而不是按钮
    if any(c in text for c in "，。！？：,.:!?"):
        score += 1
    # 汉字标题
    if _HAN.search(text):
        score += 1

    if same_host:
        score += 3

    return score


def discover(
    html: str,
    base_url: str,
    *,
    same_host_only: bool = True,
    min_text: int = 6,
    min_score: float = 8.0,
    limit: int = 200,
    dedupe: bool = True,
) -> List[Link]:
    """从 HTML 里发现候选文章链接，按分数从高到低返回。"""
    root = parse(html)
    host = url_host(base_url)
    seen: Set[str] = set()
    out: List[Link] = []

    # 优先看 <article> / <main> 里的链接（更可能是正文）
    containers = []
    for tag in ("main", "article"):
        containers.extend(root.find_all(tag))
    # 也扫全页（很多列表页没有语义标签）
    scopes = [(c, 3.0) for c in containers] + [(root, 0.0)]

    for scope, bonus in scopes:
        for a in scope.find_all("a"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            url = safe_urljoin(base_url, href)
            text = _clean_text(a)
            reason = _is_skippable(url, text)
            if reason:
                continue
            h = url_host(url)
            same = (h == host)
            if same_host_only and not same:
                continue
            key = normalize_url_for_dedup(url)
            if dedupe and key in seen:
                continue
            seen.add(key)
            sc = _score(url, text, same, min_text) + bonus
            if sc < min_score:
                continue
            out.append(Link(url=url, text=text[:200], score=sc, host=h,
                            same_host=same, reason=""))

    out.sort(key=lambda x: x.score, reverse=True)
    return out[:limit]


def discover_from_url(
    fetcher,
    url: str,
    *,
    same_host_only: bool = True,
    min_text: int = 6,
    min_score: float = 8.0,
    limit: int = 200,
) -> List[Link]:
    """抓取一个页面并发现链接。fetcher 是一个 callable(url) -> str|None。"""
    html = fetcher(url)
    if not html:
        return []
    return discover(html, url, same_host_only=same_host_only, min_text=min_text,
                    min_score=min_score, limit=limit)
