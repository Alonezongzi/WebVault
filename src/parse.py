"""容错 HTML 解析器 —— 构建一棵可查询、可修改的 DOM 树。

为什么不用 lxml/html5lib：
  - 本工具承诺零依赖，必须在只有标准库的机器上跑
  - lxml 存在时会走 lxml 快路径（见 parse_html_fast），但树结构统一

设计要点：
  - 基于 html.parser，但自己维护开放元素栈，处理真实世界的残缺 HTML
  - 记录每个节点的源码起止位置，便于"原样保留"未知片段
  - VOID 元素、隐式闭合规则（<p> 遇 <div> 自动闭合等）都实现
  - 解析失败绝不抛异常：宁可产出一棵粗糙的树，也不能让整个流程挂掉
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Dict, Iterator, List, Optional, Tuple

# 自闭合（空）元素
VOID_ELEMENTS = frozenset(
    """area base br col embed hr img input link meta param source track wbr
    basefont bgsound frame keygen""".split()
)

# 这些元素的内容是原始文本，不能当 HTML 解析
RAW_TEXT_ELEMENTS = frozenset(["script", "style", "textarea", "title"])

# 隐式闭合规则：遇到 key 时，栈顶若是 value 中的元素则自动闭合
IMPLICIT_CLOSE: Dict[str, frozenset] = {
    "li": frozenset(["li"]),
    "dt": frozenset(["dt", "dd"]),
    "dd": frozenset(["dt", "dd"]),
    "tr": frozenset(["tr", "td", "th"]),
    "td": frozenset(["td", "th"]),
    "th": frozenset(["td", "th"]),
    "option": frozenset(["option"]),
    "optgroup": frozenset(["optgroup", "option"]),
    "thead": frozenset(["thead", "tbody", "tfoot", "tr", "td", "th"]),
    "tbody": frozenset(["thead", "tbody", "tfoot", "tr", "td", "th"]),
    "tfoot": frozenset(["thead", "tbody", "tfoot", "tr", "td", "th"]),
    "p": frozenset(["p"]),
    "h1": frozenset(["p"]),
    "h2": frozenset(["p"]),
    "h3": frozenset(["p"]),
    "h4": frozenset(["p"]),
    "h5": frozenset(["p"]),
    "h6": frozenset(["p"]),
}

# 块级元素：<p> 遇到它们要闭合
BLOCK_FOR_P = frozenset(
    """address article aside blockquote details div dl fieldset figcaption figure
    footer form h1 h2 h3 h4 h5 h6 header hgroup hr main menu nav ol p pre
    section table ul""".split()
)

# 容器类元素 —— 正文提取时的重要信号
CONTENT_TAGS = frozenset(["article", "main", "section", "div", "td", "body"])

# 完全丢弃的元素（广告、脚本、导航噪音）—— 这是默认策略，可配置
DEFAULT_DROP_TAGS = frozenset(
    """script noscript iframe object embed applet frame frameset
    form input button select textarea label fieldset legend
    svg canvas video audio track source map area
    link meta base""".split()
)


class Node:
    """HTML 节点。element 或 text 二选一。"""

    __slots__ = (
        "tag", "attrs", "children", "parent", "text",
        "start", "end", "kind", "self_closing",
    )

    def __init__(self, tag: str = "", attrs=None, kind: str = "element"):
        self.tag = (tag or "").lower()
        self.attrs: Dict[str, str] = dict(attrs or {})
        self.children: List["Node"] = []
        self.parent: Optional["Node"] = None
        self.text: str = ""
        self.start: int = -1
        self.end: int = -1
        self.kind: str = kind  # 'element' | 'text' | 'root'
        self.self_closing: bool = False

    # ---- 属性访问 ----
    def get(self, name: str, default: str = "") -> str:
        return self.attrs.get((name or "").lower(), default)

    def has(self, name: str) -> bool:
        return (name or "").lower() in self.attrs

    def set(self, name: str, value: str) -> None:
        self.attrs[(name or "").lower()] = value

    def drop(self, name: str) -> None:
        self.attrs.pop((name or "").lower(), None)

    @property
    def classes(self) -> List[str]:
        return (self.attrs.get("class") or "").split()

    @property
    def id(self) -> str:
        return self.attrs.get("id") or ""

    # ---- 树操作 ----
    def append(self, child: "Node") -> "Node":
        child.parent = self
        self.children.append(child)
        return child

    def remove(self) -> None:
        if self.parent is not None:
            try:
                self.parent.children.remove(self)
            except ValueError:
                pass
            self.parent = None

    def is_element(self) -> bool:
        return self.kind == "element"

    def is_text(self) -> bool:
        return self.kind == "text"

    # ---- 遍历 ----
    def iter(self) -> Iterator["Node"]:
        """深度优先遍历自身及所有后代。"""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            if node.children:
                stack.extend(reversed(node.children))

    def find_all(self, tags) -> List["Node"]:
        if isinstance(tags, str):
            tags = {tags}
        else:
            tags = set(tags)
        return [n for n in self.iter() if n.is_element() and n.tag in tags]

    def find(self, tags) -> Optional["Node"]:
        if isinstance(tags, str):
            tags = {tags}
        else:
            tags = set(tags)
        for n in self.iter():
            if n.is_element() and n.tag in tags:
                return n
        return None

    def find_by_id(self, node_id: str) -> Optional["Node"]:
        for n in self.iter():
            if n.is_element() and n.id == node_id:
                return n
        return None

    # ---- 内容 ----
    def text_content(self) -> str:
        """所有后代文本拼接（不含 script/style）。"""
        parts = []
        for n in self.iter():
            if n.kind == "text":
                p = n.parent
                if p is not None and p.tag in ("script", "style"):
                    continue
                parts.append(n.text)
        return "".join(parts)

    def own_text(self) -> str:
        """仅直接文本子节点。"""
        return "".join(c.text for c in self.children if c.is_text())

    def all_text_len(self) -> int:
        return len(self.text_content())

    # ---- 序列化 ----
    def to_html(self) -> str:
        out = []
        self._serialize(out)
        return "".join(out)

    def _serialize(self, out: List[str]) -> None:
        if self.kind == "root":
            for c in self.children:
                c._serialize(out)
            return
        if self.kind == "text":
            out.append(self.text)
            return

        tag = self.tag
        if not tag:
            for c in self.children:
                c._serialize(out)
            return

        out.append("<")
        out.append(tag)
        for k, v in self.attrs.items():
            if v is None or v == "":
                out.append(" %s" % k)
            else:
                out.append(' %s="%s"' % (k, str(v).replace('"', "&quot;")))
        out.append(">")

        if tag in VOID_ELEMENTS:
            return

        for c in self.children:
            c._serialize(out)

        out.append("</%s>" % tag)

    def pretty(self, indent: int = 0, _depth: int = 0) -> str:
        """调试用缩进输出。"""
        pad = "  " * _depth
        if self.kind == "text":
            t = self.text.strip()
            return ("%s%s\n" % (pad, t[:120])) if t else ""
        if self.kind == "root":
            return "".join(c.pretty(indent, _depth) for c in self.children)
        head = "%s<%s%s>\n" % (
            pad, self.tag,
            "".join(' %s="%s"' % (k, v) for k, v in list(self.attrs.items())[:4]),
        )
        body = "".join(c.pretty(indent, _depth + 1) for c in self.children)
        return head + body

    def __repr__(self) -> str:
        if self.kind == "text":
            return "<text %r>" % (self.text[:30],)
        return "<%s %s>" % (self.tag, dict(list(self.attrs.items())[:2]))


class _TreeBuilder(HTMLParser):
    """把 html.parser 的事件流建成 DOM 树，自己做容错闭合。"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.root = Node(kind="root")
        self.stack: List[Node] = [self.root]
        self.pending_text: List[str] = []
        self._raw_tag: Optional[str] = None

    # ---- 内部 ----
    @property
    def current(self) -> Node:
        return self.stack[-1]

    def _flush_text(self) -> None:
        if not self.pending_text:
            return
        raw = "".join(self.pending_text)
        self.pending_text = []
        if not raw:
            return
        node = Node(kind="text")
        node.text = raw
        node.start = self.getpos()[1]
        self.current.append(node)

    def _close_until(self, tags) -> None:
        """向上弹出直到关掉匹配的元素。"""
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag in tags:
                del self.stack[i:]
                return

    def _auto_close(self, tag: str) -> None:
        """处理隐式闭合。

        关键：不能只看栈顶。像 <table><tr><td>1<td>2<tr>... 里，遇到新的 <tr>
        时栈顶是 <td>，必须一路向上弹出 td/tr 才能让 tr 成为兄弟节点。
        """
        # 表格结构：向上弹出所有需要为此标签让位的元素。
        # 注意 <table><tr><td>1<td>2<tr> —— 遇到第二个 <tr> 时，栈是
        # [table, tr, td]，既要关掉 td 也要关掉 tr，所以命中后要继续向上找，
        # 直到真正找到与 tag 同名的元素（真正的兄弟层级）。
        rule = IMPLICIT_CLOSE.get(tag)
        if rule:
            cut = -1
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i].tag == tag:
                    # 找到同名元素：把它及其上全部弹出，新元素成为它的兄弟
                    cut = i
                    break
                if self.stack[i].tag in rule:
                    # 命中"让位者"，记下位置但继续向上，看还有没有同名元素
                    if cut < 0:
                        cut = i
                    continue
                if self.stack[i].tag in ("table", "body", "html", "ul", "ol", "dl", "select"):
                    # 到达容器边界，停止上溯
                    break
            if cut > 0:
                del self.stack[cut:]

        # <p> 遇到块级元素要闭合
        if tag in BLOCK_FOR_P:
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i].tag == "p":
                    del self.stack[i:]
                    break
                if self.stack[i].tag in ("body", "html", "div", "td", "li", "blockquote"):
                    break

    # ---- HTMLParser 回调 ----
    def handle_starttag(self, tag, attrs):
        self._flush_text()
        tag = tag.lower()
        self._auto_close(tag)
        node = Node(tag, {k.lower(): (v if v is not None else "") for k, v in attrs})
        self.current.append(node)
        if tag not in VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._flush_text()
        tag = tag.lower()
        node = Node(tag, {k.lower(): (v if v is not None else "") for k, v in attrs})
        node.self_closing = True
        self.current.append(node)

    def handle_endtag(self, tag):
        self._flush_text()
        tag = tag.lower()
        if tag in VOID_ELEMENTS:
            return
        # 找到最近的匹配元素并关掉它
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # 没找到匹配 —— 真实世界的 HTML 经常这样，忽略即可

    def handle_data(self, data):
        if data:
            self.pending_text.append(data)

    def handle_entityref(self, name):
        self.pending_text.append("&%s;" % name)

    def handle_charref(self, name):
        self.pending_text.append("&#%s;" % name)

    def handle_comment(self, data):
        # 注释直接丢，不保留（注释里可能有条件编译，但少见且体积大）
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    def close(self):
        super().close()
        self._flush_text()


