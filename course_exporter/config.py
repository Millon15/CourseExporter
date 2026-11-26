"""Deployment settings that name a real machine, account or path; they live outside the repository.

Read from $COURSE_EXPORTER_CONFIG or ~/.config/course-exporter/config.toml (see config.example.toml). Command line
flags override every value; a missing file means the built-in neutral defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

DEFAULT_PATH = Path.home() / ".config" / "course-exporter" / "config.toml"


@dataclass(frozen=True)
class Settings:
    browser_node: str = "mac-browser"
    """OpenClaw node whose Chrome holds the course logins."""
    browser_profile: str = "chrome"
    openclaw: str = str(Path.home() / ".npm-global" / "bin" / "openclaw")
    drive_remote: str | None = None
    """rclone remote folder, e.g. "gdrive:Courses"; each course goes to <drive_remote>/<course name>."""
    staging_root: str | None = None
    min_free_gib: int = 20
    max_staging_gib: int = 5


def load(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("COURSE_EXPORTER_CONFIG", DEFAULT_PATH))
    if not path.is_file():
        return Settings()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(Settings)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"{path}: unknown setting(s) {', '.join(unknown)}")
    return Settings(**data)
