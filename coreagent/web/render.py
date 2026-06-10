"""服务端 Markdown 渲染（#0024 T1）：Markdown 文本 → **净化后的 HTML**（含代码高亮）。

承接 #0021 的 ``web/sse.py`` 风格——纯函数、无 I/O、HTTP 库只活在 app 层。本模块把 assistant
文本块的 Markdown 渲染为可直接注入页面的 HTML：标题 / 粗体 / 列表 / 链接走标准 Markdown，fenced
code 走 pygments 语法高亮（容器 class = ``codehilite``）。

**净化策略 = 标签白名单**（防 XSS，spec 非功能要求）：渲染产出的 HTML 再过一遍白名单清洗——
- 白名单外的标签**丢弃其标签标记**（内容保留为转义文本）；``script`` / ``style`` 连**内容一并丢弃**；
- 每标签按属性白名单过滤，**所有** ``on*`` 事件属性一律剥离；
- ``href`` / ``src`` 仅放行 http(s)/mailto 与相对/锚点 URL，``javascript:`` / ``data:`` 等剥离
  （URL 先去控制字符再判协议，挡 ``java&#9;script:`` 之类绕过）。
故输出 HTML 绝不含可执行 ``<script>`` 或事件属性、绝不含危险 URL。

依赖：``markdown`` + ``pygments``（随 ``[web]`` 可选 extra 安装，不进核心依赖）；清洗用标准库
``html`` / ``html.parser`` / ``re``。本模块**绝不** import 任何 HTTP / Web 框架库（守单向依赖：
网络库只活在 app 层）；纯函数、不触盘、不发网。
"""

import html as _html
import re
from html.parser import HTMLParser

import markdown as _markdown
from markdown.extensions.codehilite import CodeHiliteExtension
from markdown.extensions.fenced_code import FencedCodeExtension

# 代码块高亮容器 class（pygments codehilite）；前端 CSS 须对此 class 配色
# （见 coreagent/web/frontend/src/styles/codehilite.css，#0026 起取代旧内联 INDEX_HTML 配色）。
HIGHLIGHT_CLASS = "codehilite"

# ── 标签 / 属性 / URL 白名单 ────────────────────────────────────────────────────────
# 标签白名单：Markdown 正常产出的结构标签 + 高亮容器（div/pre/code/span）。
_ALLOWED_TAGS: frozenset[str] = frozenset({
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "b", "i", "del", "ins", "sub", "sup", "blockquote",
    "ul", "ol", "li", "dl", "dt", "dd",
    "code", "pre", "span", "div",
    "a", "img",
    "table", "thead", "tbody", "tr", "th", "td",
})
# 每标签允许的属性（其余一律剥离，含所有 on* 事件属性 / style）；class 仅用于高亮 token 着色。
_ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title"}),
    "span": frozenset({"class"}),
    "div": frozenset({"class"}),
    "code": frozenset({"class"}),
    "pre": frozenset({"class"}),
    "th": frozenset({"align"}),
    "td": frozenset({"align"}),
}
# URL 协议白名单（href/src）；其余协议（javascript/data/…）一律剥离该属性。
_SAFE_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})
# 连内容一并丢弃的危险容器标签。
_DROP_CONTENT_TAGS: frozenset[str] = frozenset({"script", "style"})
# 空元素（不产闭合标签）。
_VOID_TAGS: frozenset[str] = frozenset({"br", "hr", "img"})

_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.IGNORECASE)
_URL_CTRL_RE = re.compile(r"[\x00-\x20]")  # 控制字符 + 空白：判协议前先剔除（仿浏览器）


def _is_safe_url(value: str | None) -> bool:
    """URL 是否安全：无协议（相对/锚点）放行；有协议须在白名单内。

    判协议前先剔除控制字符与空白（浏览器会先做同样剔除），挡 ``java\\tscript:`` 之类绕过。
    """
    if not value:
        return True
    stripped = _URL_CTRL_RE.sub("", value)
    m = _SCHEME_RE.match(stripped)
    if m is None:
        return True  # 相对路径 / 锚点 / 片段 —— 无协议，安全
    return m.group(1).lower() in _SAFE_URL_SCHEMES


