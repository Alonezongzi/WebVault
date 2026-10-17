"""正文提取 —— Readability 风格的算法，纯标准库实现。

思路（和 Arc90 Readability 一脉相承，但针对中文网页做了调整）：

  1. 先做「垃圾清理」：去掉明显的 script/style/nav/footer/广告位
  2. 给每个候选容器打分：
       - 段落文本越多越像正文
       - 标点密度高（逗号、句号）说明是自然语言而非菜单
       - class/id 里出现 article/content/post/正文 等词加分
       - 出现 nav/sidebar/comment/ad/footer/推荐 等词重罚
       - 链接密度高 = 导航或列表页，重罚
  3. 选最高分节点，向上/向下补齐兄弟节点
  4. 清洗结果，输出干净 HTML

中文特有的调整：
  - 中文没有空格分词，所以用「标点 + 汉字数」而不是「词数」估长度
  - 链接密度阈值调高（中文导航常常一个链接包好几个字）
  - 对「阅读全文」「点击查看」这类诱导文案识别为噪音
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .parse import Node, parse
from .util import collapse_ws, normalize_text, unescape

# --------------------------------------------------------------------------
# 词表
# --------------------------------------------------------------------------

# class/id 里出现这些 → 几乎肯定是噪音
NEGATIVE_HINTS = (
    "nav", "navbar", "navigation", "menu", "breadcrumb", "breadcrumbs",
    "sidebar", "side-bar", "aside", "footer", "header", "topbar", "top-bar",
    "comment", "comments", "disqus", "remark", "reply",
    "ad-", "-ad", "ads", "advert", "advertisement", "sponsor", "promo",
    "banner", "popup", "modal", "overlay", "cookie", "consent", "gdpr",
    "share", "sharing", "social", "follow", "subscribe", "newsletter",
    "related", "recommend", "recommendation", "rec-", "hot", "popular",
    "trending", "rank", "ranking", "widget", "toolbar", "meta-nav",
    "pagination", "pager", "paging", "tags", "taglist", "bread",
    "search", "login", "signup", "signin", "register", "auth",
    "masthead", "site-header", "site-footer", "copyright", "powered",
    "backtop", "back-to-top", "goto-top", "float", "fixed-bar",
    "相关", "推荐", "热门", "广告", "评论", "导航", "菜单", "侧栏",
    "版权", "登录", "注册", "搜索", "分享", "关注",
)

# class/id 里出现这些 → 可能是正文
POSITIVE_HINTS = (
    "article", "articlebody", "article-body", "post", "post-content",
    "content", "entry", "entry-content", "main", "main-content",
    "story", "story-body", "text", "body", "detail", "details",
    "markdown", "rich-text", "rich_media", "prose", "read",
    "正文", "内容", "文章", "详情", "主体",
)

# 这些标签本身就该整体丢弃（full 模式除外）
JUNK_TAGS = frozenset(
    "script style noscript iframe object embed applet frame frameset "
    "form input button select textarea label fieldset legend "
    "svg canvas video audio map area link meta base".split()
)

# 在"保留原站样式"模式下，这两类不该被当垃圾丢掉
STYLE_TAGS = frozenset(["style", "link"])

# 结构性噪音标签
STRUCTURAL_JUNK = frozenset(
    "nav footer header aside menu menuitem dialog figure".split()
)

# 段落级标签
PARA_TAGS = frozenset("p div section article blockquote pre td li dd".split())

# 句子标点（中英）
SENTENCE_PUNCT = "。！？；.!?;…"
COMMA_PUNCT = "，,、：:"

# 诱导/噪音文案
BOILERPLATE_PATTERNS = [
    re.compile(r"^(阅读全文|查看全文|点击查看|更多详情|展开全部|继续阅读)\s*[>》»]*$"),
    re.compile(r"^(上一篇|下一篇|返回列表|回到顶部|分享到)\s*[:：]?$"),
    re.compile(r"^(广告|赞助内容|推广|Advertisement|Sponsored)\s*$", re.I),
    re.compile(r"^(登录|注册|立即登录|免费注册|关注我们|订阅)\s*$"),
    re.compile(r"^(评论|暂无评论|写下你的评论)\s*$"),
    re.compile(r"^[\s\-—_=*·。，,.]{3,}$"),
]

_HAN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_TEXT_LIKE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")


# --------------------------------------------------------------------------
# 打分工具
# --------------------------------------------------------------------------

def _collect_style(bucket: Dict[str, list], node: Node) -> None:
    """把一个 <style> 或 <link rel=stylesheet> 记录进 bucket。

    样式信息不留在正文树里（否则正文容器会带着一堆 <style>），
    而是由 strip_junk 收集后交回调用方。
    """
    if node.tag == "style":
        # 注意：不能用 text_content() —— 它刻意跳过 script/style 的文本
        # （那是给正文提取用的），这里要的恰恰是 <style> 自己的内容。
        txt = node.own_text()
        if txt and txt.strip():
            bucket["inline"].append(txt)
    elif node.tag == "link":
        rel = (node.get("rel") or "").lower()
        href = node.get("href") or ""
        if href and ("stylesheet" in rel or "icon" in rel):
            bucket["links"].append(href)


def _hint_score(node: Node) -> float:
    """根据 class/id 判断这块像不像正文。"""
    ident = " ".join([node.id, " ".join(node.classes)]).lower()
    if not ident:
        return 0.0
    score = 0.0
    for w in POSITIVE_HINTS:
        if w in ident:
            score += 25.0
    for w in NEGATIVE_HINTS:
        if w in ident:
            score -= 30.0
    return score


def text_length(text: str) -> int:
    """中文友好的"长度"：汉字每个算 1.6，其余字符算 0.6。

    为什么不是直接数字符：中文一个汉字的信息量约等于英文两个多字母，
    而英文按空格分词、标点也占字符。用 1.6 / 0.6 加权之后，中英文
    的"长度"才大致可比，后面的阈值（正文判定、链接密度）才能通用。

    注意这个值是加权分，不是字数统计 —— 别拿它当"这篇文章多少字"用。
    """
    if not text:
        return 0
    han = len(_HAN.findall(text))
    other = len(text) - han
    return int(han * 1.6 + other * 0.6)


def is_probably_readable(text: str) -> bool:
    """判断一段文字像不像自然语言句子。"""
    if not text:
        return False
    t = collapse_ws(text)
    if len(t) < 12:
        return False
    han = len(_HAN.findall(t))
    # 中文：有标点就说明是句子
    if han >= 8:
        if any(p in t for p in SENTENCE_PUNCT):
            return True
        if any(p in t for p in COMMA_PUNCT):
            return True
        # 没标点但够长，也可能是正文
        return len(t) >= 30
    # 西文：靠句号 + 空格
    if t.count(" ") >= 6 and any(p in t for p in ".!?"):
        return True
    return len(t) >= 90


def link_density(node: Node) -> float:
    """节点内链接文本占比。导航/列表页这个值很高。"""
    total = len(collapse_ws(node.text_content()))
    if total == 0:
        return 0.0
    link_len = 0
    for a in node.find_all("a"):
        link_len += len(collapse_ws(a.text_content()))
    return min(1.0, link_len / float(total))


def punctuation_density(text: str) -> float:
    """标点占比。自然语言有标点，菜单按钮没有。"""
    if not text:
        return 0.0
    t = collapse_ws(text)
    if not t:
        return 0.0
    punct = sum(1 for c in t if c in SENTENCE_PUNCT or c in COMMA_PUNCT)
    return punct / float(len(t))


def _is_boilerplate(text: str) -> bool:
    t = collapse_ws(text)
    if not t:
        return True
    for pat in BOILERPLATE_PATTERNS:
        if pat.match(t):
            return True
    return False


# --------------------------------------------------------------------------
# 清理
# --------------------------------------------------------------------------

def strip_junk(root: Node, aggressive: bool = True,
               keep_styles: bool = False) -> Dict[str, list]:
    """就地移除明显的噪音节点。

    keep_styles=True 时，页面自身的 <style> 内容与外链样式表地址会被收集起来
    并作为返回值交出（形如 {"inline": [...], "links": [...]}），供"保留原站样式"
    的阅读模式使用。这些节点本身仍然会从树里摘掉 —— 它们不该出现在正文容器内。

    返回值始终是同样的结构，keep_styles=False 时两个列表都为空。
    """
    styles: Dict[str, list] = {"inline": [], "links": []}
    to_remove = []
    for n in root.iter():
        if not n.is_element():
            continue
        tag = n.tag

        if tag in JUNK_TAGS:
            # 保留样式时，先把 <style>/<link> 的信息摘出来存好
            if keep_styles and tag in STYLE_TAGS:
                _collect_style(styles, n)
            to_remove.append(n)
            continue

        if aggressive and tag in STRUCTURAL_JUNK:
            # header/footer/nav 偶尔也包正文，用链接密度再确认一次
            if tag in ("nav", "menu", "menuitem"):
                to_remove.append(n)
                continue
            if tag in ("footer", "header", "aside"):
                if link_density(n) > 0.4 or len(collapse_ws(n.text_content())) < 200:
                    to_remove.append(n)
                    continue

        # 隐藏元素
        style = (n.get("style") or "").lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            to_remove.append(n)
            continue
        if n.has("hidden"):
            to_remove.append(n)
            continue
        aria = (n.get("aria-hidden") or "").lower()
        if aria == "true" and len(collapse_ws(n.text_content())) < 400:
            to_remove.append(n)
            continue

        # class/id 明确是噪音
        ident = " ".join([n.id, " ".join(n.classes)]).lower()
        if ident:
            if any(w in ident for w in ("ad-", "-ad", "ads-", "advert", "sponsor")):
                if len(collapse_ws(n.text_content())) < 500:
                    to_remove.append(n)
                    continue

    for n in to_remove:
        n.remove()

    return styles


def prune_empty(node: Node, keep_media: bool = True) -> None:
    """递归删掉没有内容、也没有媒体/换行的空容器。"""
    if not node.is_element():
        return
    for child in list(node.children):
        prune_empty(child, keep_media)

    if node.tag in ("img", "br", "hr", "td", "th", "li", "blockquote", "pre"):
        return

    if not node.children:
        node.remove()
        return

    has_content = False
    for c in node.children:
        if c.is_text():
            if collapse_ws(c.text):
                has_content = True
                break
        else:
            if c.tag in ("img", "br", "hr", "video", "audio", "iframe", "table"):
                has_content = True
                break
            if collapse_ws(c.text_content()):
                has_content = True
                break
    if not has_content:
        node.remove()


# --------------------------------------------------------------------------
# 打分与选择
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    node: Node
    score: float = 0.0
    text_len: int = 0

    def __repr__(self):
        return "<Cand %s score=%.1f len=%d>" % (self.node.tag, self.score, self.text_len)


def score_nodes(root: Node) -> List[Candidate]:
    """给所有候选容器打分。"""
    candidates: List[Candidate] = []
    index: Dict[int, Candidate] = {}

    for node in root.iter():
        if not node.is_element():
            continue
        if node.tag not in ("div", "article", "main", "section", "td", "body", "blockquote"):
            continue

        text = normalize_text(node.text_content())
        tl = text_length(text)
        if tl < 40:
            continue

        cand = Candidate(node=node, text_len=tl, score=0.0)
        score = 0.0

        # 基础分：文本量（开方，避免超大容器无脑赢）
        score += (tl ** 0.5) * 1.6

        # 自然语言信号
        punct = punctuation_density(text)
        if punct > 0.02:
            score += punct * 180.0

        # 链接密度惩罚
        ld = link_density(node)
        score -= ld * 120.0
        if ld > 0.6:
            score -= 100.0

        # class/id 提示
        score += _hint_score(node)

        # 语义标签加分
        if node.tag == "article":
            score += 60.0
        elif node.tag == "main":
            score += 45.0
        elif node.tag in ("div", "section"):
            score += 5.0

        # 段落数：正文通常由多个 <p> 组成
        paras = node.find_all("p")
        good_paras = sum(
            1 for p in paras
            if is_probably_readable(p.text_content())
        )
        score += good_paras * 8.0
        if good_paras >= 3:
            score += 25.0

        # 图片太少但有大量文本 → 可能是文字墙（列表/评论），轻微惩罚
        imgs = len(node.find_all("img"))
        if imgs == 0 and tl > 4000 and good_paras < 3:
            score -= 30.0

        cand.score = score
        candidates.append(cand)
        index[id(node)] = cand

    return candidates


def pick_best(candidates: List[Candidate]) -> Optional[Candidate]:
    """选出最佳候选，并做一次「父节点不显著更差就取父」的合并。"""
    if not candidates:
        return None
    best = max(candidates, key=lambda c: c.score)
    if best.score <= 0:
        return None
    return best


def expand_with_siblings(root: Node, best: Candidate, threshold: float = 0.22) -> Node:
    """把最佳节点的兄弟段落也纳进来。

    正文常常被切成 <div class="content"><p>…</p></div> 加若干平行 div，
    只取最高的那个会漏掉后续段落。
    """
    node = best.node
    parent = node.parent
    if parent is None:
        return node

    # 如果父节点的文本量比子节点多很多，说明父节点才是真正的容器
    ptext = text_length(normalize_text(parent.text_content()))
    if ptext > best.text_len * 1.6 and parent.tag in ("div", "article", "main", "section", "body"):
        gp = parent.parent
        if gp is not None:
            gtext = text_length(normalize_text(gp.text_content()))
            if gtext < ptext * 1.5:
                return parent
        return parent

    return node


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------

@dataclass
class ExtractResult:
    title: str = ""
    byline: str = ""
    excerpt: str = ""
    site_name: str = ""
    html: str = ""
    text: str = ""
    root: Optional[Node] = None
    word_count: int = 0
    image_count: int = 0
    link_count: int = 0
    confidence: float = 0.0
    method: str = "readability"
    styles: Dict[str, list] = field(default_factory=lambda: {"inline": [], "links": []})

    def has_styles(self) -> bool:
        return bool(self.styles.get("inline") or self.styles.get("links"))

    def stats(self) -> Dict[str, object]:
        return {
            "title": self.title,
            "byline": self.byline,
            "excerpt": self.excerpt,
            "site_name": self.site_name,
            "word_count": self.word_count,
            "image_count": self.image_count,
            "link_count": self.link_count,
            "confidence": round(self.confidence, 3),
            "method": self.method,
        }


def clean_content(node: Node) -> None:
    """对选中的正文容器做二次清洗。

    选中容器后，里面仍可能混着导航条、分享按钮、"上一篇/下一篇"。
    这里用「链接密度 + 文本量 + 位置」三条线索把它们挑掉。
    """
    # 1) 删掉明显是导航/分享的子孙块
    to_remove = []
    for n in node.iter():
        if not n.is_element() or n is node:
            continue
        if n.tag in ("nav", "footer", "header", "aside", "menu"):
            to_remove.append(n)
            continue
        if n.tag not in ("div", "section", "ul", "ol", "p", "span", "dl"):
            continue

        ident = " ".join([n.id, " ".join(n.classes)]).lower()
        txt = normalize_text(n.text_content())
        ld = link_density(n)

        # class/id 明确是噪音，且内容不多 → 删
        if ident and any(w in ident for w in NEGATIVE_HINTS):
            if len(txt) < 400 and ld > 0.15:
                to_remove.append(n)
                continue

        # 纯链接块 → 导航
        # 但对 <p> 放行：段落是正文单位，整段一个链接很常见（参考链接、延伸阅读）。
        # 只有当这个 <p> 本身还带噪音 class 时才按链接密度删。
        if n.tag == "p":
            if ident and any(w in ident for w in NEGATIVE_HINTS) and len(txt) < 200:
                to_remove.append(n)
            continue
        if ld > 0.75 and len(txt) < 300:
            to_remove.append(n)
            continue

        # 大量短链接 + 几乎没标点 → 列表导航
        links = n.find_all("a")
        if len(links) >= 4 and punct_count(txt) <= 1 and len(txt) < 400:
            to_remove.append(n)
            continue

    for n in to_remove:
        n.remove()

    # 2) 删掉孤立的样板文字节点
    for n in list(node.iter()):
        if n.is_text() and n.parent is not None and n.parent is not node:
            if _is_boilerplate(n.text):
                n.remove()

    # 3) 去掉头尾的导航残渣：如果开头/结尾的块链接密度极高，删掉
    _trim_edges(node)


def punct_count(text: str) -> int:
    return sum(1 for c in (text or "") if c in SENTENCE_PUNCT or c in COMMA_PUNCT)


def _trim_edges(node: Node, max_rounds: int = 6) -> None:
    """从首尾反复剥掉低质量块。"""
    for _ in range(max_rounds):
        changed = False
        kids = [c for c in node.children if c.is_element()]
        if len(kids) <= 1:
            break
        # 开头
        head = kids[0]
        if _is_low_quality_block(head):
            head.remove()
            changed = True
            kids = [c for c in node.children if c.is_element()]
            if not kids:
                break
        # 结尾
        tail = kids[-1]
        if _is_low_quality_block(tail):
            tail.remove()
            changed = True
        if not changed:
            break


def _is_low_quality_block(n: Node) -> bool:
    """判断一个块是不是该从正文里剥掉的边角料。"""
    if not n.is_element():
        return False
    if n.tag in ("h1", "h2", "h3", "table", "pre", "blockquote", "figure", "img"):
        return False
    txt = normalize_text(n.text_content())

    # <p> 是正文的基本单位，不能因为"链接密度高"就删。
    # 真实文章里到处都是整段只放一个链接的情况（参考资料、扩展阅读）。
    # 只按样板文案判断。
    if n.tag == "p":
        return _is_boilerplate(txt)

    if text_length(txt) >= 600:
        return False
    ld = link_density(n)
    ident = " ".join([n.id, " ".join(n.classes)]).lower()
    if ident and any(w in ident for w in ("nav", "pager", "pagination", "share", "breadcrumb",
                                          "上一篇", "下一篇", "相关", "推荐", "ad-")):
        return True
    if ld > 0.5:
        return True
    if len(txt) < 40 and punct_count(txt) == 0:
        return True
    return False


def _find_title(root: Node, meta: Dict[str, str], url: str) -> str:
    for key in ("og:title", "twitter:title"):
        if meta.get(key):
            return normalize_text(unescape(meta[key]))
    t = root.find("title")
    if t is not None:
        s = normalize_text(unescape(t.text_content()))
        if s:
            return s
    h1 = root.find("h1")
    if h1 is not None:
        s = normalize_text(unescape(h1.text_content()))
        if s:
            return s
    return ""


# 作者名一定不该包含这些 —— 出现就说明抓到了句子或评论
_BYLINE_REJECT = re.compile(
    r"[:：。！？；!?;]|\s{2,}|\n|说\s*[:：]|评论|回复|楼主|匿名|admin$|^匿名",
    re.I,
)


def _byline_ok(txt: str) -> bool:
    """作者名的合理性检查。"""
    if not txt:
        return False
    t = collapse_ws(txt)
    if not (1 <= len(t) <= 40):
        return False
    if _BYLINE_REJECT.search(t):
        return False
    if _is_boilerplate(t):
        return False
    # 作者名通常不含大量汉字句读，也不该是短语
    if t.count(" ") > 4:
        return False
    # 不能是 URL / 邮箱 / 纯数字
    if t.startswith("http") or "@" in t or t.isdigit():
        return False
    # 中文作者名一般很短
    han = len(_HAN.findall(t))
    if han > 0 and han > 12:
        return False
    return True


def _find_byline(root: Node, meta: Dict[str, str]) -> str:
    """找作者。

    坑：很多站点评论区也有 "author" 或用户名 class，直接搜会抓到
    「刺猬说:」这种评论者。所以策略是：
      1. meta 优先（最可靠）
      2. 只在"可能是文章头部"的范围内找，并要求文本短、不含句读
      3. weak 词（name/user）必须搭配强词才采信
    """
    for key in ("article:author", "author", "og:article:author", "twitter:creator"):
        v = meta.get(key)
        if v:
            s = normalize_text(unescape(v))
            if s.startswith("http"):
                # og 里常是作者主页链接，取最后一段
                s = s.rstrip("/").rsplit("/", 1)[-1]
            if _byline_ok(s):
                return s

    for a in root.find_all("a"):
        if (a.get("rel") or "").lower().find("author") >= 0:
            s = normalize_text(unescape(a.text_content()))
            if _byline_ok(s):
                return s

    # 强信号 class 名
    strong = ("byline", "author-name", "authorname", "post-author", "article-author",
              "entry-author", "articleauthor", "writer", "作者", "作者名")
    for el in root.find_all(("span", "a", "div", "p", "cite", "address", "em", "strong")):
        ident = " ".join([el.id, " ".join(el.classes)]).lower()
        if not ident:
            continue
        if not any(w in ident for w in strong):
            continue
        if any(w in ident for w in ("comment", "reply", "remark", "review", "评论", "回复")):
            continue
        txt = normalize_text(unescape(el.text_content()))
        if _byline_ok(txt):
            return txt

    return ""


def _find_excerpt(root: Node, meta: Dict[str, str]) -> str:
    for key in ("og:description", "description", "twitter:description"):
        if meta.get(key):
            s = normalize_text(unescape(meta[key]))
            if s:
                return s
    return ""


def _site_name(root: Node, meta: Dict[str, str], url: str) -> str:
    if meta.get("og:site_name"):
        return normalize_text(meta["og:site_name"])
    from .util import url_host
    host = url_host(url)
    if host.startswith("www."):
        host = host[4:]
    return host


def extract(html: str, url: str = "", *, aggressive: bool = True,
            fast_parse: bool = True, keep_styles: bool = False) -> ExtractResult:
    """从 HTML 里抽出正文。永不抛异常；实在抽不出就退回 body。

    keep_styles=True 时，页面自身的 <style> 内容与样式表链接会被收集起来
    （放在 ExtractResult.styles 上），供"保留原站样式"的阅读模式使用。
    """
    from .parse import get_meta_map

    res = ExtractResult()
    if not html or not html.strip():
        return res

    try:
        root = parse(html, fast=fast_parse)
    except Exception:
        return res

    meta = get_meta_map(root)
    res.title = _find_title(root, meta, url)
    res.byline = _find_byline(root, meta)
    res.excerpt = _find_excerpt(root, meta)
    res.site_name = _site_name(root, meta, url)

    # 清理（需要保留样式时，边清理边收集）
    collected = strip_junk(root, aggressive=aggressive, keep_styles=keep_styles)
    if keep_styles:
        res.styles = collected

    # 打分
    candidates = score_nodes(root)
    best = pick_best(candidates)

    chosen: Optional[Node] = None
    confidence = 0.0

    if best is not None:
        chosen = expand_with_siblings(root, best)
        # 置信度：得分 + 文本量 +
        total_text = text_length(normalize_text(root.text_content()))
        if total_text > 0:
            ratio = best.text_len / float(total_text)
            confidence = min(1.0, (best.score / 400.0) * 0.6 + ratio * 0.4)

    if chosen is None or text_length(normalize_text(chosen.text_content())) < 60:
        # 退回：body 或整棵树
        body = root.find("body")
        chosen = body if body is not None else root
        res.method = "fallback"
        confidence = min(confidence, 0.25)
    else:
        res.method = "readability"

    res.confidence = confidence
    res.root = chosen
    clean_content(chosen)
    prune_empty(chosen)

    res.html = chosen.to_html()
    res.text = normalize_text(chosen.text_content())
    res.word_count = text_length(res.text)
    res.image_count = len(chosen.find_all("img"))
    res.link_count = len(chosen.find_all("a"))
    return res


def refresh(res: ExtractResult) -> ExtractResult:
    """内联等操作改过 DOM 之后，重新同步派生字段。

    调用方如果不做这一步，res.html/res.text 会和 res.root 不一致。
    """
    if res.root is None:
        return res
    res.html = res.root.to_html()
    res.text = normalize_text(res.root.text_content())
    res.word_count = text_length(res.text)
    res.image_count = len(res.root.find_all("img"))
    res.link_count = len(res.root.find_all("a"))
    return res


# 这些标签在正文里代表"一个独立的块"，块与块之间应该有换行。
# 变更检测按块比对才有意义：否则 <h1> 会和后面第一段粘成一行，
# 改一个字就显示成"整段都变了"。
BLOCK_TAGS = (
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre",
    "td", "th", "dd", "dt", "figcaption", "caption", "summary",
)


def block_text(node: Optional[Node]) -> str:
    """按块提取纯文本，块之间用换行分隔。

    和 `ExtractResult.text` 的区别：那个是把整篇文字压成一行（适合喂给
    LLM 或做字数统计），这个保留了段落边界（适合做版本比对）。
    """
    if node is None:
        return ""
    lines: List[str] = []

    def walk(n: Node) -> None:
        if n.is_element():
            if n.tag in ("script", "style"):
                return
            if n.tag in BLOCK_TAGS:
                # 块内的文字自己收集，不再往下递归（避免嵌套重复）
                t = normalize_text(n.text_content())
                if t:
                    lines.append(t)
                return
        elif n.kind == "text":
            t = normalize_text(n.text or "")
            if t:
                lines.append(t)
            return
        for c in n.children:
            walk(c)

    walk(node)
    # 去掉空行
    return "\n".join(l for l in lines if l.strip())


def extract_text(html: str, url: str = "") -> str:
    return extract(html, url).text
