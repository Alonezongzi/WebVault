#!/usr/bin/env python3
"""WebVault —— 把网页变成单文件离线副本。

命令行入口。

用法示例：
    python webvault.py https://example.com
    python webvault.py https://example.com -m full -o D:\\saves
    python webvault.py -i urls.txt -o D:\\saves --workers 6
    python webvault.py https://example.com -m data --stdout
    python webvault.py index -o D:\\saves            # 生成书库索引
    python webvault.py discover https://site.com/list  # 发现文章链接
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# 允许直接运行（把上级目录加进 path）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from src import __version__  # noqa: E402
from src.inline import InlineOptions  # noqa: E402
from src.pipeline import vault, vault_many  # noqa: E402
from src.util import human_size  # noqa: E402


def _setup_console() -> None:
    """Windows 控制台编码修正，避免中文输出乱码。"""
    try:
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


BANNER = r"""
 __      __     _  __     __         _ _
 \ \    / /    | | \ \   / /        | | |
  \ \  / /__ __| |__\ \ / /_ _ _   _| | |_
   \ \/ / _ \ '_ \ __\ V / _` | | | | | __|
    \  /  __/ |_| | |_ | | (_| | |_| | | |_
     \/ \___|\__,_|\__| |_|\__,_|\__,_|_|\__|

 网页 → 单文件离线副本          v%s
""".strip("\n") % __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="webvault",
        description="把网页保存成自包含的单文件离线副本。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
模式说明:
  reader  只保留正文，干净排版，适合阅读/分享/存档文章  (默认)
  full    保留整页结构与样式，只内联资源，适合原样存档
  data    不产出页面，产出结构化 JSON，适合喂给爬虫/分析流程

子命令:
  %(prog)s discover <列表页>    从一个页面里找出所有文章链接
  %(prog)s index <目录>         把存下来的页面变成可搜索的书库
  %(prog)s diff <网址>          重新抓一次，和上次存的比，看改了什么

  子命令的详细参数用 "%(prog)s <子命令> -h" 查看。

示例:
  %(prog)s https://example.com
  %(prog)s news.site.com/article/123 -m full
  %(prog)s -i urls.txt -o D:\\saves --workers 6
  %(prog)s https://example.com -m data --stdout

  %(prog)s discover https://blog.example.com/ -n 10
  %(prog)s discover https://blog.example.com/ --save -o D:\\saves
  %(prog)s index D:\\saves
  %(prog)s diff https://example.com/notice -o D:\\saves
""",
    )

    p.add_argument("url", nargs="*", help="要保存的网址（可多个）")
    p.add_argument("-o", "--out", default="", help="输出目录（默认：当前目录下的 vault/）")
    p.add_argument("-m", "--mode", default="reader", choices=("reader", "full", "data"),
                   help="保存模式（默认 reader）")
    p.add_argument("-i", "--input", default="", help="从文件读取 URL 列表（每行一个，# 开头忽略）")
    p.add_argument("-n", "--name", default="", help="指定输出文件名（单个 URL 时有效）")
    p.add_argument("--stdout", action="store_true", help="输出到标准输出，不写文件")
    p.add_argument("--workers", type=int, default=4, help="批量并发数（默认 4）")
    p.add_argument("--timeout", type=float, default=25.0, help="单个请求超时秒数（默认 25）")

    g = p.add_argument_group("资源内联")
    g.add_argument("--no-images", action="store_true", help="不内联图片")
    g.add_argument("--no-css", action="store_true", help="不内联 CSS")
    g.add_argument("--no-fonts", action="store_true", help="不内联字体")
    g.add_argument("--scripts", default="keep", choices=("keep", "inline", "drop"),
                   help="脚本处理方式（默认 keep）")
    g.add_argument("--max-total", type=int, default=24, metavar="MB",
                   help="内联资源总体积上限 MB（默认 24）")
    g.add_argument("--max-asset", type=int, default=4, metavar="MB",
                   help="单个资源体积上限 MB（默认 4）")
    g.add_argument("--no-compress", action="store_true", help="不压缩图片（保留原图）")
    g.add_argument("--max-width", type=int, default=1600, help="图片最大宽度（默认 1600）")
    g.add_argument("--local-only", action="store_true", help="只抓同域资源，不用外部图床")

    p.add_argument("--keep-site-css", action="store_true",
                   help="阅读模式下保留原站 CSS（默认丢弃，只用内置排版）")
    p.add_argument("--no-banner", action="store_true", help="完整快照模式不显示底部来源条")
    p.add_argument("--verify-tls", action="store_true",
                   help="严格校验 HTTPS 证书（默认不校验，兼容证书链不全的站点）")
    p.add_argument("-q", "--quiet", action="store_true", help="安静模式")
    p.add_argument("-V", "--version", action="version", version="WebVault %s" % __version__)
    return p