class _WhitelistSanitizer(HTMLParser):
    """白名单 HTML 清洗器：解析 → 仅重发白名单内标签/属性，其余丢弃、内容转义。

    ``convert_charrefs=False``：保留 pygments 产出的实体（``&quot;`` / ``&lt;`` 等）原样回写，
    避免二次转义破坏代码块显示。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._skip_depth = 0  # >0 表示正处于被丢弃内容的危险容器（script/style）内

    def result(self) -> str:
        return "".join(self._out)

    def _emit_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed = _ALLOWED_ATTRS.get(tag, frozenset())
        parts: list[str] = []
        for raw_key, value in attrs:
            key = raw_key.lower()
            if key not in allowed:
                continue  # 不在白名单（含所有 on* 事件属性 / style）→ 剥离
            if key in ("href", "src") and not _is_safe_url(value):
                continue  # 危险 URL（javascript: / data: 等）→ 剥离该属性
            parts.append(f' {key}="{_html.escape(value or "", quote=True)}"')
        return "".join(parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip_depth:
            return
        if tag in _DROP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if tag not in _ALLOWED_TAGS:
            return  # 丢弃标签标记，内容仍经 handle_data 转义保留
        astr = self._emit_attrs(tag, attrs)
        if tag in _VOID_TAGS:
            self._out.append(f"<{tag}{astr} />")
        else:
            self._out.append(f"<{tag}{astr}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip_depth or tag in _DROP_CONTENT_TAGS or tag not in _ALLOWED_TAGS:
            return
        astr = self._emit_attrs(tag, attrs)
        self._out.append(f"<{tag}{astr} />")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth or tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        self._out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._out.append(_html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if self._skip_depth:
            return
        self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._skip_depth:
            return
        self._out.append(f"&#{name};")


def sanitize_html(html_text: str) -> str:
    """对 HTML 串做白名单清洗：剥离危险标签 / 属性 / URL，返回安全 HTML。纯函数。"""
    sanitizer = _WhitelistSanitizer()
    sanitizer.feed(html_text)
    sanitizer.close()
    return sanitizer.result()


def render_markdown(text: str) -> str:
    """Markdown 文本 → 净化后的 HTML（含代码高亮）。纯函数、可被 ``to_thread`` 安全并发调用。

    每次构造全新 ``Markdown`` 实例（不复用有状态实例），fenced code 走 pygments（容器 class =
    ``codehilite``）；产出 HTML 再过白名单清洗（``sanitize_html``）。空串原样返回空串。
    """
    if not text:
        return ""
    raw = _markdown.markdown(
        text,
        extensions=[
            FencedCodeExtension(),
            CodeHiliteExtension(guess_lang=False, css_class=HIGHLIGHT_CLASS),
        ],
    )
    return sanitize_html(raw)


def render_assistant_messages(messages: list[dict]) -> list[dict]:
    """为每条 assistant 消息的文本块附渲染 HTML（字段 ``html``），**原始文本保留**（供前端复制）。

    返回浅拷贝、**不改存储结构语义**（仅旁加 ``html`` 字段）：
    - content 为字符串 → 消息体加 ``html`` = 渲染结果（原 ``content`` 字符串不动）；
    - content 为块列表 → 每个 ``text`` 块加 ``html`` = 渲染结果（thinking / tool_use / 服务端工具块不变）。
    其余角色（user / 承载 tool_result 的 user）消息原样返回。两模式共用：code 的 assistant 文本同样富渲染。
    """
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            out.append(m)
            continue
        m2 = dict(m)
        content = m2.get("content")
        if isinstance(content, str):
            m2["html"] = render_markdown(content)
        elif isinstance(content, list):
            new_blocks: list = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    nb = dict(b)
                    nb["html"] = render_markdown(b.get("text", ""))
                    new_blocks.append(nb)
                else:
                    new_blocks.append(b)
            m2["content"] = new_blocks
        out.append(m2)
    return out