_UNCLOSED_ATTR = re.compile(r'=\s*"([^"<>]*?)(?=[<>])')
_UNCLOSED_ATTR_S = re.compile(r"=\s*'([^'<>]*?)(?=[<>])")


def _repair_attrs(html: str) -> str:
    """修复最常见的致命破损：属性值的引号没有闭合。

    `<div class="a><p>x</p>` —— html.parser 会直接吞掉后面所有内容。
    我们给未闭合的引号补上，让解析继续。
    """
    if '"' not in html and "'" not in html:
        return html
    if html.count('"') % 2 == 0 and html.count("'") % 2 == 0:
        return html
    out = _UNCLOSED_ATTR.sub(lambda m: '="%s"' % m.group(1), html)
    out = _UNCLOSED_ATTR_S.sub(lambda m: "='%s'" % m.group(1), out)
    return out


def parse_html(html: str) -> Node:
    """解析 HTML 字符串，返回根节点。永不抛异常。"""
    if not html:
        return Node(kind="root")
    builder = _TreeBuilder()
    failed = False
    try:
        builder.feed(html)
        builder.close()
    except Exception:
        failed = True

    # 解析中断且产出过少 —— 尝试修复破损属性后重来一次
    if failed or len(builder.root.children) == 0:
        repaired = _repair_attrs(html)
        if repaired != html:
            b2 = _TreeBuilder()
            try:
                b2.feed(repaired)
                b2.close()
                if len(list(b2.root.iter())) > len(list(builder.root.iter())):
                    return b2.root
            except Exception:
                try:
                    b2.close()
                except Exception:
                    pass
                if len(list(b2.root.iter())) > len(list(builder.root.iter())):
                    return b2.root

    if failed:
        try:
            builder.close()
        except Exception:
            pass
    return builder.root


