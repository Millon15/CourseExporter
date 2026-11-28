"""Linked course materials: Google Docs, Sheets, Slides, Drive files and Notion pages that a lesson links to.

| Link | Saved as |
| --- | --- |
| docs.google.com/document/d/<id> | PDF export |
| docs.google.com/spreadsheets/d/<id> | XLSX export |
| docs.google.com/presentation/d/<id> | PDF export |
| drive.google.com/file/d/<id>, open?id=, uc?id= | the file itself |
| <x>.notion.site/..<32 hex>, notion.so/..<32 hex> | PDF printed in the owner's Chrome |
| docs.google.com/forms/d/e/<id>, forms.gle/<code> | PDF of the form's questions, printed in the owner's Chrome |
| calendar.google.com/..?cid=<base64 calendar id> | the public calendar as .ics |
| any https URL whose path ends in a document suffix | the file itself |

Everything else stays a plain link."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, replace
import hashlib
from html import unescape
from pathlib import Path, PurePosixPath
import re
from urllib.parse import parse_qs, quote, urlsplit

from .model import Asset, Lesson, Media, MediaKind, UnsupportedMedia

PREFIX = "link-"
REF_PREFIX = "link:"
TITLE_LIMIT = 80
DOCUMENT_SUFFIXES = frozenset(".pdf .doc .docx .xls .xlsx .ppt .pptx .odt .ods .odp .rtf .txt .csv .epub .zip".split())

_ANCHOR = re.compile(r'<a href="(https://[^"]+)" rel="noreferrer">(.*?)</a>', re.S)
_TAG = re.compile(r"<[^>]+>")
_GOOGLE_DOC = re.compile(r"/(document|spreadsheets|presentation)/d/([A-Za-z0-9_-]{20,})")
_DRIVE_FILE = re.compile(r"/file/d/([A-Za-z0-9_-]{20,})")
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{20,}\Z")
_NOTION_ID = re.compile(r"([0-9a-f]{32})\Z")
_GOOGLE_FORM = re.compile(r"/forms/d/e/([A-Za-z0-9_-]{20,})")
_SHORT_FORM = re.compile(r"/([A-Za-z0-9]{8,})/?\Z")
FORM_READY = 'form[action*="formResponse"]'
"""Only an open form renders its questions inside this form; a sign-in wall or a closed form never does."""
_HTML_START = re.compile(rb"(?:\xef\xbb\xbf)?\s*(<!doctype html|<html)", re.I)
UNKNOWN_SUFFIX = ".bin"
ZIP_TAIL = 1 << 16
"""Bytes from the end of a zip that hold its central directory, where Office part names show the document type."""
_MAGIC = ((b"%PDF-", ".pdf"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif"),
          (b"\xd0\xcf\x11\xe0", ".doc"), (b"Rar!", ".rar"))
_OFFICE = ((b"word/document.xml", ".docx"), (b"xl/workbook.xml", ".xlsx"), (b"ppt/presentation.xml", ".pptx"))
_OFFICE_TYPES = b"[Content_Types].xml"
"""Every Office package carries it; a plain zip that merely holds a word/ folder does not."""
NOTION_READY = ".notion-page-content, .notion-collection-view-body"
"""A plain page renders its blocks into the first; a database page (a calendar, a table) into the second."""


@dataclass(frozen=True)
class Material:
    canonical: str
    """The link reduced to what identifies the material; the asset ref carries it."""
    kind: MediaKind
    source: str
    suffix: str
    ready_selector: str = ""
    """For a printed page: the element that exists only once the material itself rendered, not a sign-in wall."""


def add_materials(lesson: Lesson) -> Lesson:
    """Adds one `link-<digest>` asset per distinct material and a "saved copy" link after every anchor that points to
    it; the id follows the material, so a reordered or inserted link never inherits another material's saved file."""
    ids: dict[str, str] = {}
    assets: list[Asset] = []

    def annotate(anchor: re.Match[str]) -> str:
        material = classify(unescape(anchor[1]))
        if material is None:
            return anchor[0]
        if material.canonical not in ids:
            ids[material.canonical] = asset_id(material)
            assets.append(_asset(ids[material.canonical], material, unescape(anchor[1]), anchor[2]))
        return f'{anchor[0]} <a href="asset:{ids[material.canonical]}">saved copy</a>'

    html = _ANCHOR.sub(annotate, lesson.html)
    return replace(lesson, html=html, assets=lesson.assets + tuple(assets)) if assets else lesson


def asset_id(material: Material) -> str:
    return PREFIX + hashlib.sha256(material.canonical.encode()).hexdigest()[:12]


def ready_selector(media: Media) -> str:
    material = classify(media.url)
    return material.ready_selector if material else ""


def is_material(asset: Asset) -> bool:
    return asset.ref.startswith(REF_PREFIX)


def resolve(asset: Asset) -> Media:
    material = classify(asset.ref.removeprefix(REF_PREFIX))
    if material is None:
        raise UnsupportedMedia("linked material is no longer recognised")
    return Media(material.kind, material.source)


