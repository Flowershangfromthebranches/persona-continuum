"""Safe markdown renderer contract for protocol chat message bodies.

Mirrors tests/unit/test_room_protocol_ui_state.py: the renderer is a UMD
pure module (safe_markdown.js) evaluated through a node subprocess so the
browser bundle and the tests exercise the exact same code.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

SAFE_MARKDOWN = (
    Path(__file__).parents[2]
    / "src"
    / "persona_continuum"
    / "web"
    / "static"
    / "safe_markdown.js"
)

# Every "<" in renderer output must open one of these whitelist tags; any
# other "<" would be unescaped content leaking into the DOM.
ALLOWED_TAG_PATTERN = r"</?(?:a|h3|h4|p|ul|ol|li|code|strong|em)\b[^>]*/?>"


def _evaluate(expression: str) -> object:
    script = (
        f"const md = require({json.dumps(str(SAFE_MARKDOWN))});"
        f"process.stdout.write(JSON.stringify({expression}));"
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _render(content: str) -> str:
    return _evaluate(f"md.renderSafeMessageContent({json.dumps(content)})")


def _strip_allowed_tags(html: str) -> str:
    return re.sub(ALLOWED_TAG_PATTERN, "", html)


def test_whitelist_renders_heading_bold_and_list() -> None:
    html = _render("## 标题\n**重点**\n- 条目")
    assert "<h3>标题</h3>" in html
    assert "<strong>重点</strong>" in html
    assert "<ul><li>条目</li></ul>" in html
    # Markdown markers must never survive as literal text.
    assert "##" not in html
    assert "**" not in html
    # No unescaped "<" outside of whitelist tags.
    assert "<" not in _strip_allowed_tags(html)


def test_script_and_img_injection_are_escaped() -> None:
    script_html = _render("<script>alert(1)</script>")
    assert "<script" not in script_html.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in script_html
    assert "alert(1)" in script_html  # text stays visible, markup does not

    img_html = _render('<img src=x onerror="alert(1)">')
    assert "<img" not in img_html.lower()
    assert "&lt;img" in img_html
    assert "<" not in _strip_allowed_tags(img_html)


def test_heading_levels_map_to_compact_tags() -> None:
    assert "<h3>大标题</h3>" in _render("# 大标题")
    assert "<h3>中标题</h3>" in _render("## 中标题")
    assert "<h4>小标题</h4>" in _render("### 小标题")
    # Four hashes are outside the whitelist: render as literal text.
    assert "<h5>" not in _render("#### 不支持")
    assert "#### 不支持" in _render("#### 不支持")


def test_inline_code_is_rendered_and_marker_removed() -> None:
    html = _render("运行 `npm install` 命令")
    assert "<code>npm install</code>" in html
    assert "`" not in html
    # Emphasis inside code spans must stay literal.
    code_html = _render("保持 `**原样**` 输出")
    assert "<code>**原样**</code>" in code_html


def test_ordered_list_and_paragraph_cases() -> None:
    ordered = _render("1. 第一步\n2. 第二步")
    assert "<ol><li>第一步</li><li>第二步</li></ol>" in ordered

    paragraphs = _render("第一段\n\n第二段")
    assert paragraphs == "<p>第一段</p><p>第二段</p>"

    mixed = _render("开头\n- 甲\n- 乙\n结尾")
    assert mixed.startswith("<p>开头</p>")
    assert "<ul><li>甲</li><li>乙</li></ul>" in mixed
    assert mixed.endswith("<p>结尾</p>")


def test_non_string_and_empty_input_is_safe() -> None:
    assert _evaluate("md.renderSafeMessageContent(null)") == ""
    assert _evaluate("md.renderSafeMessageContent(undefined)") == ""
    assert _evaluate("md.renderSafeMessageContent('')") == ""
    assert _evaluate("md.renderSafeMessageContent('   ')") == ""
    assert _evaluate("md.renderSafeMessageContent({})") == ""
    assert _evaluate("md.renderSafeMessageContent(['x'])") == ""
    assert _evaluate("md.renderSafeMessageContent(42)") == "<p>42</p>"


def test_links_render_only_for_http_schemes() -> None:
    ok = _render("[x](https://a.b)")
    assert '<a href="https://a.b" rel="noopener noreferrer" target="_blank">x</a>' in ok
    assert "<" not in _strip_allowed_tags(ok)

    labelled = _render("[文档](https://a.b/doc?x=1&y=2)")
    assert 'href="https://a.b/doc?x=1&amp;y=2"' in labelled
    assert ">文档</a>" in labelled

    js = _render("[x](javascript:alert(1))")
    assert "<a " not in js
    assert "javascript:alert(1)" in js  # non-http scheme stays escaped literal text
    assert "<" not in _strip_allowed_tags(js)

    html_url = _render('[x](https://a.b/"onmouseover="alert(1))')
    assert '<a href="https://a.b/&quot;onmouseover=&quot;alert(1"' in html_url
    assert "<" not in _strip_allowed_tags(html_url)

    # Links compose with emphasis and stay literal inside inline code.
    mixed = _render("**[x](https://a.b)**")
    assert "<strong><a" in mixed and "</a></strong>" in mixed
    code = _render("看 `[x](https://a.b)` 原文")
    assert "<code>[x](https://a.b)</code>" in code


def test_hostile_content_never_produces_raw_angle_brackets() -> None:
    nasty = "## <b onmouseover=x>x</b>\n- <i onclick='y()'>y</i>\n`<script>`\n$& $` $'"
    html = _render(nasty)
    assert "<script" not in html.lower()
    assert "<b" not in html.lower()
    assert "<i>" not in html.lower()
    assert "<" not in _strip_allowed_tags(html)