def _read_urls_from_file(path: str) -> list:
    urls = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 支持 "标题 | URL" 这种写法，取 URL 部分
            if "|" in line:
                line = line.split("|")[-1].strip()
            urls.append(line)
    return urls


# --------------------------------------------------------------------------
# 子命令：index / discover
# --------------------------------------------------------------------------

def build_index_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="webvault index",
        description="扫描保存目录，生成可搜索的离线书库索引页。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  webvault index                       # 扫描 ./vault，产出 ./vault/index.html
  webvault index -o D:\\saves           # 扫描 D:\\saves
  webvault index -o D:\\saves\\index.html  # 指定输出文件
""",
    )
    p.add_argument("dir", nargs="?", default="",
                   help="要扫描的目录（默认：./vault）")
    p.add_argument("-o", "--out", default="",
                   help="输出位置：可以是目录，也可以是具体的 .html 文件")
    p.add_argument("-t", "--title", default="WebVault 书库", help="书库标题")
    p.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    p.add_argument("-q", "--quiet", action="store_true")
    return p


def build_discover_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="webvault discover",
        description="从一个页面里找出值得归档的文章链接。\n"
                    "默认只列出来；加 --save 可以直接批量保存。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  webvault discover https://site.com/blog
  webvault discover https://site.com/blog --any-host -n 30
  webvault discover https://site.com/blog --save -o D:\\saves --workers 5
  webvault discover https://site.com/blog --list urls.txt
""",
    )
    p.add_argument("url", help="从这里发现链接")
    p.add_argument("-n", "--limit", type=int, default=50, help="最多返回多少条（默认 50）")
    p.add_argument("--any-host", action="store_true", help="允许跨域链接（默认只留同域）")
    p.add_argument("--min-score", type=float, default=8.0, help="最低分数阈值（默认 8）")
    p.add_argument("--min-text", type=int, default=6, help="链接文字最短长度（默认 6）")
    p.add_argument("--timeout", type=float, default=25.0)
    p.add_argument("--save", action="store_true", help="发现后直接批量保存")
    p.add_argument("-o", "--out", default="", help="保存目录（配合 --save，默认 ./vault）")
    p.add_argument("-m", "--mode", default="reader", choices=("reader", "full", "data"))
    p.add_argument("--workers", type=int, default=4, help="保存时的并发数")
    p.add_argument("--list", dest="listfile", default="",
                   help="把发现的链接写入文件，便于后续用 -i 复用")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.add_argument("-q", "--quiet", action="store_true")
    return p


def cmd_index(argv) -> int:
    args = build_index_parser().parse_args(argv)
    from src.library import build_index

    # 扫描目录：位置参数优先，否则用 -o（当 -o 是目录时）
    target = args.dir or ""
    if not target and args.out:
        if os.path.isdir(args.out) or args.out.endswith(("/", "\\")):
            target = args.out
    target = target or os.path.join(os.getcwd(), "vault")

    if not os.path.isdir(target):
        print("错误：目录不存在 %s" % target, file=sys.stderr)
        print("提示：先存几个页面，或用 -o 指定已有目录。", file=sys.stderr)
        return 2

    # 输出路径：-o 指向目录时用其作为扫描目录，索引写在里面
    out_path = ""
    if args.out and not (os.path.isdir(args.out) or args.out.endswith(("/", "\\"))):
        out_path = args.out

    path, entries = build_index(target, out_path=out_path, title=args.title,
                                recursive=not args.no_recursive)

    if not args.quiet:
        total = sum(e.size for e in entries)
        print("书库索引已生成")
        print("  扫描   %s" % target)
        print("  收录   %d 项（%s）" % (len(entries), human_size(total)))
        hosts = {e.host for e in entries if e.host}
        if hosts:
            print("  来源   %d 个站点" % len(hosts))
        print("  文件   %s" % path)
        if not entries:
            print()
            print("  目录里还没有 WebVault 产物。先用 webvault.py <网址> 存几个页面。")
    return 0