def refuse_html(path: Path) -> None:
    """A host that answers with a web page (a sign-in wall, a private document, Drive's virus-scan notice) instead of
    the file gave no material."""
    with path.open("rb") as file:
        head = file.read(512)
    if _HTML_START.match(head):
        raise UnsupportedMedia("the material host answered with a web page, not the file; it may be private")


def name_by_content(path: Path) -> Path:
    """Gives a file saved without a known suffix (a Drive download names neither its type nor its file) the suffix
    its leading bytes declare; any other file keeps its name."""
    if path.suffix != UNKNOWN_SUFFIX:
        return path
    suffix = _sniff(path)
    return path.rename(path.with_suffix(suffix)) if suffix else path


def classify(url: str) -> Material | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username is not None:
        return None
    host = parts.hostname.lower()
    if host == "docs.google.com":
        return _google_form(parts.path) or _google_doc(parts.path)
    if host == "forms.gle":
        return _short_form(parts.path)
    if host == "calendar.google.com":
        return _calendar(parts.query)
    if host == "drive.google.com":
        return _drive_file(parts.path, parts.query)
    if host.endswith(".notion.site") or host in ("notion.so", "www.notion.so"):
        return _notion_page(host, parts.path, parts._replace(fragment="").geturl())
    return _document_file(parts._replace(fragment="").geturl(), parts.path)


def _google_doc(path: str) -> Material | None:
    match = _GOOGLE_DOC.search(path)
    if match is None:
        return None
    kind, doc_id = match[1], match[2]
    base = f"https://docs.google.com/{kind}/d/{doc_id}"
    export = {"document": ("/export?format=pdf", ".pdf"), "spreadsheets": ("/export?format=xlsx", ".xlsx"),
              "presentation": ("/export/pdf", ".pdf")}[kind]
    return Material(base, "file", base + export[0], export[1])


def _google_form(path: str) -> Material | None:
    match = _GOOGLE_FORM.search(path)
    if match is None:
        return None
    form = f"https://docs.google.com/forms/d/e/{match[1]}"
    return Material(form, "pdf", form + "/viewform", ".pdf", FORM_READY)


def _short_form(path: str) -> Material | None:
    match = _SHORT_FORM.fullmatch(path)
    if match is None:
        return None
    form = f"https://forms.gle/{match[1]}"
    return Material(form, "pdf", form, ".pdf", FORM_READY)


def _calendar(query: str) -> Material | None:
    encoded = (parse_qs(query).get("cid") or [""])[0]
    try:
        calendar_id = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None
    if not re.fullmatch(r"[\w.+-]+@[\w.-]+", calendar_id):
        return None
    ics = f"https://calendar.google.com/calendar/ical/{quote(calendar_id, safe='')}/public/basic.ics"
    return Material(ics, "file", ics, ".ics")


def _drive_file(path: str, query: str) -> Material | None:
    match = _DRIVE_FILE.search(path)
    file_id = match[1] if match else None
    if file_id is None and path in ("/open", "/uc"):
        candidate = (parse_qs(query).get("id") or [""])[0]
        file_id = candidate if _DRIVE_ID.fullmatch(candidate) else None
    if file_id is None:
        return None
    return Material(f"https://drive.google.com/file/d/{file_id}", "file",
                    f"https://drive.google.com/uc?export=download&id={file_id}", "")


def _notion_page(host: str, path: str, url: str) -> Material | None:
    tail = path.rstrip("/").rsplit("/", 1)[-1].replace("-", "")
    match = _NOTION_ID.search(tail.lower())
    if match is None:
        return None
    page = f"https://{host}/{match[1]}"
    return Material(page, "pdf", url, ".pdf", NOTION_READY)


def _document_file(url: str, path: str) -> Material | None:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in DOCUMENT_SUFFIXES:
        return None
    return Material(url, "file", url, suffix)


def _sniff(path: Path) -> str | None:
    with path.open("rb") as file:
        head = file.read(8)
        if not head.startswith(b"PK\x03\x04"):
            return next((suffix for magic, suffix in _MAGIC if head.startswith(magic)), None)
        file.seek(max(0, file.seek(0, 2) - ZIP_TAIL))
        tail = file.read()
    if _OFFICE_TYPES not in tail:
        return ".zip"
    return next((suffix for part, suffix in _OFFICE if part in tail), ".zip")


def _asset(asset_id: str, material: Material, url: str, label_html: str) -> Asset:
    label = " ".join(unescape(_TAG.sub(" ", label_html)).split())[:TITLE_LIMIT].strip()
    title = label or PurePosixPath(urlsplit(material.canonical).path).name or asset_id
    if material.suffix and not title.lower().endswith(material.suffix):
        title += material.suffix
    return Asset(asset_id, "file", title, REF_PREFIX + urlsplit(url)._replace(fragment="").geturl())


