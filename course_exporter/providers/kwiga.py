"""Kwiga course discovery and lesson export through the authenticated browser."""

from __future__ import annotations

from html import escape
import json
from math import isfinite
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..archive import sanitize_html
from ..model import Asset, AssetKind, Browser, BrowserError, Lesson, LessonRef, Media, UnsupportedMedia


_COURSE_PATH = re.compile(r"/courses/([A-Za-z0-9_-]+)/?\Z")
_LESSON_SLUG = re.compile(r"[A-Za-z0-9_-]+\Z")
_HLS_MIMES = {"application/vnd.apple.mpegurl", "application/x-mpegurl"}

_FETCH_PAGE = r"""(async (url, project, fileId) => {
    if (new URL(url).origin !== location.origin) return {fetchError: true};
    let response;
    try {
        response = await fetch(url, {mode: 'same-origin', credentials: 'same-origin', cache: 'no-store'});
    } catch {
        return {fetchError: true};
    }
    if (!response.ok) return {httpError: response.status};
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    return project(page, response.url || url, fileId);
})"""

_COURSE = r"""(page, baseUrl) => ({
    entries: [...page.querySelectorAll('a[href]')].flatMap(anchor => {
        try {
            const url = new URL(anchor.getAttribute('href'), baseUrl);
            const title = anchor.textContent.trim() || anchor.getAttribute('title') || '';
            return [{url: url.href, title}];
        } catch {
            return [];
        }
    }),
})"""

_LESSON = r"""(page, _baseUrl, fileId) => {
    let dto;
    for (const script of page.querySelectorAll('script:not([src])')) {
        const text = script.textContent;
        const match = /\bconst\s+lesson_dto\s*=\s*(?=\{)/.exec(text);
        if (!match) continue;
        const tail = text.slice(match.index + match[0].length);
        let depth = 0;
        for (const token of tail.matchAll(/"(?:[^"\\]|\\[\s\S])*"|[{}]/g)) {
            if (token[0] === '{') depth++;
            if (token[0] === '}' && --depth === 0) {
                try {
                    dto = JSON.parse(tail.slice(0, token.index + 1));
                } catch {
                    return {invalidDTO: true};
                }
                break;
            }
        }
        if (dto) break;
    }
    if (!dto) return {invalidDTO: true};
    const available = dto.is_lesson_available ?? dto.section?.is_available;
    if (available === false) return {available};
    const units = dto.section?.info_units;
    if (!Array.isArray(units)) return {invalidDTO: true};
    const supported = unit => unit && (unit.type_id === 2 || unit.type_id === 3);
    const activeFiles = unit => Array.isArray(unit.files)
        ? unit.files.filter(file => file && !file.is_deleted) : [];
    if (fileId !== null) {
        for (const unit of units.filter(supported)) {
            const file = activeFiles(unit).find(item => String(item.id) === fileId);
            if (file) {
                const drm = [dto, unit, file].some(item => item.drm || item.is_drm || item.encrypted || item.is_encrypted);
                return {available, file: {url: file.url, drm}};
            }
        }
        return {available, file: null};
    }
    return {
        available,
        has_only_one_section: dto.has_only_one_section,
        section: {
            info_units: units.map(unit => {
                if (!supported(unit)) {
                    return {type_id: Number.isSafeInteger(unit?.type_id) ? unit.type_id : null};
                }
                return {
                    type_id: unit.type_id,
                    description_formatted: unit.type_id === 3 ? unit.description_formatted : null,
                    description: unit.type_id === 3 ? unit.description : null,
                    files: activeFiles(unit).map(({id, mime_type, name, original_name, duration}) => ({
                        id, mime_type, name, original_name, duration,
                    })),
                };
            }),
        },
    };
}"""


def _https_url(value: Any) -> str | None:
    if not isinstance(value, str) or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme == "https" and parts.hostname and parts.username is None:
            return parts._replace(fragment="").geturl()
    except ValueError:
        pass
    return None


def _read(browser: Browser, url: str, project: str, file_id: str | None = None) -> dict[str, Any]:
    result = browser.evaluate(url, f"{_FETCH_PAGE}({json.dumps(url)}, {project}, {json.dumps(file_id)})")
    if not isinstance(result, dict):
        raise BrowserError("Kwiga returned an invalid page projection")
    if result.get("fetchError") or result.get("httpError"):
        raise BrowserError("Kwiga page could not be fetched in the browser")
    if result.get("invalidDTO"):
        raise BrowserError("Kwiga page does not expose a readable lesson DTO")
    return result


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _duration_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if isfinite(seconds) and seconds > 0 else None


