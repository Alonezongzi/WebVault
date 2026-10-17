<div align="center">

# WebVault · 网页金库

**把一个网址，变成一个文件。**

零依赖、可离线、双击就开的网页存档工具。

[![Python](https://img.shields.io/badge/python-3.8%2B-3776ab?style=flat-square&logo=python&logoColor=white)](#环境要求)
[![Dependencies](https://img.shields.io/badge/dependencies-0-3ddc97?style=flat-square)](#环境要求)
[![Tests](https://img.shields.io/badge/tests-340%20passing-3ddc97?style=flat-square)](#自测)
[![License](https://img.shields.io/badge/license-MIT-4c6fff?style=flat-square)](#许可)

</div>

---

## 这是什么

WebVault 把任意网页保存成**一个自包含的 `.html` 文件**。

图片、CSS、字体全部转成 data URI 内嵌进去，产物不依赖任何外部资源 —— 断网能开，丢 U 盘能开，发微信能开，十年后大概率还能开。

普通"网页另存为"会给你一个文件夹加一堆 `_files`，一移动就散架；外链的图床一挂，页面就烂了。WebVault 给的是一个文件。

```bash
python webvault.py https://example.com
```

```
✔ 完成
  标题   Example Domain
  模式   reader
  大小   4.1 KB
  正文   93 字，0 图
  文件   .\vault\Example-Domain.html
```

---

## 它解决什么问题

| 常见做法 | 问题 | WebVault 的做法 |
|---|---|---|
| 浏览器"另存为" | 一堆文件，移动就散 | 单文件，内联一切 |
| 复制粘贴到笔记 | 丢排版、丢图片 | 保留结构与图片 |
| 存 PDF | 丢链接、不可检索 | 保留 HTML 结构 |
| 丢进稍后读服务 | 服务倒闭就没了 | 文件在你自己硬盘上 |
| 截图 | 不可选中、不可检索 | 真 HTML，可搜索可复制 |
| 自己写爬虫 | 只拿到 HTML，没有内联资源 | 抓到的是**能看的页面** |

---

## 五种用法

| 命令 | 做什么 |
|---|---|
| `webvault.py <网址>` | 保存一个页面 |
| `webvault.py discover <列表页>` | 从一个页面里**找出**所有文章链接 |
| `webvault.py index` | 把存下来的页面变成**可搜索的书库** |
| `webvault.py diff <网址>` | 和上次存的比，看这页**改了什么** |
| `webvault-ui.py` | 浏览器里操作 |

---

## 三种保存模式

| 模式 | 产出 | 用途 |
|---|---|---|
| `reader` | 干净排版的正文页 | 阅读、分享、存档文章（**默认**） |
| `full` | 保留原站结构与样式的整页快照 | 原样存档、留证 |
| `data` | 结构化 JSON | 喂给爬虫流程、喂给 LLM |

### reader —— 只留正文

自动识别文章主体，去掉导航、侧边栏、广告位、推荐流、评论区、页脚，套一层自适应深浅色的排版。

```bash
python webvault.py https://www.ruanyifeng.com/blog/2026/10/weekly-issue-415.html
```

实测：原文的 47 张图全部内联，产物 1.7 MB，**0 个外部资源**。

### full —— 完整快照

保留整页结构，只做资源内联，底部加一条来源信息条（可关）。

```bash
python webvault.py https://example.com -m full
```

### data —— 结构化提取

不产出页面，产出一个 JSON：

```json
{
  "title": "文章标题",
  "byline": "作者",
  "text": "纯文本正文…",
  "html": "<article>…</article>",
  "images": [{"src": "data:image/png;base64,…", "alt": "配图"}],
  "links":  [{"href": "https://…", "text": "链接文字"}],
  "tables": [[["列一", "列二"], ["甲", "乙"]]],
  "headings": [{"level": 1, "text": "文章标题"}],
  "word_count": 12719,
  "confidence": 1.0
}
```

```bash
python webvault.py https://example.com -m data --stdout | jq .title
```

---

## 找出要存的东西

不用一个个复制链接。给一个列表页，它把文章挑出来：

```bash
python webvault.py discover https://www.ruanyifeng.com/blog/ -n 10
```

```
  1. [ 28.0] 科技爱好者周刊（第 415 期）：3D 版《看不见的城市
        https://www.ruanyifeng.com/blog/2026/10/weekly-issue-415.html
  2. [ 28.0] 科技爱好者周刊（第 414 期）：Jev 决策模型有什么用
        https://www.ruanyifeng.com/blog/2026/10/weekly-issue-414.html
  3. [ 19.0] 更多文章……
        https://www.ruanyifeng.com/blog/archives.html
```

它会算一个分数给你看：路径里有日期或文章 slug、链接文字够长、是同域 —— 分就高。
导航、登录、标签页、`.pdf`/`.jpg` 这些会被直接扔掉。

可以直接一条龙存下来：

```bash
# 发现 → 保存 → 建书库
python webvault.py discover https://www.ruanyifeng.com/blog/ --save -o D:\saves --workers 5
python webvault.py index D:\saves
```

也可以先导成清单，人工挑完再存：

```bash
python webvault.py discover https://site.com/blog --list urls.txt
# 编辑 urls.txt，删掉不想要的
python webvault.py -i urls.txt -o D:\saves
```

---

## 存完能找得到

存了 200 篇然后一篇都找不到，是这类工具最常见的死法。所以有个书库：

```bash
python webvault.py index D:\saves
```

```
书库索引已生成
  扫描   D:\saves
  收录   57 项（128.4 MB）
  来源   8 个站点
  文件   D:\saves\index.html
```

生成一个 `index.html`，双击打开就能：搜索标题/网址/摘要、按来源站筛选、按时间/标题/体积排序。

**索引页自己也是零外部依赖的** —— 和它索引的那些页面一样，断网可用。

---

## 它什么时候偷偷改了

存下来只是第一步。真正难回答的是：**这个页面跟上个月比，改了什么？**

公告改了日期、价格调了两块、条款里多了一句"最终解释权"—— 你不想每次重读一遍。
所以有了 `diff`：

```bash
python webvault.py diff https://example.com/notice -o D:\saves
```

```
内容有变化（对比 2026-10-17 09:54:11 那一版）：
  新增 2 行，删除 1 行（旧 3 行 → 新 4 行）

- 本周三(10 月 15 日)晚 22:00 开始系统维护,预计持续两小时。
+ 本周四(10 月 16 日)晚 23:00 开始系统维护,预计持续三小时。
+ 本次维护会同时升级数据库,维护后首次访问可能稍慢。
```

没变的段落不会出现在输出里。这次没变就一句话：

```
没有变化。
  最近记录：2026-10-17 09:54:11（第 1 版）
```

看看都在盯哪些页面：

```bash
python webvault.py diff --list -o D:\saves
```

### 为什么不用 HTML 比

因为 HTML 每次都不一样：时间戳、"相关阅读"随机排序、广告位、CSRF token。
拿 HTML 做 diff，你会得到"整页都变了"，等于没信息。

这里比的是**提取出来的正文**，而且按段落/标题/列表项切成行再比 ——
所以「第三段改了一个字」就只显示那一行，不会牵连整篇。

历史存在 `D:\saves\.webvault\` 里（一个索引 + 每个版本一份纯文本），
最多留 20 版。它是隐藏目录，书库扫描不会把它当成收藏项。

---

## 图形界面

不想用命令行的话，有个本地操作台：

```bash
python webvault-ui.py
```

自动打开 `http://127.0.0.1:8770/`，三个标签页对应三件事：

| 标签页 | 做什么 |
|---|---|
| **保存网页** | 粘贴一批网址，选模式，点保存。任务列表实时显示进度、体积、资源统计 |
| **发现链接** | 输入一个列表页，勾选想要的文章，一键丢进保存队列 |
| **我的书库** | 浏览已存页面、生成索引页 |

界面本身也是零依赖的 —— 没有 CDN、没有前端框架，断网照常工作。服务只监听 `127.0.0.1`，不对外暴露。

`--port` 可改端口，被占用时会自动往后找；`--out` 指定保存目录；`--no-browser` 不自动开浏览器。

---

## 批量保存

```bash
# 多个网址
python webvault.py url1 url2 url3 -o D:\saves --workers 6

# 从文件读
python webvault.py -i urls.txt -o D:\saves
```

`urls.txt` 每行一个，`#` 开头是注释，也支持 `标题 | URL` 写法：

```
# 技术文章
阮一峰周刊 | https://www.ruanyifeng.com/blog/2024/01/weekly-issue-286.html
https://example.com
```

---

## 环境要求

- **Python 3.8+**
- **不需要 pip install 任何东西**

纯标准库实现。`lxml` 和 `Pillow` 存在时会被自动用来提速和压缩图片，没有也完全不影响功能。

在 Windows / Linux / macOS 上都能跑。

---

## 它做得比较讲究的地方

### 中文网页的编码

大量中文老站是 GBK 却不声明编码。WebVault 按 `BOM → HTTP 头 → meta → 试解 → 中日常见编码` 逐级判定，不会给你一堆乱码。

### 正文提取算法

自己实现的 Readability 风格打分，针对中文做了调整：

- 中文没有空格分词，用**汉字数加权 + 标点密度**判断，而不是词数
- 链接密度阈值调高（中文导航常一个链接包好几个字）
- 识别「阅读全文」「点击查看」「上一篇/下一篇」这类诱导文案为噪音

`extract.py` 里的每个信号都有注释说明为什么这么定。

### 容错解析器

真实网页的 HTML 都很脏。WebVault 自带一个容错解析器，处理：

- 标签不闭合、交叉嵌套
- `<table><tr><td>1<td>2<tr>` 这种省略闭合的表格
- `class="a>` 这种引号没配对的属性
- `<script>` 里出现 `</div>` 字符串

解析失败不会崩，最差也会退回到整页。

### 体积预算

```bash
python webvault.py URL --max-total 8 --max-asset 2
```

超预算时按优先级放弃资源，并在输出里告诉你跳过了哪些、为什么。预算按 **base64 之后的体积**计算，所以产物不会比上限大。

### 被抓了会告诉你

抓到的"页面"有时候根本不是内容，而是 Cloudflare 的 `Just a moment...`、登录墙、验证码页。
这类页面往往返回 `200`，旧式的工具会给你一个空壳，你还以为是网站改版了。

WebVault 会认出来，并且直说：

```
✔ 完成
  标题   Just a moment...
  正文   24 字，0 图

  ⚠ 注意：这个页面看起来是「Cloudflare 人机验证」。
    站点启用了 Cloudflare 挑战，需要真实浏览器通过验证后才能访问
    保存下来的多半是拦截页而不是正文，建议用 -m full 保留原样，
    或加上正确的 Cookie / 换用可访问的网络环境。
```

能识别 Cloudflare 挑战/拦截、浏览器检查页、验证码、登录墙、访问拒绝、限流页。
判定只在页面**很短**时才生效 —— 长文章里出现 "captcha" 这个词不会被误伤。

### 超时是"总共等多久"

`--timeout 20` 的意思是：**这个网址我最多等你 20 秒**（含所有重试）。

旧式实现里它是个"单次连接超时"，一个连不上的地址会卡 `20 × 3 = 60` 秒，
用起来像卡死了。现在到点就放弃，并且告诉你为什么：

```
✘ 失败：连接超时：服务器在限定时间内没有响应（可能不在线、被防火墙拦截，或网址不可达）
```

域名写错、连接被拒、证书问题、网络不可达，都会给出对应的中文说明，
而不是甩一个 `URLError: <urlopen error timed out>` 给你。

### 被限流也不会留破图

并发抓几十张图时，图床经常回 `429 Too Many Requests`。

WebVault 把 429/5xx/408 当作**暂时性失败**重试，并且整个线程池共享一个退避窗口 ——
一个线程撞上 429，所有线程一起慢下来，而不是各自重试再一起撞墙。

抓不到的资源不会被静默丢掉：产物的资源统计里会写明失败几个、具体是哪些地址。

（这个功能有回归测试：测试里起了个先返回 429 两次的本地服务，验证最终仍能内联成功。）

---

## 自测

```bash
python tests/test_all.py            # 离线测试（332 项）
python tests/test_all.py --online   # 加上真实站点（340 项）
python tests/test_diff.py           # 变更检测端到端（16 项）
python tests/test_index.py          # 书库索引页与前端逻辑（20 项）
python tests/test_ui.py 8774        # 本地服务集成测试（63 项）
python tests/check_selfcontained.py # 确认 examples/ 里没有任何外部网络请求
```

跑 `test_ui.py` 之前，要在**临时目录**上起服务，别指向 `examples/` ——
这个套件会真的提交保存任务，服务端输出目录是全局的，指向样例集就会把产物写进去：

```bash
python webvault-ui.py --port 8774 --no-browser --out %TEMP%\wv-uitest
```

离线测试用内置 fixture 和一个临时起的本地 HTTP 服务，不依赖网络。覆盖解析容错、正文提取、编码嗅探、CSS 内联、预算控制、重试与限流退避、拦截页识别、超时预算、HTTP 错误状态码、产物自包含、三种模式渲染、链接发现、书库索引、变更追踪、端到端落盘。

索引页的前端逻辑用 Node 真跑一遍（搜索、筛选、排序），不是只做字符串检查。

---

## 已知限制

说在前面：

- **不执行 JavaScript**。靠 JS 渲染内容的站点（很多 SPA）抓不到正文，拿到的是空壳。这类站点需要浏览器渲染，WebVault 不做。
- **不绕过反爬**。Cloudflare 挑战、登录墙、验证码会被识别出来并明确告诉你，但不会伪造会话去突破。需要的话请自行准备 Cookie / 代理。
- **视频/音频不内联**，体积不现实，只保留地址。
- **`discover` 不是爬虫框架**。它不递归、不遵守 robots、不做任务队列持久化。它只负责"把这一页上的文章链接找出来"，递归抓取请自己写循环。
- **`reader` 模式默认丢弃原站 CSS**。想要原版样式用 `-m full`，或 `--keep-site-css` 保留。
- **TLS 默认不严格校验**。很多公开站证书链不完整，严格校验会让工具在正常网页上失败。需要安全性时加 `--verify-tls`。
- **正文提取是启发式的**，不保证 100% 准确。产物里带 `confidence`（置信度），偏低说明算法没把握，建议改用 `full` 模式。

---

## 目录结构

```
gift/
├── webvault.py          CLI 入口（保存 / discover / index / diff）
├── webvault-ui.py       本地图形界面
├── src/
│   ├── fetch.py         HTTP 抓取、编码嗅探、重试与退避、拦截页识别
│   ├── parse.py         容错 HTML 解析器 / DOM
│   ├── extract.py       正文提取与打分
│   ├── inline.py        资源内联（图片/CSS/字体 → data URI）
│   ├── discover.py      从页面里发现文章链接
│   ├── library.py       离线书库索引生成
│   ├── watch.py         变更追踪（记录版本 + 正文 diff）
│   ├── render.py        三种模式的渲染
│   ├── pipeline.py      串起整个流程
│   └── util.py          工具函数
├── tests/
│   ├── test_all.py      主测试套件
│   ├── test_ui.py       本地服务集成测试
│   ├── test_index.py    索引页与前端逻辑测试
│   ├── test_diff.py     变更检测端到端
│   └── check_selfcontained.py  自包含审计
├── docs/
│   ├── 快速上手.md
│   ├── 命令行手册.md
│   └── 设计说明.md
└── examples/            示例产物
```

---

## 许可

MIT
