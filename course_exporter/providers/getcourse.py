"""GetCourse stream discovery, lesson extraction and media resolution."""

from __future__ import annotations

from html.parser import HTMLParser
import json
from math import isfinite
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from ..archive import sanitize_html
from ..media import DownloadError, fetch_public_text
from ..model import Asset, AssetKind, Browser, BrowserError, Lesson, LessonRef, Media, MediaKind, UnsupportedMedia


_PAGE_PATH = re.compile(r"/(?:pl/)?teach/control/(stream|lesson)/view(?:/id/([0-9]+))?/?\Z")
_PLAYER_HOST = re.compile(r"vh-api-[0-9]+(?:-[a-z0-9]+)*\.gceuproxy\.com\Z")
_PLAYER_HOSTS = {"player02.getcourse.ru", "cf-api-2.vhcdn.com"}
_PLAYER_ASSIGNMENT = re.compile(r"\bwindow\s*\.\s*configs\s*=\s*(?=\{)")
_DRM_FIELDS = frozenset({
    "drm", "drmconfig", "drmtype", "drmenabled", "isdrm", "isdrmenabled", "isprotected", "encrypted",
    "isencrypted", "encryption", "encryptionkey", "keysystem", "keysystems", "licenseurl", "licenseserver",
    "widevine", "fairplay", "playready", "clearkey",
})

_FETCH_PAGE = r"""(async (url, project) => {
    if (new URL(url).origin !== location.origin) return {fetchError: true};
    let response;
    try {
        response = await fetch(url, {mode: 'same-origin', credentials: 'same-origin', cache: 'no-store'});
    } catch {
        return {fetchError: true};
    }
    if (!response.ok) return {httpError: response.status};
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const absolute = value => {
        if (typeof value !== 'string' || !value.trim()) return '';
        try {
            const link = new URL(value, response.url || url);
            return link.protocol === 'https:' && !link.username && !link.password ? link.href : '';
        } catch {
            return '';
        }
    };
    return project(page, absolute);
})"""


_DISCOVER = r"""(page, absolute) => {
    const main = page.querySelector('.main-page-block, main') || page.body;
    const label = anchor => {
        const copy = anchor.cloneNode(true);
        copy.querySelectorAll('div, p, br, li').forEach(node => {
            node.before('\n');
            if (node.tagName !== 'BR') node.after('\n');
        });
        return copy.textContent.trim();
    };
    return {
        login: !!page.querySelector('form[action*="/login"], form.state-login'),
        title: main.querySelector('h1')?.textContent.trim() || page.title,
        links: [...main.querySelectorAll('a[href]')]
            .filter(a => !a.closest('nav, .breadcrumb, .breadcrumbs, .lesson-navigation'))
            .map(a => ({text: label(a), url: absolute(a.getAttribute('href'))})),
    };
}"""


