import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from course_exporter import media
from course_exporter.model import Asset, Media, UnsupportedMedia

SIGNED = "https://media.example.test/v.mp4?expires=1&signature=SECRET"
VIDEO_PLAYLIST = "https://media.example.test/v720/index.m3u8?sig=SECRET"
AUDIO_PLAYLIST = "https://media.example.test/audio/index.m3u8?sig=SECRET"
PROXY = "http://127.0.0.1:9"


def done(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class Recorder:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return self.results.pop(0)


class FakeProcess:
    pid = 999_999_999  # above any pid_max, so the group signal finds nothing

    def __init__(self, target, code, polls_before_exit):
        self.target, self.code, self.polls, self.returncode, self.terminated = target, code, polls_before_exit, None, False

    def poll(self):
        if self.polls <= 0 and self.returncode is None:
            if self.target is not None:
                self.target.write_bytes(b"merged")
            self.returncode = self.code
        self.polls -= 1
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


class FakePopen:
    def __init__(self, target=None, code=0, polls_before_exit=0):
        self.target, self.code, self.polls, self.calls, self.process, self.loaded = target, code, polls_before_exit, [], None, None

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        self.loaded = json.loads(Path(args[args.index("--load-info-json") + 1]).read_text())
        self.process = FakeProcess(self.target, self.code, self.polls)
        return self.process


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self.body[:size] if size >= 0 else self.body


def opener_for(pages):
    def open_url(request, timeout=0):
        return FakeResponse(pages[request.full_url].encode())
    return open_url


def selection(*formats):
    return done(stdout=json.dumps({"id": "v", "requested_formats": list(formats)}))


def hls(url):
    return {"protocol": "m3u8_native", "url": url}


class MediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.asset = Asset(asset_id="v1", kind="video", title="Welcome.mp4", ref="1001", expected_seconds=776)
        self.clear = opener_for({VIDEO_PLAYLIST: "#EXTM3U\n#EXT-X-KEY:METHOD=NONE\nseg1.ts\n",
                                 AUDIO_PLAYLIST: "#EXTM3U\nseg1.aac\n"})
        patcher = mock.patch.object(media, "WATCH_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def download(self, run, popen, opener=None, **kwargs):
        return media.download(Media(kind="hls", url=SIGNED, headers={"Referer": "https://kwiga.test/"}),
                              self.asset, self.dir / "v1", 720, run=run, opener=opener or self.clear,
                              proxy_url=PROXY, popen=popen, **kwargs)

    def test_selected_formats_are_checked_then_downloaded_through_the_proxy_without_urls_in_argv(self):
        run = Recorder(selection(hls(VIDEO_PLAYLIST), hls(AUDIO_PLAYLIST)))
        popen = FakePopen(self.dir / "v1.mp4")
        self.assertEqual(self.download(run, popen), self.dir / "v1.mp4")
        select_args, select_kwargs = run.calls[0]
        self.assertEqual(select_kwargs["input"], SIGNED + "\n")
        self.assertIn("bv*[height<=?720]+ba/b[height<=?720]", select_args)
        for args in (select_args, popen.calls[0]):
            self.assertNotIn("SECRET", " ".join(args))
            self.assertEqual(args[args.index("--proxy") + 1], PROXY)
        self.assertEqual(popen.calls[0][popen.calls[0].index("--downloader") + 1], "native")
        self.assertEqual([f["url"] for f in popen.loaded["requested_formats"]], [VIDEO_PLAYLIST, AUDIO_PLAYLIST])
        self.assertEqual(list(self.dir.glob(".info-*")), [], "the signed info file is gone after the run")

    def test_encryption_in_any_selected_playlist_is_unsupported_before_download(self):
        for audio in ("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"k\"\nseg.aac\n",
                      "#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT=\"com.apple.streamingkeydelivery\"\n"):
            popen = FakePopen()
            opener = opener_for({VIDEO_PLAYLIST: "#EXTM3U\nseg1.ts\n", AUDIO_PLAYLIST: audio})
            with self.assertRaises(UnsupportedMedia):
                self.download(Recorder(selection(hls(VIDEO_PLAYLIST), hls(AUDIO_PLAYLIST))), popen, opener)
            self.assertEqual(popen.calls, [])
        with self.assertRaises(UnsupportedMedia):
            self.download(Recorder(selection({"protocol": "https", "url": SIGNED, "has_drm": True})), FakePopen())

    def test_formats_outside_the_proxied_protocols_are_unsupported(self):
        for fmt in ({"protocol": "m3u8", "url": VIDEO_PLAYLIST}, {"protocol": "rtmp", "url": "rtmp://x/y"},
                    {"protocol": "https", "url": "http://media.example.test/v.mp4"}):
            popen = FakePopen()
            with self.assertRaises(UnsupportedMedia, msg=fmt["protocol"]):
                self.download(Recorder(selection(fmt)), popen)
            self.assertEqual(popen.calls, [])

    def test_no_proxy_cannot_bypass_the_mandatory_proxy(self):
        import urllib.request
        handler = media._MandatoryProxy({"https": "http://127.0.0.1:9"})
        request = urllib.request.Request("https://cdn.test/x")
        with mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}):
            handler.proxy_open(request, "http://127.0.0.1:9", "https")
        self.assertEqual(request._tunnel_host, "cdn.test")
        self.assertEqual(request.host, "127.0.0.1:9")
        with mock.patch.dict(os.environ, {"no_proxy": "*", "HTTPS_PROXY": "http://evil:1"}):
            self.assertFalse({"no_proxy", "HTTPS_PROXY"} & set(media.downloader_env()))

    def test_the_proxied_opener_has_no_file_ftp_or_data_handler_and_refuses_plain_redirects(self):
        opener = media.proxied_opener("http://127.0.0.1:9").__self__
        schemes = {name.split("_")[0] for h in opener.handlers for name in dir(h) if name.endswith("_open")}
        self.assertFalse(schemes & {"file", "ftp", "data"}, schemes)
        redirects = media._HttpsOnlyRedirects()
        with self.assertRaises(UnsupportedMedia):
            redirects.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/latest")

    def test_the_height_cap_has_no_uncapped_fallback(self):
        run = Recorder(done(1, stderr="ERROR: Requested format is not available"))
        with self.assertRaises(media.DownloadError):
            self.download(run, FakePopen())
        args = run.calls[0][0]
        self.assertEqual(args[args.index("-f") + 1], "bv*[height<=?720]+ba/b[height<=?720]")

    def test_a_floor_breach_stops_the_transfer(self):
        popen = FakePopen(polls_before_exit=10)
        with self.assertRaises(media.Aborted) as caught:
            self.download(Recorder(selection(hls(VIDEO_PLAYLIST))), popen, abort=lambda: "3.0 GiB free, floor 20")
        self.assertIn("floor 20", str(caught.exception))

    def test_an_abort_stops_the_whole_process_group(self):
        pidfile = self.dir / "child.pid"
        script = f"sleep 60 & echo $! > {pidfile}; wait"
        polls = iter([None, None, "stop requested"])

        def abort():
            return next(polls, "stop requested") if pidfile.exists() else None

        with self.assertRaises(media.Aborted):
            media._run_watched(["bash", "-c", script], abort, subprocess.Popen)
        child = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_a_child_that_ignores_sigterm_is_killed_after_the_parent_exits(self):
        pidfile = self.dir / "stubborn.pid"
        script = f"(trap '' TERM; while :; do sleep 0.1; done) & echo $! > {pidfile}; wait"
        process = subprocess.Popen(["bash", "-c", script], start_new_session=True)
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            import time
            time.sleep(0.05)
        child = int(pidfile.read_text())
        media._stop_group(process, grace_seconds=1)
        import time
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_non_https_media_is_unsupported(self):
        with self.assertRaises(UnsupportedMedia):
            media.download(Media(kind="hls", url="http://media.example.test/v.m3u8"), self.asset, self.dir / "v1", 720)

    def test_a_finished_file_from_an_earlier_run_is_reused(self):
        (self.dir / "v1.mp4").write_bytes(b"done")
        run = Recorder()
        self.assertEqual(self.download(run, FakePopen()), self.dir / "v1.mp4")
        self.assertEqual(run.calls, [])

    def test_verify_needs_video_audio_and_the_expected_duration(self):
        path = self.dir / "v1.mp4"
        path.write_bytes(b"x" * 10)

        def probe(streams, duration):
            return Recorder(done(stdout=json.dumps({"streams": [{"codec_type": k} for k in streams],
                                                    "format": {"duration": duration}})))

        verified = media.verify(path, "video", expected_seconds=776, run=probe(["video", "audio"], "776.32"))
        self.assertEqual((verified.bytes, verified.duration), (10, 776.32))
        for streams, duration in ((["video"], "776"), (["audio"], "776"), (["video", "audio"], "0"),
                                  (["video", "audio"], "400")):
            with self.assertRaises(media.VerificationError, msg=f"{streams} {duration}"):
                media.verify(path, "video", expected_seconds=776, run=probe(streams, duration))

    def test_decode_errors_fail_verification(self):
        path = self.dir / "v1.mp4"
        path.write_bytes(b"x")
        probe = {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}], "format": {"duration": "1"}}
        run = Recorder(done(stdout=json.dumps(probe)), done(0, stderr="corrupt frame"))
        with self.assertRaises(media.VerificationError):
            media.verify(path, "video", decode=True, run=run)
        self.assertIn("-protocol_whitelist", run.calls[1][0])

    def test_set_aside_keeps_the_bytes_under_a_rejected_name(self):
        path = self.dir / "v1.mp4"
        path.write_bytes(b"bad")
        self.assertEqual(media.set_aside(path).read_bytes(), b"bad")
        self.assertFalse(path.exists())

    def test_repr_and_redaction_hide_signed_urls(self):
        self.assertNotIn("SECRET", repr(Media(kind="hls", url=SIGNED, headers={"X": "SECRET"})))
        self.assertEqual(media.redact(f"failed for {SIGNED} at hls://x/y"), "failed for <url> at <url>")


