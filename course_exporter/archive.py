"""Write the offline course index and lesson pages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape
from html.parser import HTMLParser
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import unicodedata
from urllib.parse import quote, urlsplit

from .model import Lesson, LessonRef


_TEXT_TAGS = frozenset("""
    a abbr article b blockquote br caption code dd details div dl dt em figcaption
    figure h1 h2 h3 h4 h5 h6 hr i kbd li mark ol p pre s section small span strong
    sub summary sup table tbody td th thead tr u ul
""".split())
_VOID_TAGS = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_DROP_CONTENT = frozenset("button embed form iframe math noscript object script select style svg template textarea".split())
_ASSET_LINK = re.compile(r"asset:([A-Za-z0-9_.-]+)\Z")


def _asset_id(value: str) -> str | None:
    match = _ASSET_LINK.fullmatch(value)
    return match[1] if match else None


def _outside_url(href: str) -> str | None:
    if href.startswith("//"):
        href = f"https:{href}"
    if not href.lower().startswith(("http://", "https://")) or re.search(r"[\s\\\x00-\x1f\x7f]", href):
        return None
    try:
        return href if urlsplit(href).hostname else None
    except ValueError:
        return None


def _missing(asset_id: str, title: str) -> str:
    return f'<span class="missing" data-asset="asset:{escape(asset_id)}">{escape(title)} (missing)</span>'


class _LessonHTML(HTMLParser):
    def __init__(self, links: Mapping[str, str] | None):
        super().__init__(convert_charrefs=True)
        self.links = links
        self.parts: list[str] = []
        self.open_tags: list[tuple[str, str]] = []
        self.hidden: list[str] = []
        self.used_assets: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.hidden or tag in _DROP_CONTENT:
            if tag not in _VOID_TAGS:
                self.hidden.append(tag)
            return

        attrs_by_name = {name: value or "" for name, value in attrs}
        if tag in {"img", "video"}:
            asset_id = _asset_id(attrs_by_name.get("src", ""))
            label = attrs_by_name.get("alt") or attrs_by_name.get("title") or tag.capitalize()
            if asset_id is not None:
                self.parts.append(self.asset_markup(tag, asset_id, label))
            elif tag == "img" and attrs_by_name.get("alt"):
                self.parts.append(escape(label))
            return
        if tag not in _TEXT_TAGS:
            return

        opening = f"<{tag}>"
        closing = f"</{tag}>"
        if tag == "a":
            href = attrs_by_name.get("href", "").strip()
            asset_id = _asset_id(href)
            if asset_id is not None:
                self.used_assets.add(asset_id)
                if self.links is None:
                    opening = f'<a href="asset:{asset_id}">'
                elif asset_id in self.links:
                    opening = f'<a href="{escape(self.links[asset_id])}" download>'
                else:
                    opening = f'<span class="missing" data-asset="asset:{asset_id}">'
                    closing = " (missing)</span>"
            elif outside_url := _outside_url(href):
                opening = f'<a href="{escape(outside_url)}" rel="noreferrer">'
            else:
                opening = "<span>"
                closing = "</span>"

        self.parts.append(opening)
        if tag not in _VOID_TAGS:
            self.open_tags.append((tag, closing))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag in self.hidden:
                index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
                del self.hidden[index:]
            return
        for index in range(len(self.open_tags) - 1, -1, -1):
            if self.open_tags[index][0] == tag:
                self._close_from(index)
                break

    def _close_from(self, index: int) -> None:
        while len(self.open_tags) > index:
            _, closing = self.open_tags.pop()
            self.parts.append(closing)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(escape(data, quote=False))

    def asset_markup(self, tag: str, asset_id: str, label: str) -> str:
        self.used_assets.add(asset_id)
        if self.links is None:
            source = f"asset:{asset_id}"
        else:
            source = self.links.get(asset_id)
            if source is None:
                return _missing(asset_id, label)
        source = escape(source)
        label = escape(label)
        if tag == "img":
            return f'<img src="{source}" alt="{label}">'
        if tag == "video":
            return f'<video controls preload="metadata" src="{source}" title="{label}"></video>'
        download = " download" if self.links is not None else ""
        return f'<a href="{source}"{download}>{label}</a>'

    def close(self) -> None:
        super().close()
        self._close_from(0)


def sanitize_html(html: str) -> str:
    """Keep lesson text and opaque asset references for the manifest."""
    parser = _LessonHTML(None)
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


def lesson_dir(out: Path, ref: LessonRef) -> Path:
    """Return the directory shared by the lesson page and its downloads."""
    title = unicodedata.normalize("NFKC", ref.title).lower()
    slug = re.sub(r"[\W_]+", "-", title).strip("-")
    slug = slug.encode("utf-8")[:96].decode("utf-8", errors="ignore").rstrip("-")
    order = ".".join(f"{position:03d}" for position in ref.order)
    return out / f"{order}-{slug or 'lesson'}"


def _relative_url(path: Path, start: Path) -> str:
    return quote(Path(os.path.relpath(path, start)).as_posix())


def _write_page(path: Path, title: str, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src file: 'self'; media-src file: 'self'; style-src 'unsafe-inline'">
<title>{escape(title)}</title>
<style>
body {{ max-width: 52rem; margin: 2rem auto; padding: 0 1rem; font: 1rem/1.6 system-ui, sans-serif; }}
img, video {{ max-width: 100%; height: auto; }}
video {{ display: block; }}
pre {{ overflow-x: auto; }}
.missing {{ color: #96401a; }}
</style></head><body>{content}</body></html>
"""
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(page)
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def write_lesson(out: Path, lesson: Lesson, files: Mapping[str, Path]) -> Path:
    directory = lesson_dir(out, lesson.ref)
    root = out.resolve()
    resolved_directory = directory.resolve()
    if not resolved_directory.is_relative_to(root):
        raise ValueError("Lesson directory is outside the archive")
    links = {}
    for asset_id, file in files.items():
        resolved = file.resolve()
        if resolved.is_relative_to(root):
            links[asset_id] = _relative_url(resolved, resolved_directory)

    parser = _LessonHTML(links)
    parser.feed(lesson.html)
    parser.close()
    for asset in lesson.assets:
        if asset.asset_id not in parser.used_assets:
            tag = {"file": "a", "image": "img", "video": "video"}[asset.kind]
            parser.parts.append(f"<p>{parser.asset_markup(tag, asset.asset_id, asset.title)}</p>")

    title = lesson.ref.title
    status = "<p>This lesson is gated.</p>" if lesson.status == "gated" else ""
    back = _relative_url(out / "index.html", directory)
    content = f'<nav><a href="{back}">Course index</a></nav><main><h1>{escape(title)}</h1>{status}'
    return _write_page(directory / "lesson.html", title, content + "".join(parser.parts) + "</main>")


def write_index(out: Path, entries: Sequence[tuple[LessonRef, str]]) -> Path:
    items = []
    for ref, status in sorted(entries, key=lambda entry: entry[0].order):
        page = lesson_dir(out, ref) / "lesson.html"
        title = escape(ref.title)
        if page.is_file():
            title = f'<a href="{_relative_url(page, out)}">{title}</a>'
        module = f"<small>{escape(' / '.join(ref.modules))}</small><br>" if ref.modules else ""
        items.append(f"<li>{module}{title} <small>({escape(status)})</small></li>")
    content = "<main><h1>Course index</h1><ol>" + "\n".join(items) + "</ol></main>"
    return _write_page(out / "index.html", "Course index", content)
