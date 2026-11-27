import json
import subprocess
import unittest

from course_exporter.browser import OpenClawBrowser
from course_exporter.model import BrowserError, PageNotReady


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


def payload(result: dict, files: list | None = None) -> subprocess.CompletedProcess:
    body = {"result": result} if files is None else {"result": result, "files": files}
    return subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "payload": body}), "")


class FreshTabTest(unittest.TestCase):
    def browser(self, run):
        clock = iter(range(0, 1000, 2))
        return OpenClawBrowser(node="mac", profile="chrome", openclaw="openclaw", run=run, sleep=lambda s: None,
                               clock=lambda: next(clock))

    def test_render_waits_for_the_selector_then_evaluates_and_closes_its_own_tab(self):
        run = FakeOpenClaw(reply({"ok": True, "targetId": "q1"}), reply({"ok": True, "result": False}),
                           reply({"ok": True, "result": True}), reply({"ok": True, "result": ["task"]}),
                           reply({"ok": True}))
        self.assertEqual(self.browser(run).render("https://a.test/quiz/1", "[...x]", ".practice-question"), ["task"])
        self.assertEqual([(p["method"], p["path"]) for p in run.params],
                         [("POST", "/tabs/open"), ("POST", "/act"), ("POST", "/act"), ("POST", "/act"),
                          ("DELETE", "/tabs/q1")])
        self.assertIn('document.querySelector(".practice-question")', run.params[1]["body"]["fn"])

    def test_render_times_out_and_still_closes_the_tab(self):
        run = FakeOpenClaw(reply({"ok": True, "targetId": "q1"}), *[reply({"ok": True, "result": False})] * 40,
                           reply({"ok": True}))
        with self.assertRaisesRegex(BrowserError, "did not render"):
            self.browser(run).render("https://a.test/quiz/1", "1", ".never", timeout_s=10)
        self.assertEqual(run.params[-1]["path"], "/tabs/q1")

    def test_pdf_waits_for_the_ready_element_before_printing(self):
        run = FakeOpenClaw(reply({"ok": True, "targetId": "n1"}), *[reply({"ok": True, "result": False})] * 40,
                           reply({"ok": True}))
        with self.assertRaises(PageNotReady):
            self.browser(run).pdf("https://w.notion.site/p", ".notion-page-content", timeout_s=10)
        self.assertIn('document.querySelector(".notion-page-content")', run.params[1]["body"]["fn"])
        self.assertNotIn("/pdf", [p["path"] for p in run.params])
        self.assertEqual(run.params[-1]["path"], "/tabs/n1")

    def test_pdf_waits_for_steady_text_expands_toggles_and_decodes_the_file(self):
        pdf = "JVBERi0xLjQKJcfs"  # %PDF-1.4
        run = FakeOpenClaw(reply({"ok": True, "targetId": "n1"}), *[reply({"ok": True, "result": n}) for n in
                                                                     (-1, 900, 3500, 3500, 3500)],
                           reply({"ok": True, "result": 0}),
                           payload({"ok": True, "path": "/x.pdf"}, [{"mimeType": "application/pdf", "base64": pdf}]),
                           reply({"ok": True}))
        self.assertTrue(self.browser(run).pdf("https://w.notion.site/p-0123").startswith(b"%PDF-"))
        self.assertEqual(run.params[-2], {"method": "POST", "path": "/pdf", "query": {"profile": "chrome"},
                                          "body": {"targetId": "n1"}})
        self.assertEqual(run.params[-1]["path"], "/tabs/n1")

    def test_pdf_without_a_pdf_file_is_an_error(self):
        run = FakeOpenClaw(reply({"ok": True, "targetId": "n1"}), *[reply({"ok": True, "result": 5})] * 3,
                           reply({"ok": True, "result": 0}),
                           payload({"ok": True}, [{"mimeType": "text/html", "base64": "PGh0bWw+"}]), reply({"ok": True}))
        with self.assertRaisesRegex(BrowserError, "no PDF"):
            self.browser(run).pdf("https://w.notion.site/p")


if __name__ == "__main__":
    unittest.main()