def cmd_discover(argv) -> int:
    args = build_discover_parser().parse_args(argv)
    import json as _json

    from src.discover import discover
    from src.fetch import fetch

    verbose = not args.quiet

    if verbose:
        print("发现链接：%s" % args.url, file=sys.stderr)
    fr = fetch(args.url, timeout=args.timeout, retries=2)
    if not fr.body:
        print("错误：抓取失败 %s" % (fr.error or ""), file=sys.stderr)
        return 1
    if verbose and fr.error:
        print("警告：%s（仍尝试解析返回内容）" % fr.error, file=sys.stderr)

    links = discover(
        fr.text, fr.final_url or args.url,
        same_host_only=not args.any_host,
        min_text=args.min_text, min_score=args.min_score, limit=max(1, args.limit),
    )

    if not links:
        print("没有发现符合条件的链接。", file=sys.stderr)
        print("提示：可以降低门槛试试 --min-score 5，或加 --any-host 允许跨域。",
              file=sys.stderr)
        return 1

    if args.listfile:
        try:
            parent = os.path.dirname(os.path.abspath(args.listfile))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(args.listfile, "w", encoding="utf-8") as f:
                f.write("# 由 WebVault discover 生成，来源 %s\n" % (fr.final_url or args.url))
                for lk in links:
                    f.write("%s | %s\n" % (lk.text.replace("|", "/"), lk.url))
            if verbose:
                print("已写入 %d 条到 %s" % (len(links), args.listfile), file=sys.stderr)
        except Exception as e:
            print("写入列表文件失败：%s" % e, file=sys.stderr)

    if args.json:
        sys.stdout.write(_json.dumps([l.to_dict() for l in links],
                                     ensure_ascii=False, indent=2))
    elif not args.save:
        for i, lk in enumerate(links, 1):
            print("%3d. [%5.1f] %s" % (i, lk.score, lk.text[:70]))
            print("        %s" % lk.url)

    if args.save:
        out_dir = args.out or os.path.join(os.getcwd(), "vault")
        if verbose:
            print("开始保存 %d 个页面 → %s" % (len(links), out_dir), file=sys.stderr)
        opts = InlineOptions(scripts="drop" if args.mode == "reader" else "keep")
        items = vault_many([l.url for l in links], out_dir=out_dir, mode=args.mode,
                           workers=max(1, args.workers), inline_options=opts,
                           timeout=args.timeout,
                           progress=(lambda m: print("  " + m, file=sys.stderr)) if verbose else None)
        ok = sum(1 for i in items if i.ok)
        print("完成：成功 %d / 失败 %d" % (ok, len(items) - ok), file=sys.stderr)
        if verbose:
            print("已保存到 %s" % out_dir, file=sys.stderr)
        return 0 if ok else 1

    return 0


def build_diff_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="webvault diff",
        description="重新抓取一个网址，和上次保存的版本比较，看内容改了什么。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  webvault diff https://example.com/announce -o D:\\saves
  webvault diff https://example.com/announce -o D:\\saves --context 1
  webvault diff --list -o D:\\saves        # 看看都追踪了哪些页面

说明:
  每次保存都会把正文记一份到 <保存目录>/.webvault/ 下。
  diff 会抓最新的正文，和最近一版对比，按行给出 + / - 差异。
  只比对纯文本，不看 HTML —— 否则时间戳、广告位会让每次都"看起来变了"。
