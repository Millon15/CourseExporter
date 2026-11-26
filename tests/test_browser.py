import json
import subprocess
import unittest

from course_exporter.browser import OpenClawBrowser
from course_exporter.model import BrowserError


def reply(result: dict, ok: bool = True) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, json.dumps({"ok": ok, "payload": {"result": result}}), "")


class FakeOpenClaw:
    def __init__(self, *results: subprocess.CompletedProcess) -> None:
        self.results, self.params = list(results), []

    def __call__(self, args, **kwargs):
        self.params.append(json.loads(args[args.index("--params") + 1]))
        return self.results.pop(0)


class BrowserTest(unittest.TestCase):
    def test_one_tab_per_origin_is_opened_once_then_reused_and_closed(self):
        run = FakeOpenClaw(reply({"ok": True, "suggestedTargetId": "t4"}), reply({"ok": True, "result": "one"}),
                           reply({"ok": True, "result": {"n": 2}}),
                           reply({"ok": True, "suggestedTargetId": "t5"}), reply({"ok": True, "result": 3}),
                           reply({"ok": True}), reply({"ok": True}))
        tab = OpenClawBrowser(node="mac", profile="chrome", openclaw="openclaw", run=run)
        self.assertEqual(tab.evaluate("https://a.test/1", "document.title"), "one")
        self.assertEqual(tab.evaluate("https://a.test/2", "({n: 2})"), {"n": 2})
        self.assertEqual(tab.evaluate("https://player.test/p", "3"), 3)
        tab.close()
        calls = [(p["method"], p["path"]) for p in run.params]
        self.assertEqual(calls, [("POST", "/tabs/open"), ("POST", "/act"), ("POST", "/act"), ("POST", "/tabs/open"),
                                 ("POST", "/act"), ("DELETE", "/tabs/t4"), ("DELETE", "/tabs/t5")])
        self.assertEqual(run.params[1]["body"], {"kind": "evaluate", "targetId": "t4", "fn": "async () => (document.title)"})
        self.assertTrue(all(p["query"] == {"profile": "chrome"} for p in run.params))

    def test_a_lost_tab_is_reopened_once(self):
        lost = subprocess.CompletedProcess([], 1, "", "INVALID_REQUEST: Error: 404: tab not found: t4")
        run = FakeOpenClaw(reply({"ok": True, "suggestedTargetId": "t4"}), lost,
                           reply({"ok": True, "suggestedTargetId": "t9"}), reply({"ok": True, "result": "back"}))
        self.assertEqual(OpenClawBrowser(node="mac", profile="chrome", openclaw="openclaw", run=run).evaluate("https://a.test/1", "1"), "back")
        self.assertEqual(run.params[3]["body"]["targetId"], "t9")

    def test_failures_raise_browser_error_without_urls(self):
        failed = subprocess.CompletedProcess([], 1, "", "tab access revoked for https://a.test/x?token=SECRET")
        with self.assertRaises(BrowserError) as caught:
            OpenClawBrowser(node="mac", profile="chrome", openclaw="openclaw", run=FakeOpenClaw(failed)).evaluate("https://a.test/x", "1")
        self.assertNotIn("SECRET", str(caught.exception))
        not_ok = FakeOpenClaw(reply({"ok": True, "targetId": "t1"}), reply({"ok": False, "error": "eval threw"}))
        with self.assertRaises(BrowserError):
            OpenClawBrowser(node="mac", profile="chrome", openclaw="openclaw", run=not_ok).evaluate("https://a.test/x", "1")


if __name__ == "__main__":
    unittest.main()