# --------------------------------------------------------------------------
# lxml 快路径（可选增强）
# --------------------------------------------------------------------------

_LXML_OK = None


def has_lxml() -> bool:
    global _LXML_OK
    if _LXML_OK is None:
        try:
            import lxml.html  # noqa: F401
            _LXML_OK = True
        except Exception:
            _LXML_OK = False
    return bool(_LXML_OK)


def parse_html_fast(html: str) -> Node:
    """有 lxml 就用 lxml 提速，失败自动退回纯 stdlib。"""
    if not has_lxml():
        return parse_html(html)
    try:
        import lxml.html as LH
        from lxml import etree

        doc = LH.fromstring(html)
        root = Node(kind="root")

        def conv(el) -> Node:
            n = Node(el.tag if isinstance(el.tag, str) else "div",
                     {str(k).lower(): ("" if v is None else str(v))
                      for k, v in (el.attrib or {}).items()})
            if el.text:
                t = Node(kind="text")
                t.text = el.text
                n.append(t)
            for child in el:
                if not isinstance(child.tag, str):
                    # 注释 / PI
                    if child.tail:
                        t = Node(kind="text")
                        t.text = child.tail
                        n.append(t)
                    continue
                n.append(conv(child))
                if child.tail:
                    t = Node(kind="text")
                    t.text = child.tail
                    n.append(t)
            return n

        # 关键：要把 doc 自己转换并挂上，而不是只遍历它的子节点。
        # lxml 对片段输入可能直接返回一个叶子元素（比如 <a>…</a>），
        # 这时"遍历子节点"会把整个内容丢掉 —— 曾经导致 discover()
        # 对片段 HTML 返回空列表。
        root.append(conv(doc))
        return root
    except Exception:
        return parse_html(html)