class PublicProxyTest(unittest.TestCase):
    def connect(self, proxy, request):
        port = int(proxy.url.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(request)
            return client.recv(256)

    def test_private_hosts_other_ports_and_plain_requests_are_refused(self):
        asked = []

        def resolve(host):
            asked.append(host)
            return []

        with media.PublicProxy(resolve=resolve) as proxy:
            for request in (b"CONNECT metadata.test:443 HTTP/1.1\r\n\r\n", b"CONNECT cdn.test:80 HTTP/1.1\r\n\r\n",
                            b"GET http://cdn.test/ HTTP/1.1\r\n\r\n"):
                self.assertTrue(self.connect(proxy, request).startswith(b"HTTP/1.1 403"), request)
        self.assertEqual(asked, ["metadata.test"])
        self.assertEqual(len(proxy.refused), 3)

    def test_public_addresses_rejects_any_private_answer(self):
        def answers(*ips):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]

        with mock.patch.object(socket, "getaddrinfo", return_value=answers("93.184.216.34")):
            self.assertEqual(media.public_addresses("cdn.test"), ["93.184.216.34"])
        for ips in (("169.254.169.254",), ("127.0.0.1",), ("93.184.216.34", "10.0.0.5")):
            with mock.patch.object(socket, "getaddrinfo", return_value=answers(*ips)):
                self.assertEqual(media.public_addresses("x.test"), [], ips)


if __name__ == "__main__":
    unittest.main()