_LESSON = r"""(page, absolute) => {
    const content = '.lesson-content, .lesson-description, .lesson-text, .lesson-blocks, .lt-lesson';
    const excluded = [
        'form', '.lesson-comments', '.lesson-comment', '.lt-lesson-comment', '.comments',
        '.comment-list', '.gc-comment', '.other-answers', '.lesson-mission-wrapper', '.lesson-mission',
        '.lt-lesson-mission', '.lesson-navigation', '.lesson-access-denied',
        'script', 'style', 'template',
    ].join(', ');
    const frames = [], videos = [], assets = [], markup = [];
    const seen = new Map();
    const counts = {image: 0, file: 0};
    const add = (kind, url, label) => {
        const existing = seen.get(kind + ':' + url);
        if (existing) return existing;
        const asset = {kind, url, title: label, id: kind + '-' + (++counts[kind])};
        seen.set(kind + ':' + url, asset);
        assets.push(asset);
        return asset;
    };
    const videoPlaceholder = (element, id, label) => {
        const placeholder = page.createElement('video');
        placeholder.setAttribute('src', 'asset:' + id);
        placeholder.title = label;
        element.replaceWith(placeholder);
    };
    for (const root of page.querySelectorAll(content)) {
        if (root.parentElement?.closest(content) || root.closest(excluded)) continue;
        const copy = root.cloneNode(true);
        copy.querySelectorAll(excluded).forEach(element => element.remove());
        for (const element of copy.querySelectorAll('iframe, video, img, a[href]')) {
            if (!copy.contains(element)) continue;
            if (element.matches('iframe')) {
                const url = absolute(element.getAttribute('data-src') || element.getAttribute('src'));
                const player = url && new URL(url);
                const inVideoBlock = element.closest('.lt-video, .lesson-video, .video-player');
                if (!inVideoBlock && (!player || !/^\/sign-player(?:\/|$)/.test(player.pathname))) {
                    element.remove();
                    continue;
                }
                const id = 'video-' + (frames.length + 1);
                const label = element.title || 'Video ' + (frames.length + 1);
                frames.push({title: label, url});
                videoPlaceholder(element, id, label);
            } else if (element.matches('video')) {
                const sources = [element, ...element.querySelectorAll('source[src]')].map(source => ({
                    url: absolute(source.getAttribute('src')),
                    type: source.getAttribute('type') || '',
                })).filter(source => source.url);
                const id = 'video-direct-' + (videos.length + 1);
                const label = element.title || 'Video';
                videos.push({title: label, sources});
                videoPlaceholder(element, id, label);
            } else if (element.matches('img')) {
                const url = absolute(element.getAttribute('data-src') || element.getAttribute('src'));
                if (!url) {
                    element.remove();
                    continue;
                }
                const asset = add('image', url, element.alt || 'Image');
                element.setAttribute('src', 'asset:' + asset.id);
            } else {
                const url = absolute(element.getAttribute('href'));
                const attachment = element.matches('a[download], .lt-file a, .file-attachment a')
                    || (url && /\/(?:fileservice\/file|file\/download)\//.test(new URL(url).pathname));
                if (url && attachment) {
                    const asset = add('file', url, element.textContent.trim() || element.download || 'Attachment');
                    element.setAttribute('href', 'asset:' + asset.id);
                } else {
                    element.removeAttribute('href');
                    if (url) element.append(' (external link)');
                }
            }
        }
        markup.push(copy.innerHTML);
    }
    return {
        login: !!page.querySelector('form[action*="/login"], form.state-login'),
        gated: !!page.querySelector('.lesson-access-denied, .lesson-not-available, .lesson-locked'),
        html: markup.join('\n'),
        frames, videos, assets,
    };
}"""


