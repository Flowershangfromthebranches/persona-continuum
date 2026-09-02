from pathlib import Path


def test_parallel_world_simple_navigation() -> None:
    static = Path("src/persona_continuum/web/static")
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert "继续模拟" in html
    assert "查看人物" in html
    assert "查看分支" in html
    assert "开发者工具" in html
    assert "startButton.disabled = true" in js
    assert "request_id" in js