""",
    )
    p.add_argument("url", nargs="?", default="", help="要检查的网址")
    p.add_argument("-o", "--out", default="", help="保存目录（默认 ./vault）")
    p.add_argument("--context", type=int, default=2, help="差异块之间保留的分隔（默认 2）")
    p.add_argument("--max-lines", type=int, default=400, help="最多输出多少行差异")
    p.add_argument("--list", dest="list_pages", action="store_true",
                   help="列出所有已追踪的页面，不抓取")
    p.add_argument("--timeout", type=float, default=25.0)
    p.add_argument("--save", action="store_true",
                   help="有变化时顺便把新版本也存下来")
    p.add_argument("--verify-tls", action="store_true", help="严格校验 HTTPS 证书")
    p.add_argument("-q", "--quiet", action="store_true")
    return p


def cmd_diff(argv) -> int:
    args = build_diff_parser().parse_args(argv)
    from src import watch

    out_dir = args.out or os.path.join(os.getcwd(), "vault")
    verbose = not args.quiet

    if args.list_pages:
        pages = watch.known_pages(out_dir)
        if not pages:
            print("还没有追踪任何页面。")
            print("先用 webvault.py <网址> -o %s 存一次，就会开始记录。" % out_dir)
            return 0
        print("已追踪 %d 个页面（%s）" % (len(pages), out_dir))
        print()
        for pg in pages:
            print("  %s" % (pg["title"] or "(无标题)")[:60])
            print("     %s" % pg["url"][:100])
            print("     %d 个版本 · 最近 %s" % (pg["versions"], pg["last_seen"]))
        return 0

    if not args.url:
        build_diff_parser().print_help()
        return 2

    # 先在有历史的前提下抓一次，再比对
    old_entry = watch.history_for(out_dir, args.url)
    old_versions = (old_entry.get("versions") or [])
    new_text = ""
    if verbose:
        print("抓取 %s …" % args.url, file=sys.stderr)

    r = vault(args.url, mode="reader", out_dir=out_dir, timeout=args.timeout,
              write=False, verify_tls=args.verify_tls, track_changes=False)
    if not r.ok:
        print("抓取失败：%s" % r.error, file=sys.stderr)
        return 1
    if r.challenge:
        print("⚠ 抓到的是「%s」拦截页，比对结果没有意义。" % r.challenge, file=sys.stderr)
        return 1
    new_text = ""
    from src.extract import block_text
    if r.extraction:
        # 必须和记录时用同一套文本（按块的），否则每次都会"看起来变了"
        new_text = block_text(r.extraction.root) or r.extraction.text
    if not new_text:
        new_text = str((r.data or {}).get("text") or "")
    new_text = new_text.strip()
    if not new_text:
        print("没提取到正文，无法比较。", file=sys.stderr)
        return 1

    if not old_versions:
        print("这是第一次检查这个页面，已记录为基线。")
        if args.save:
            vault(args.url, mode="reader", out_dir=out_dir, timeout=args.timeout,
                  verify_tls=args.verify_tls)
        else:
            watch.record(out_dir, args.url, r.title, new_text, "")
        return 0

    info = watch.compare(out_dir, args.url, new_text)
    status = info.get("status")
    stats = info.get("stats") or {}

    if status == "same":
        print("没有变化。")
        print("  最近记录：%s（第 %d 版）" % (info.get("since", ""), len(old_versions)))
    elif status == "changed":
        print("内容有变化（对比 %s 那一版）：" % info.get("since", ""))
        print("  新增 %d 行，删除 %d 行（旧 %d 行 → 新 %d 行）" % (
            stats.get("added", 0), stats.get("removed", 0),
            stats.get("old_lines", 0), stats.get("new_lines", 0)))
        print()
        diff, _ = watch.diff_texts(
            watch.read_version_text(out_dir, old_versions[-1].get("hash", "")),
            new_text, context=args.context, max_lines=args.max_lines)
        for line in diff:
            print(line)
    else:
        print(info.get("message", "无法比较。"))

    # 更新基线（除非用户只是想看看）
    if args.save:
        vault(args.url, mode="reader", out_dir=out_dir, timeout=args.timeout,
              verify_tls=args.verify_tls)
        if verbose:
            print()
            print("新版本已保存到 %s" % out_dir)
    else:
        watch.record(out_dir, args.url, r.title, new_text, "")
    return 0


def main(argv=None) -> int:
    _setup_console()
    argv = list(sys.argv[1:] if argv is None else argv)

    # 子命令分发：只有第一个词是已知子命令时才走子命令，
    # 否则保持原来的"位置参数就是 URL"的行为不变。
    if argv:
        first = argv[0].lower()
        if first == "index":
            return cmd_index(argv[1:])
        if first == "discover":
            return cmd_discover(argv[1:])
        if first == "diff":
            return cmd_diff(argv[1:])

    args = build_parser().parse_args(argv)

    urls = list(args.url)
    if args.input:
        if not os.path.isfile(args.input):
            print("错误：找不到 URL 列表文件 %s" % args.input, file=sys.stderr)
            return 2
        urls.extend(_read_urls_from_file(args.input))

    if not urls:
        # 没给网址不算"失败"，属于用法问题：和 diff 子命令空参数保持一致，返回 2
        print(BANNER)
        print()
        build_parser().print_help()
        return 2

    out_dir = args.out or os.path.join(os.getcwd(), "vault")

    inline_opts = InlineOptions(
        images=not args.no_images,
        css=not args.no_css,
        fonts=not args.no_fonts,
        scripts=args.scripts,
        max_total_bytes=max(1, args.max_total) * 1024 * 1024,
        max_asset_bytes=max(1, args.max_asset) * 1024 * 1024,
        compress_images=not args.no_compress,
        image_max_width=max(200, args.max_width),
        allow_external=not args.local_only,
        verify_tls=args.verify_tls,
    )

    verbose = not args.quiet

    # ---------- 单 URL ----------
    if len(urls) == 1:
        if verbose and not args.stdout:
            print(BANNER)
            print()

        def prog(msg: str) -> None:
            if verbose and not args.stdout:
                print("  · %s" % msg, flush=True)

        t0 = time.time()
        r = vault(
            urls[0],
            mode=args.mode,
            inline_options=inline_opts,
            out_dir=out_dir,
            filename=args.name,
            progress=prog,
            timeout=args.timeout,
            write=not args.stdout,
            keep_site_css=args.keep_site_css,
            show_banner=not args.no_banner,
            verify_tls=args.verify_tls,
        )

        if args.stdout:
            if args.mode == "data":
                import json
                sys.stdout.write(json.dumps(r.data or {}, ensure_ascii=False, indent=2))
            else:
                sys.stdout.write(r.html)
            return 0 if r.ok else 1

        print()
        if r.ok:
            print("✔ 完成")
            print("  标题   %s" % (r.title or "(无)"))
            print("  模式   %s" % r.mode)
            print("  大小   %s" % r.size_h())
            if r.extraction:
                print("  正文   %d 字，%d 图" % (r.extraction.word_count,
                                                r.extraction.image_count))
            if r.stats:
                print("  资源   %s" % r.stats.summary())
            print("  耗时   %.1fs" % r.elapsed)
            print("  文件   %s" % r.out_path)
            if r.challenge:
                print()
                print("  ⚠ 注意：这个页面看起来是「%s」。" % r.challenge)
                print("    %s" % r.challenge_advice)
                print("    保存下来的多半是拦截页而不是正文，建议用 -m full 保留原样，")
                print("    或加上正确的 Cookie / 换用可访问的网络环境。")
            if r.http_warning:
                print()
                print("  ⚠ %s。" % r.http_warning)
                print("    存下来的是服务器的错误页，不是你要的正文。")
                print("    如果这个网址本来就该失效，那没问题；否则请检查地址是否写错。")
            if r.thin_content:
                print()
                print("  ⚠ %s" % r.thin_advice)
                print("    这类页面得用浏览器渲染才拿得到内容，WebVault 不执行 JavaScript。")
                print("    可以试试 -m full 留个原样快照，或者用浏览器手动另存。")
            if r.stats and r.stats.skipped_urls and verbose:
                print("  跳过   %d 个资源（前几个）：" % len(r.stats.skipped_urls))
                for u in r.stats.skipped_urls[:5]:
                    print("         %s" % u[:100])
            return 0
        else:
            print("✘ 失败：%s" % r.error, file=sys.stderr)
            if r.fetch and r.fetch.status:
                print("  HTTP %s" % r.fetch.status, file=sys.stderr)
            return 1

    # ---------- 批量 ----------
    if args.stdout:
        print("错误：--stdout 只支持单个 URL（批量时请去掉 --stdout）", file=sys.stderr)
        return 2

    if args.name:
        print("提示：批量模式下 -n/--name 会被忽略（文件名由各页标题决定）",
              file=sys.stderr)

    if verbose:
        print(BANNER)
        print()
        print("批量保存 %d 个 URL → %s" % (len(urls), out_dir))
        print()

    def bprog(msg: str) -> None:
        if verbose:
            print("  %s" % msg, flush=True)

    items = vault_many(
        urls, out_dir=out_dir, mode=args.mode, workers=args.workers,
        progress=bprog, inline_options=inline_opts, timeout=args.timeout,
        keep_site_css=args.keep_site_css, show_banner=not args.no_banner,
    )

    ok = sum(1 for i in items if i.ok)
    fail = len(items) - ok
    total = sum(i.size for i in items)
    print()
    print("完成：成功 %d / 失败 %d，共 %s" % (ok, fail, human_size(total)))
    if fail:
        print("失败的：")
        for i in items:
            if not i.ok:
                print("  %s  →  %s" % (i.url[:70], i.error[:70]))
    if ok and verbose:
        print("已保存到 %s" % out_dir)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
