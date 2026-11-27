"""Browser seam over an OpenClaw node: the owner's logged-in Chrome evaluates the page scripts; cookies stay there."""

from __future__ import annotations

import base64
import json
import subprocess
import time
from contextlib import contextmanager
from typing import Any, Callable, Generator
from urllib.parse import urlsplit

from .media import redact
from .model import BrowserError, PageNotReady

LOST_TAB = "tab not found"
POLL_SECONDS = 1.5
SETTLE_POLLS = 2
"""Polls in a row with the same text length before a page counts as rendered."""
EXPAND_ROUNDS = 4
PDF_MAGIC = b"%PDF-"

Runner = Callable[..., subprocess.CompletedProcess]

_READY = "!!document.querySelector({selector})"
_TEXT_LENGTH = "document.readyState === 'complete' ? document.body.innerText.length : -1"
_EXPAND = r"""(async () => {
    for (let round = 0; round < %d; round++) {
        const closed = [...document.querySelectorAll('[role="button"][aria-expanded="false"]')];
        if (!closed.length) return 0;
        closed.forEach(button => button.click());
        await new Promise(done => setTimeout(done, %d));
    }
    return document.querySelectorAll('[role="button"][aria-expanded="false"]').length;
})()""" % (EXPAND_ROUNDS, int(POLL_SECONDS * 1000))


class OpenClawBrowser:
    """One tab per origin, opened on the first URL of that origin and never navigated (a navigated tab drops out of
    the extension's reach); scripts read other same-origin pages with fetch(). A lost tab is reopened once.
    `render` and `pdf` use a fresh tab of their own and close it."""

    def __init__(self, node: str, profile: str, openclaw: str, timeout_ms: int = 60000,
                 run: Runner = subprocess.run, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._node = node
        self._profile = profile
        self._openclaw = openclaw
        self._timeout_ms = timeout_ms
        self._run = run
        self._sleep = sleep
        self._clock = clock
        self._tabs: dict[str, str] = {}

    def evaluate(self, url: str, script: str) -> Any:
        try:
            return self._evaluate_in(self._tab(url), script)
        except BrowserError as error:
            if LOST_TAB not in str(error):
                raise
            self._tabs.pop(_origin(url), None)
            return self._evaluate_in(self._tab(url), script)

    def render(self, url: str, script: str, ready_selector: str, timeout_s: float = 45) -> Any:
        with self._fresh_tab(url) as target:
            self._wait_for(target, _READY.format(selector=json.dumps(ready_selector)), timeout_s)
            return self._evaluate_in(target, script)

    def pdf(self, url: str, ready_selector: str = "", timeout_s: float = 60) -> bytes:
        with self._fresh_tab(url) as target:
            if ready_selector:
                self._wait_for(target, _READY.format(selector=json.dumps(ready_selector)), timeout_s)
            self._wait_until_settled(target, timeout_s)
            self._evaluate_in(target, _EXPAND)
            payload = self._invoke("POST", "/pdf", {"targetId": target})
        return _pdf_bytes(payload)

    def close(self) -> None:
        for target in self._tabs.values():
            self._close_tab(target)
        self._tabs.clear()

    def _tab(self, url: str) -> str:
        origin = _origin(url)
        if origin not in self._tabs:
            self._tabs[origin] = self._open_tab(url)
        return self._tabs[origin]

    @contextmanager
    def _fresh_tab(self, url: str) -> Generator[str]:
        target = self._open_tab(url)
        try:
            yield target
        finally:
            self._close_tab(target)

    def _open_tab(self, url: str) -> str:
        opened = self._call("POST", "/tabs/open", {"url": url})
        target = opened.get("suggestedTargetId") or opened.get("targetId")
        if not target:
            raise BrowserError("tabs/open returned no target id")
        return target

    def _close_tab(self, target: str) -> None:
        try:
            self._call("DELETE", f"/tabs/{target}", None)
        except BrowserError:
            pass

    def _wait_for(self, target: str, condition: str, timeout_s: float) -> None:
        deadline = self._clock() + timeout_s
        while not self._evaluate_in(target, condition):
            if self._clock() >= deadline:
                raise PageNotReady(f"page did not render within {timeout_s:.0f}s")
            self._sleep(POLL_SECONDS)

    def _wait_until_settled(self, target: str, timeout_s: float) -> None:
        """Waits for the load event, then for the text length to hold still: client-rendered pages fill in late."""
        deadline = self._clock() + timeout_s
        last, steady = -1, 0
        while steady < SETTLE_POLLS:
            if self._clock() >= deadline:
                raise BrowserError(f"page did not settle within {timeout_s:.0f}s")
            self._sleep(POLL_SECONDS)
            length = self._evaluate_in(target, _TEXT_LENGTH)
            steady = steady + 1 if isinstance(length, int) and length > 0 and length == last else 0
            last = length

    def _evaluate_in(self, target: str, script: str) -> Any:
        result = self._call("POST", "/act", {"kind": "evaluate", "targetId": target, "fn": f"async () => ({script})"})
        return result.get("result")

    def _call(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        return self._invoke(method, path, body).get("result") or {}

    def _invoke(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """The proxy reply's payload: `result`, plus `files` for a route that returns one."""
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
        return _payload(path, proc.stdout)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _payload(path: str, stdout: str) -> dict[str, Any]:
    try:
        reply = json.loads(stdout)
    except json.JSONDecodeError:
        raise BrowserError(f"{path}: openclaw printed no JSON") from None
    payload = reply.get("payload") or {}
    result = payload.get("result") or {}
    if not reply.get("ok") or not result.get("ok", True):
        raise BrowserError(f"{path}: browser call not ok: {redact(str(result.get('error') or reply.get('error')), 300)}")
    return payload


def _pdf_bytes(payload: dict[str, Any]) -> bytes:
    for item in payload.get("files") or []:
        if not isinstance(item, dict) or item.get("mimeType") != "application/pdf":
            continue
        try:
            data = base64.b64decode(item.get("base64") or "", validate=True)
        except ValueError:
            raise BrowserError("/pdf: file envelope is not base64") from None
        if data.startswith(PDF_MAGIC):
            return data
    raise BrowserError("/pdf: the reply carried no PDF")
