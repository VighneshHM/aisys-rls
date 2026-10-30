"""UI hygiene regressions: stale cached scripts and the literal word 'null' appearing in pages."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from conftest import login_card
from shelfsense import pages

TEMPLATES = Path(pages.__file__).parent / "web" / "templates"
DOM_CALLS = ("replaceChildren(", ".append(", ".prepend(")


# ---------------------------------------------------------------- caching
@pytest.mark.req("NFR05", "AC02")
def test_static_files_are_revalidated_not_blindly_cached(client):
    for path in ("/static/ui.js", "/static/app.css"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and r.headers["etag"]
        again = client.get(path, headers={"If-None-Match": r.headers["etag"]})
        assert again.status_code == 304  # unchanged files cost nothing, changed files are always picked up


@pytest.mark.req("NFR05", "AC02")
def test_pages_reference_scripts_and_styles_with_a_content_based_version(client):
    login_card(client, "STAFFLIB0001")
    stamp = pages.asset_version()
    assert stamp != pages.__version__  # not the constant release number that kept stale files alive
    assert f"app.css?v={stamp}" in client.get("/").text or f"app.css?v={stamp}" in client.get("/dashboard").text
    assert f"ui.js?v={stamp}" in client.get("/catalogue").text
    assert f"ui.js?v={stamp}" in client.get("/tagging").text


@pytest.mark.req("NFR05")
def test_asset_version_changes_when_a_static_file_changes(tmp_path, monkeypatch):
    (tmp_path / "ui.js").write_text("a", encoding="utf-8")
    monkeypatch.setattr(pages, "STATIC_DIR", tmp_path)
    before = pages.asset_version()
    assert pages.asset_version() == before  # stable while nothing changes
    st = (tmp_path / "ui.js").stat()
    os.utime(tmp_path / "ui.js", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert pages.asset_version() != before


# ---------------------------------------------------------------- the literal word "null"
def top_level_nulls(source: str) -> list[str]:
    """Find DOM calls (replaceChildren/append/prepend) that are handed a bare `... : null` / `null` argument.

    Element.replaceChildren(null) inserts the TEXT "null". Our `h()` and `UI.fill()` helpers skip nulls, so those
    are fine; the raw DOM methods are not. Conditionals nested inside h(...) are at depth > 1 and are ignored.
    """
    problems = []
    for call in DOM_CALLS:
        start = 0
        while (i := source.find(call, start)) != -1:
            start = i + len(call)
            depth, j, quote = 1, start, None
            top = []
            while j < len(source) and depth:
                ch = source[j]
                if quote:
                    if ch == "\\":
                        j += 1
                    elif ch == quote:
                        quote = None
                elif ch in "\"'`":
                    quote = ch
                elif ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                if depth == 1 and not quote:
                    top.append(ch)
                j += 1
            flat = "".join(top)
            if re.search(r"(:|\?|,|^)\s*null\s*(,|$)", flat):
                problems.append(f"{call} near: {source[i : i + 60]!r}")
    return problems


@pytest.mark.req("NFR05", "AC02")
def test_the_scanner_catches_the_bug_it_exists_for():
    bad = 'el.replaceChildren(h("div"), cond ? h("p", {}, "x") : null, h("span"));'
    good = 'el.replaceChildren(h("div"), h("p", {}, cond ? "a" : null), h("span")); UI.fill(el, cond ? 1 : null);'
    assert top_level_nulls(bad) and not top_level_nulls(good)
    assert top_level_nulls("box.append(x ? y : null)") and not top_level_nulls('box.append(h("a", {}, x ? y : null))')


@pytest.mark.req("NFR05", "AC02")
@pytest.mark.parametrize("template", sorted(p.name for p in TEMPLATES.glob("*.html")))
def test_no_page_script_passes_a_raw_null_to_the_dom(template):
    assert top_level_nulls((TEMPLATES / template).read_text(encoding="utf-8")) == []


@pytest.mark.req("NFR05", "AC02")
def test_shared_helper_is_null_safe():
    js = (Path(pages.__file__).parent / "web" / "static" / "ui.js").read_text(encoding="utf-8")
    assert "function fill(" in js and "k != null && k !== false" in js  # the filter that keeps "null" out of the page


@pytest.mark.req("NFR05", "AC02")
def test_text_box_styles_do_not_leak_onto_radio_buttons():
    """A bare `.confirm input {width:100%}` once stretched the remove/retire radios across the whole panel."""
    css = (Path(pages.__file__).parent / "web" / "static" / "app.css").read_text(encoding="utf-8")
    for selector in re.findall(r"^([^{}\n]*\binput\b[^{}\n]*)\{", css, flags=re.MULTILINE):
        sel = selector.strip()
        if ".confirm" in sel:  # panels that contain radios must only style text-like inputs
            assert ":not([type=radio])" in sel or "[type=radio]" in sel, f"selector styles radios too: {sel}"
    assert ".radio input[type=radio]" in css and ".radio.on" in css


def test_no_page_script_declares_the_same_name_twice():
    """A duplicate `const` makes the browser drop the page's whole script (it once broke the gate page silently)."""
    import re
    from collections import Counter
    from pathlib import Path

    templates = Path(__file__).parent.parent / "src" / "shelfsense" / "web" / "templates"
    for path in sorted(templates.glob("*.html")):
        names = re.findall(
            r"^  (?:const|let|function|async function) ([A-Za-z_$][\w$]*)", path.read_text("utf-8"), re.M
        )
        dupes = [n for n, c in Counter(names).items() if c > 1]
        assert not dupes, f"{path.name} declares {dupes} twice"
