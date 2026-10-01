"""Safe minimal markdown -> HTML. Every piece of text is HTML-escaped; only tags emitted here exist.

Supported: headings, ul/ol lists, fenced code, paragraphs, **bold**, `code`, [text](url), [[wikilink|label]].
Links: http/https or relative only. Anything else (javascript:, data:, vbscript:, control-char tricks)
is rendered as plain text without a link.
"""
from __future__ import annotations

import html
import re
from urllib.parse import quote

_MAX_CHARS = 500_000
_MAX_LINE = 20_000
_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*:")
_FORBIDDEN = re.compile(r"[\x00-\x20\x7f-\x9f\\<>\"'`]")
_INLINE = re.compile(
    r"`([^`\n]+)`"
    r"|\[\[([^\]\n|]+)(?:\|([^\]\n]*))?\]\]"
    r"|\[([^\]\n]*)\]\(([^()\s]*)\)"
    r"|\*\*([^*\n]+)\*\*"
)
_HEADING = re.compile(r"(#{1,6})[ \t]+(.*)")
_UL = re.compile(r"[-*+][ \t]+(.*)")
_OL = re.compile(r"\d{1,9}[.)][ \t]+(.*)")


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def safe_url(url: str) -> str | None:
    """Return the URL if it is http(s) or a plain relative reference, else None."""
    if not url or _FORBIDDEN.search(url):
        return None
    if _SCHEME.match(url):
        return url if url.split(":", 1)[0].lower() in ("http", "https") and len(url) > 8 else None
    if url.startswith("//"):
        return None
    return url


def wikilink_href(target: str) -> str:
    t = target.strip()
    if not t.endswith(".md"):
        t += ".md"
    return "#/article/" + quote(t, safe="/")


def _inline(text: str, allow_bold: bool = True) -> str:
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        out.append(esc(text[pos:m.start()]))
        pos = m.end()
        code, wiki, wlabel, ltext, lurl, bold = m.groups()
        if code is not None:
            out.append(f"<code>{esc(code)}</code>")
        elif wiki is not None:
            label = wlabel.strip() if wlabel and wlabel.strip() else wiki.strip()
            out.append(f'<a href="{esc(wikilink_href(wiki))}">{esc(label)}</a>')
        elif ltext is not None:
            u = safe_url(lurl)
            if u is None:
                out.append(esc(ltext))
            else:
                rel = ' rel="noopener noreferrer nofollow"' if _SCHEME.match(u) else ""
                out.append(f'<a href="{esc(u)}"{rel}>{esc(ltext)}</a>')
        elif allow_bold:
            out.append(f"<strong>{esc(bold)}</strong>")
        else:
            out.append(esc(m.group(0)))
    out.append(esc(text[pos:]))
    return "".join(out)


def render_markdown(text: str) -> str:
    lines = text[:_MAX_CHARS].replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    code: list[str] | None = None
    lst: str | None = None

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()

    def close_list():
        nonlocal lst
        if lst:
            out.append(f"</{lst}>")
            lst = None

    def end_code():
        nonlocal code
        out.append("<pre><code>" + esc("\n".join(code or [])) + "</code></pre>")
        code = None

    for raw in lines:
        line = raw[:_MAX_LINE]
        s = line.strip()
        if code is not None:
            end_code() if s.startswith("```") else code.append(line)
            continue
        if s.startswith("```"):
            flush_para(), close_list()
            code = []
            continue
        if not s:
            flush_para(), close_list()
            continue
        if m := _HEADING.fullmatch(s):
            flush_para(), close_list()
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2).strip())}</h{n}>")
            continue
        m_ul, m_ol = _UL.fullmatch(s), _OL.fullmatch(s)
        if m_ul or m_ol:
            flush_para()
            kind = "ul" if m_ul else "ol"
            if lst != kind:
                close_list()
                out.append(f"<{kind}>")
                lst = kind
            out.append(f"<li>{_inline((m_ul or m_ol).group(1).strip())}</li>")
            continue
        close_list()
        para.append(s)
    if code is not None:
        end_code()
    flush_para(), close_list()
    return "\n".join(out)