class _PlayerPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.configs: list[dict[str, Any]] = []
        self._inline_script = False
        self._decoder = json.JSONDecoder()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._inline_script = not any(name == "src" for name, _ in attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inline_script = False

    def handle_data(self, data: str) -> None:
        if not self._inline_script:
            return
        for match in _PLAYER_ASSIGNMENT.finditer(data):
            try:
                config, _ = self._decoder.raw_decode(data, match.end())
            except (ValueError, RecursionError):
                continue
            self.configs.append(config)


def _enabled(value: Any) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            if item.strip().lower() not in ("", "false", "none", "off", "clear", "0"):
                return True
        elif item:
            return True
    return False


def _player_media(page: str, player_url: str) -> Media:
    parser = _PlayerPage()
    try:
        parser.feed(page)
        parser.close()
    except (AssertionError, ValueError):
        raise UnsupportedMedia("GetCourse player returned unreadable HTML") from None
    if not parser.configs:
        raise UnsupportedMedia("GetCourse player HTML does not expose a readable configuration")

    playlist, file, duration = None, None, None
    pending = list(reversed(parser.configs))
    while pending:
        config = pending.pop()
        if isinstance(config, list):
            pending.extend(reversed(config))
        if not isinstance(config, dict):
            continue
        playlist = playlist or _player_source(config.get("masterPlaylistUrl"), player_url)
        file = file or _player_source(config.get("mp4Url"), player_url)
        duration = duration or _duration_seconds(config.get("videoDuration"))
        for key, value in config.items():
            if re.sub(r"[^a-z]", "", key.lower()) in _DRM_FIELDS and _enabled(value):
                raise UnsupportedMedia("GetCourse player advertises DRM or encryption")
            if isinstance(value, (dict, list)):
                pending.append(value)

    headers = {"Referer": f"https://{urlsplit(player_url).netloc}/"}
    if playlist is not None:
        return Media("hls", playlist, headers, expected_seconds=duration)
    if file is not None:
        return Media("mp4", file, headers, expected_seconds=duration)
    raise UnsupportedMedia("GetCourse player has no supported source")


def _player_source(value: Any, base: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _https_url(urljoin(base, value))
    except ValueError:
        return None


def _duration_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if isfinite(seconds) and seconds > 0 else None


def _https_url(value: Any) -> str | None:
    if not isinstance(value, str) or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme == "https" and url.hostname and url.username is None:
            return url._replace(fragment="").geturl()
    except ValueError:
        pass
    return None


def _page(value: Any, base: str) -> tuple[str, str, str] | None:
    if not isinstance(value, str):
        return None
    try:
        url = _https_url(urljoin(base, value))
    except ValueError:
        return None
    if url is None:
        return None
    parts, origin = urlsplit(url), urlsplit(base)
    if parts.netloc.lower() != origin.netloc.lower():
        return None
    match = _PAGE_PATH.fullmatch(parts.path)
    if match is None:
        return None
    kind, page_id = match.groups()
    page_id = page_id or parse_qs(parts.query).get("id", [""])[0]
    if not re.fullmatch(r"[0-9]+", page_id):
        return None
    return kind, page_id, f"https://{origin.netloc}/teach/control/{kind}/view/id/{page_id}"


def _read(browser: Browser, url: str, project: str) -> dict[str, Any]:
    result = browser.evaluate(url, f"{_FETCH_PAGE}({json.dumps(url)}, {project})")
    if not isinstance(result, dict):
        raise BrowserError("GetCourse returned an invalid page projection")
    if result.get("login") or result.get("httpError") in (401, 403):
        raise BrowserError("GetCourse requires sign-in in the browser")
    if result.get("fetchError"):
        raise BrowserError("GetCourse page could not be fetched in the browser")
    if result.get("httpError"):
        raise BrowserError("GetCourse page returned an HTTP error")
    return result


def _rows(page: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = page.get(key)
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _label(value: Any, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _link_title(value: Any, kind: str, page_id: str) -> str:
    lines = _label(value, page_id).splitlines()
    if kind == "stream":
        lines = [line for line in lines if not re.fullmatch(r"\s*\d+\s+урок\w*\s*", line, re.I)]
    else:
        lines = [
            line for line in lines
            if not re.match(r"\s*(?:просмотрен\s*$|откроется\b|необходимо выполнить задание\b)", line, re.I)
        ]
    return next((line.strip() for line in lines if line.strip()), page_id)


def _asset_rows(page: dict[str, Any]) -> list[tuple[Asset, dict[str, Any]]]:
    assets = []
    for key, prefix, kind in (("frames", "video", "video"), ("videos", "video-direct", "video")):
        for index, row in enumerate(_rows(page, key), 1):
            asset_id = f"{prefix}-{index}"
            assets.append((Asset(asset_id, kind, _label(row.get("title"), f"Video {index}"), asset_id), row))

    counts: dict[AssetKind, int] = {"image": 0, "file": 0}
    for row in _rows(page, "assets"):
        kind = row.get("kind")
        if kind not in ("image", "file"):
            continue
        counts[kind] += 1
        asset_id = f"{kind}-{counts[kind]}"
        assets.append((Asset(asset_id, kind, _label(row.get("title"), kind.capitalize()), asset_id), row))
    return assets


def _signed_player(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.hostname or ""
    return (host in _PLAYER_HOSTS or _PLAYER_HOST.fullmatch(host) is not None) and (
        parts.path == "/sign-player" or parts.path.startswith("/sign-player/")
    )


def _direct_kind(url: str, mime: Any) -> MediaKind | None:
    mime = mime.split(";", 1)[0].strip().lower() if isinstance(mime, str) else ""
    path = urlsplit(url).path.lower()
    if mime in ("application/vnd.apple.mpegurl", "application/x-mpegurl") or path.endswith(".m3u8"):
        return "hls"
    if mime == "video/mp4" or path.endswith(".mp4"):
        return "mp4"
    return None


class GetCourseProvider:
    name = "getcourse"

    def discover(self, browser: Browser, course_url: str) -> list[LessonRef]:
        root = _page(course_url, course_url)
        if root is None or root[0] != "stream":
            raise ValueError("Expected a GetCourse stream URL")
        _, course_id, root_url = root
        lessons: list[LessonRef] = []
        streams_seen = {course_id}
        lessons_seen: set[str] = set()

        def visit(url: str, order: tuple[int, ...], modules: tuple[str, ...], label: str) -> None:
            page = _read(browser, url, _DISCOVER)
            if not isinstance(page.get("links"), list):
                raise BrowserError("GetCourse returned an invalid stream projection")
            if order:
                modules += (_label(page.get("title"), label),)
            position = 0
            for link in _rows(page, "links"):
                child = _page(link.get("url"), root_url)
                if child is None:
                    continue
                kind, page_id, child_url = child
                seen = streams_seen if kind == "stream" else lessons_seen
                if page_id in seen:
                    continue
                seen.add(page_id)
                position += 1
                child_order = (*order, position)
                title = _link_title(link.get("text"), kind, page_id)
                if kind == "stream":
                    visit(child_url, child_order, modules, title)
                else:
                    lessons.append(LessonRef(self.name, course_id, page_id, child_order, title, child_url, modules))

        visit(root_url, (), (), course_id)
        return lessons

    def extract(self, browser: Browser, ref: LessonRef) -> Lesson:
        page = _read(browser, ref.url, _LESSON)
        if page.get("gated"):
            return Lesson(ref, "gated", "")
        html = page.get("html")
        assets = tuple(asset for asset, _ in _asset_rows(page))
        return Lesson(ref, "available", sanitize_html(html) if isinstance(html, str) else "", assets)

    def resolve(self, browser: Browser, ref: LessonRef, asset: Asset) -> Media:
        page = _read(browser, ref.url, _LESSON)
        if page.get("gated"):
            raise UnsupportedMedia("GetCourse lesson is gated")

        for current, row in _asset_rows(page):
            if current.ref != asset.ref or current.kind != asset.kind:
                continue
            if current.kind != "video":
                url = _https_url(row.get("url"))
                if url:
                    return Media("file", url, {"Referer": ref.url})
                raise UnsupportedMedia("GetCourse attachment URL is unavailable")

            if current.ref.startswith("video-direct-"):
                for source in _rows(row, "sources") or [row]:
                    url = _https_url(source.get("url") or source.get("src"))
                    kind = _direct_kind(url, source.get("type")) if url else None
                    if url and kind:
                        return Media(kind, url, {"Referer": ref.url})
                raise UnsupportedMedia("GetCourse video has no supported source")

            player = _https_url(row.get("url"))
            if not player or not _signed_player(player):
                raise UnsupportedMedia("GetCourse signed player URL is unavailable or unrecognized")
            headers = {"Referer": f"https://{urlsplit(ref.url).netloc}/"}
            try:
                player_html = fetch_public_text(player, headers)
            except DownloadError as error:
                if re.search(r"\bHTTP Error 403\b", str(error)):
                    raise UnsupportedMedia("GetCourse player returned HTTP 403 on the VPS") from None
                raise
            return _player_media(player_html, player)

        raise UnsupportedMedia("GetCourse asset is no longer listed")
