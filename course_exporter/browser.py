"""Browser seam over an OpenClaw node: the owner's logged-in Chrome evaluates the page scripts; cookies stay there."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Callable
from urllib.parse import urlsplit

from .media import redact
from .model import BrowserError

LOST_TAB = "tab not found"

Runner = Callable[..., subprocess.CompletedProcess]


class OpenClawBrowser:
    """One tab per origin, opened on the first URL of that origin and never navigated (a navigated tab drops out of
    the extension's reach); scripts read other same-origin pages with fetch(). A lost tab is reopened once."""

    def __init__(self, node: str, profile: str, openclaw: str, timeout_ms: int = 60000,
                 run: Runner = subprocess.run) -> None:
        self._node = node
        self._profile = profile
        self._openclaw = openclaw
        self._timeout_ms = timeout_ms
        self._run = run
        self._tabs: dict[str, str] = {}

    def evaluate(self, url: str, script: str) -> Any:
        try:
            return self._evaluate_in(self._tab(url), script)
        except BrowserError as error:
            if LOST_TAB not in str(error):
                raise
            self._tabs.pop(_origin(url), None)
            return self._evaluate_in(self._tab(url), script)

    def close(self) -> None:
        for target in self._tabs.values():
            try:
                self._call("DELETE", f"/tabs/{target}", None)
            except BrowserError:
                pass
        self._tabs.clear()

    def _tab(self, url: str) -> str:
        origin = _origin(url)
        if origin not in self._tabs:
            opened = self._call("POST", "/tabs/open", {"url": url})
            target = opened.get("suggestedTargetId") or opened.get("targetId")
            if not target:
                raise BrowserError("tabs/open returned no target id")
            self._tabs[origin] = target
        return self._tabs[origin]

    def _evaluate_in(self, target: str, script: str) -> Any:
        result = self._call("POST", "/act", {"kind": "evaluate", "targetId": target, "fn": f"async () => ({script})"})
        return result.get("result")

    def _call(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        params: dict[str, Any] = {"method": method, "path": path, "query": {"profile": self._profile}}
        if body is not None:
            params["body"] = body
        args = [self._openclaw, "nodes", "invoke", "--node", self._node, "--command", "browser.proxy",
                "--params", json.dumps(params, ensure_ascii=False), "--json",
                "--timeout", str(self._timeout_ms), "--invoke-timeout", str(self._timeout_ms)]
        try:
            proc = self._run(args, capture_output=True, text=True, timeout=self._timeout_ms / 1000 + 60)
        except subprocess.TimeoutExpired:
            raise BrowserError(f"{path}: openclaw timed out") from None
        if proc.returncode != 0:
            raise BrowserError(f"{path}: openclaw exit {proc.returncode}: {redact(proc.stderr + proc.stdout, 300)}")
        return _payload_result(path, proc.stdout)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _payload_result(path: str, stdout: str) -> dict[str, Any]:
    try:
        reply = json.loads(stdout)
    except json.JSONDecodeError:
        raise BrowserError(f"{path}: openclaw printed no JSON") from None
    result = (reply.get("payload") or {}).get("result") or {}
    if not reply.get("ok") or not result.get("ok", True):
        raise BrowserError(f"{path}: browser call not ok: {redact(str(result.get('error') or reply.get('error')), 300)}")
    return result