def _asset(row: dict[str, Any]) -> Asset | None:
    file_id = str(row.get("id"))
    if row.get("is_deleted") or not re.fullmatch(r"[0-9]+", file_id):
        return None
    mime = _text(row.get("mime_type")).split(";", 1)[0].strip().lower()
    kind: AssetKind = "file"
    if mime.startswith("video/") or mime in _HLS_MIMES:
        kind = "video"
    elif mime.startswith("image/"):
        kind = "image"
    title = _text(row.get("name")) or _text(row.get("original_name")) or f"{kind.capitalize()} {file_id}"
    duration = _duration_seconds(row.get("duration")) if kind == "video" else None
    return Asset(file_id, kind, title, file_id, expected_seconds=duration)


def _placeholder(asset: Asset) -> str:
    src = f"asset:{asset.asset_id}"
    title = escape(asset.title)
    if asset.kind == "video":
        return f'<p><video src="{src}" title="{title}"></video></p>'
    if asset.kind == "image":
        return f'<p><img src="{src}" alt="{title}"></p>'
    return f'<p><a href="{src}">{title}</a></p>'


class KwigaProvider:
    name = "kwiga"

    def discover(self, browser: Browser, course_url: str) -> list[LessonRef]:
        validated = _https_url(course_url)
        if validated is None:
            raise ValueError("Expected an HTTPS Kwiga course URL")
        parts = urlsplit(validated)
        match = _COURSE_PATH.fullmatch(parts.path)
        if match is None:
            raise ValueError("Expected a Kwiga course root URL")

        course_id = match[1]
        origin = f"https://{parts.netloc}"
        root = f"{origin}/courses/{course_id}"
        prefix = f"/courses/{course_id}/"
        page = _read(browser, root, _COURSE)
        entries = _rows(page.get("entries"))
        refs: list[LessonRef] = []
        seen_lessons: set[str] = set()
        seen_modules: set[str] = set()
        module_position, lesson_position = 0, 0
        modules: tuple[str, ...] = ()

        for entry in entries:
            raw_url = entry.get("url")
            if not isinstance(raw_url, str):
                continue
            try:
                target = _https_url(urljoin(root, raw_url))
            except ValueError:
                continue
            if target is None:
                continue
            link = urlsplit(target)
            if link.netloc.lower() != parts.netloc.lower() or not link.path.startswith(prefix):
                continue
            route = link.path.removeprefix(prefix).rstrip("/")
            title = _text(entry.get("title"))
            module = re.fullmatch(r"modules/([0-9]+)", route)
            if module is not None and module[1] not in seen_modules:
                seen_modules.add(module[1])
                module_position += 1
                lesson_position = 0
                modules = (title or f"Module {module[1]}",)
            elif route != "modules" and _LESSON_SLUG.fullmatch(route) and route not in seen_lessons:
                seen_lessons.add(route)
                lesson_position += 1
                refs.append(LessonRef(self.name, course_id, route, (module_position, lesson_position),
                                      title or route, f"{root}/{route}", modules))
        if not refs:
            raise BrowserError("Kwiga course page contains no lesson links")
        return refs

    def extract(self, browser: Browser, ref: LessonRef) -> Lesson:
        page = _read(browser, ref.url, _LESSON)
        if page.get("available") is False:
            return Lesson(ref, "gated", "")
        section = page.get("section")
        if not isinstance(section, dict) or not isinstance(section.get("info_units"), list):
            raise BrowserError("Kwiga lesson projection has no info units")

        content: list[str] = []
        assets: dict[str, Asset] = {}
        for unit in _rows(section["info_units"]):
            unit_type = unit.get("type_id")
            if unit_type not in (2, 3):
                label = unit_type if type(unit_type) is int else "unknown"
                content.append(f"<p>Kwiga info unit type {label} was not exported.</p>")
                continue
            if unit_type == 3:
                if formatted := _text(unit.get("description_formatted")):
                    content.append(formatted)
                elif description := _text(unit.get("description")):
                    content.append(f"<p>{escape(description)}</p>")
            for row in _rows(unit.get("files")):
                asset = _asset(row)
                if asset is not None and asset.asset_id not in assets:
                    assets[asset.asset_id] = asset
                    content.append(_placeholder(asset))
        if page.get("has_only_one_section") is False:
            content.append("<p>Additional Kwiga lesson sections were not exported.</p>")
        return Lesson(ref, "available", sanitize_html("\n".join(content)), tuple(assets.values()))

    def resolve(self, browser: Browser, ref: LessonRef, asset: Asset) -> Media:
        page = _read(browser, ref.url, _LESSON, asset.ref)
        if page.get("available") is False:
            raise UnsupportedMedia("Kwiga lesson is gated")
        file = page.get("file")
        if not isinstance(file, dict):
            raise UnsupportedMedia("Kwiga file is no longer listed")
        if file.get("drm"):
            raise UnsupportedMedia("Kwiga file advertises DRM or encryption")
        url = _https_url(file.get("url"))
        if url is None:
            raise UnsupportedMedia("Kwiga file URL is unavailable")
        origin = urlsplit(ref.url).netloc
        return Media("hls" if asset.kind == "video" else "file", url, {"Referer": f"https://{origin}/"})
