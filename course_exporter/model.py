"""Types shared by the providers, the runner and the archive writer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

AssetKind = Literal["video", "image", "file"]
MediaKind = Literal["hls", "mp4", "file"]


@dataclass(frozen=True)
class LessonRef:
    provider: str
    course_id: str
    lesson_id: str
    order: tuple[int, ...]
    """Module and lesson position, in course order."""
    title: str
    url: str
    modules: tuple[str, ...] = ()


@dataclass(frozen=True)
class Asset:
    asset_id: str
    """Stable within its lesson."""
    kind: AssetKind
    title: str
    ref: str
    """Provider-opaque locator; never a signed URL."""
    expected_seconds: float | None = None
    """Video length the provider states; verification and the staging reservation use it."""


@dataclass(frozen=True)
class Lesson:
    ref: LessonRef
    status: Literal["available", "gated"]
    html: str
    """Sanitized lesson text; assets appear as `asset:<asset_id>` links."""
    assets: tuple[Asset, ...] = ()


@dataclass(frozen=True)
class Media:
    """A signed source, held in memory only: repr hides it so no log or traceback prints it."""

    kind: MediaKind
    url: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    expected_seconds: float | None = None
    """Video length the player states, for providers that learn it only at resolve time."""


class UnsupportedMedia(Exception):
    """An asset this exporter will not fetch (DRM, encryption, an unknown player); the message is a reason, never a URL."""


class BrowserError(Exception):
    """The Mac browser call failed; the message never carries a URL query string or page content."""


class Browser(Protocol):
    def evaluate(self, url: str, script: str) -> Any:
        """Open `url` in the owner's Chrome and return the JSON value of the JavaScript expression `script`."""
        ...


class Provider(Protocol):
    name: str

    def discover(self, browser: Browser, course_url: str) -> list[LessonRef]: ...

    def extract(self, browser: Browser, ref: LessonRef) -> Lesson: ...

    def resolve(self, browser: Browser, ref: LessonRef, asset: Asset) -> Media:
        """Fresh signed media for one asset, right before download; raises UnsupportedMedia."""
        ...