def parse(html: str, fast: bool = True) -> Node:
    if fast:
        return parse_html_fast(html)
    return parse_html(html)


# --------------------------------------------------------------------------
# 便捷函数
# --------------------------------------------------------------------------

def get_title(root: Node) -> str:
    """尽力取出标题：<title> → og:title → 第一个 h1。"""
    from .util import collapse_ws, unescape

    t = root.find("title")
    if t is not None:
        s = collapse_ws(unescape(t.text_content()))
        if s:
            return s
    for m in root.find_all("meta"):
        prop = (m.get("property") or m.get("name") or "").lower()
        if prop in ("og:title", "twitter:title"):
            s = collapse_ws(unescape(m.get("content")))
            if s:
                return s
    h1 = root.find("h1")
    if h1 is not None:
        s = collapse_ws(unescape(h1.text_content()))
        if s:
            return s
    return ""


def get_meta_map(root: Node) -> Dict[str, str]:
    """收集所有 meta 标签成 dict。"""
    out: Dict[str, str] = {}
    for m in root.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").strip().lower()
        if not key:
            key = (m.get("http-equiv") or "").strip().lower()
        if key and m.has("content"):
            out.setdefault(key, m.get("content"))
    return out


def get_base_href(root: Node, fallback: str = "") -> str:
    b = root.find("base")
    if b is not None and b.has("href"):
        return b.get("href") or fallback
    return fallback


def collect_links(root: Node) -> List[Node]:
    return root.find_all("a")


def collect_images(root: Node) -> List[Node]:
    return root.find_all("img")
