import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from course_exporter.transcribe import source
from course_exporter.transcribe.catalog import VideoAsset

FFMPEG = shutil.which("ffmpeg") or shutil.which("ffmpeg", path=os.path.expanduser("~/.brew/bin"))
SECRET = "SECRET-REFRESH"


def asset(**overrides):
    fields = dict(provider="kwiga", course="kwiga-smm", position=1, lesson_id="l", lesson_title="T", asset_id="a",
                  lesson_dir="001-урок", name="v.mp4", bytes=10, md5="aa", duration=1.0)
    return VideoAsset(**{**fields, **overrides})


def drive_file(data=b"", **overrides):
    fields = dict(id="F1", name="v.mp4", size=len(data), md5=hashlib.md5(data).hexdigest(), revision="r1",
                  parents=("P",))
    return source.DriveFile(**{**fields, **overrides})


def done(code=0, stdout=""):
    return subprocess.CompletedProcess([], code, stdout, "")


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status, self.headers = status, headers or {}


class FakeApi:
    """Serves one file's bytes, honouring `bytes=a-b` and `bytes=a-` ranges."""

    def __init__(self, data, meta):
        self.data, self.meta, self.ranges = data, meta, []

    def metadata(self, file_id):
        return self.meta

    def media(self, file_id, byte_range=None):
        self.ranges.append(byte_range)
        if not byte_range:
            return Response(self.data, 200, {"Content-Length": str(len(self.data)), "Accept-Ranges": "bytes"})
        first, _, last = byte_range.removeprefix("bytes=").partition("-")
        first, last = int(first), int(last) if last else len(self.data) - 1
        body = self.data[first:last + 1]
        return Response(body, 206, {"Content-Length": str(len(body)), "Accept-Ranges": "bytes",
                                    "Content-Range": f"bytes {first}-{first + len(body) - 1}/{len(self.data)}"})


class ResolveTest(unittest.TestCase):
    FOLDERS = [{"Name": "001-урок", "ID": "P"}, {"Name": "002-урок", "ID": "Q"}]

    def listing(self, *entries, folders=FOLDERS):
        return lambda args, **kw: done(stdout=json.dumps(folders if "--dirs-only" in args else list(entries)))

    def entry(self, file_id, name="v.mp4", size=10, md5="AA"):
        return {"Name": name, "Size": size, "Hashes": {"md5": md5}, "ID": file_id}

    def test_pins_the_one_matching_object(self):
        api = FakeApi(b"", drive_file(id="F2", size=10, md5="aa"))
        run = self.listing(self.entry("F1", name="other.mp4"), self.entry("F2"), self.entry("F3", size=11))
        self.assertEqual(source.resolve(asset(), "gdrive:Courses", api, run=run).id, "F2")

    def test_refuses_none_or_two_matches(self):
        api = FakeApi(b"", drive_file(size=10, md5="aa"))
        for entries in ([], [self.entry("F1"), self.entry("F2")]):
            with self.subTest(len(entries)), self.assertRaisesRegex(source.SourceError, "expected exactly one"):
                source.resolve(asset(), "gdrive:Courses", api, run=self.listing(*entries))

    def test_refuses_an_object_changed_after_listing(self):
        api = FakeApi(b"", drive_file(id="F1", size=10, md5="bb"))
        with self.assertRaisesRegex(source.SourceError, "changed"):
            source.resolve(asset(), "gdrive:Courses", api, run=self.listing(self.entry("F1")))

    def test_lists_the_course_then_the_lesson_folder(self):
        calls = []
        api = FakeApi(b"", drive_file(size=10, md5="aa"))
        listed = self.listing(self.entry("F1"))
        source.resolve(asset(), "gdrive:Courses", api, run=lambda args, **kw: calls.append(args) or listed(args))
        self.assertEqual([c[-1] for c in calls], ["gdrive:Courses/kwiga-smm", "gdrive:Courses/kwiga-smm/001-урок"])

    def test_refuses_a_file_whose_parent_is_not_the_lesson_folder(self):
        api = FakeApi(b"", drive_file(size=10, md5="aa", parents=("other",)))
        with self.assertRaisesRegex(source.SourceError, "no longer in"):
            source.resolve(asset(), "gdrive:Courses", api, run=self.listing(self.entry("F1")))

    def test_refuses_a_missing_or_doubled_lesson_folder(self):
        api = FakeApi(b"", drive_file(size=10, md5="aa"))
        for folders in ([], [{"Name": "001-урок", "ID": "P"}, {"Name": "001-урок", "ID": "P2"}]):
            with self.subTest(len(folders)), self.assertRaisesRegex(source.SourceError, "Drive folders named"):
                source.resolve(asset(), "gdrive:Courses", api, run=self.listing(self.entry("F1"), folders=folders))


class TokenTest(unittest.TestCase):
    def config(self, **overrides):
        remote = {"type": "drive", "client_id": "cid", "client_secret": "cs",
                  "token": json.dumps({"refresh_token": SECRET}), **overrides}
        return lambda args, **kw: done(stdout=json.dumps({"gdrive": remote}))

    def test_refreshes_once_then_again_near_expiry(self):
        now, grants = [0.0], []

        def opener(request, timeout):
            grants.append(request.data.decode())
            return Response(json.dumps({"access_token": f"t{len(grants)}", "expires_in": 3600}).encode())

        token = source.RcloneToken("gdrive:Courses", run=self.config(), opener=opener, clock=lambda: now[0])
        self.assertEqual((token.bearer(), token.bearer()), ("t1", "t1"))
        now[0] = 3600 - 60
        self.assertEqual(token.bearer(), "t2")
        self.assertIn("grant_type=refresh_token", grants[0])

    def test_refusals_never_carry_the_secret(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(source.TOKEN_URL, 400, "bad " + SECRET, {}, None)

        cases = {"no own client": (self.config(client_id=""), opener),
                 "dump failed": (lambda args, **kw: done(code=1, stdout=SECRET), opener),
                 "refresh rejected": (self.config(), opener)}
        for name, (run, open_) in cases.items():
            with self.subTest(name), self.assertRaises(source.SourceError) as caught:
                source.RcloneToken("gdrive:", run=run, opener=open_).bearer()
            self.assertNotIn(SECRET, str(caught.exception))


class DriveApiTest(unittest.TestCase):
    class Token:
        def bearer(self):
            return "tok"

    def test_retries_throttling_and_sends_the_bearer_header(self):
        attempts = []

        def opener(request, timeout):
            attempts.append(request.get_header("Authorization"))
            if len(attempts) < 3:
                raise urllib.error.HTTPError(request.full_url, 429, "slow down", {}, None)
            return Response(json.dumps({"id": "F", "name": "v.mp4", "size": "3", "md5Checksum": "AB",
                                        "headRevisionId": "r", "parents": ["P"]}).encode())

        api = source.DriveApi(self.Token(), opener=opener, sleep=lambda s: None)
        self.assertEqual(api.metadata("F"), source.DriveFile("F", "v.mp4", 3, "ab", "r", ("P",)))
        self.assertEqual(attempts, ["Bearer tok"] * 3)

    def test_refuses_a_trashed_file_and_a_permanent_error(self):
        def trashed(request, timeout):
            return Response(json.dumps({"trashed": True}).encode())

        def denied(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "no", {}, None)


        for opener in (trashed, denied):
            with self.assertRaises(source.SourceError):
                source.DriveApi(self.Token(), opener=opener, sleep=lambda s: None).metadata("F")


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class AudioCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "cache"

    def tearDown(self):
        self.tmp.cleanup()

    def video(self, *movflags):
        path = Path(self.tmp.name) / f"v{len(movflags)}.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=4", "-f", "lavfi", "-i", "testsrc=s=320x240:d=4",
                        "-shortest", "-c:v", "mpeg4", "-q:v", "2", "-c:a", "aac", *movflags, str(path)], check=True)
        return path.read_bytes()

    def cache(self, data, **meta):
        api = FakeApi(data, drive_file(data, **meta))
        return api, source.AudioCache(self.root, api, ffmpeg=FFMPEG, min_free=0)

    def test_streams_a_front_indexed_mp4_once_and_reuses_it(self):
        data = self.video("-movflags", "+faststart")
        api, cache = self.cache(data)
        audio = cache.audio(drive_file(data), 4.0)
        self.assertEqual((audio.read_path, api.ranges), ("stream", [None]))
        self.assertAlmostEqual(audio.seconds, 4.0, delta=0.2)
        self.assertEqual(oct(self.root.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((self.root / f"{cache._key(audio.source)}.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(cache.audio(drive_file(data), 4.0).sha256, audio.sha256)
        self.assertEqual(api.ranges, [None])

    def test_falls_back_to_a_seekable_read_for_a_tail_indexed_mp4(self):
        data = self.video()
        api, cache = self.cache(data)
        audio = cache.audio(drive_file(data), 4.0)
        self.assertEqual(audio.read_path, "range")
        self.assertTrue(any(r for r in api.ranges if r))
        self.assertAlmostEqual(audio.seconds, 4.0, delta=0.2)

    def test_the_proxy_serves_only_its_secret_path_in_bounded_upstream_ranges(self):
        data = b"0123456789"
        api = FakeApi(data, drive_file(data))
        with source._loopback_proxy(api, drive_file(data), window=3) as url:
            base = url.rsplit("/", 2)[0]
            for wrong in (f"{base}/source.mp4", f"{base}/x/source.mp4"):
                with self.subTest(wrong), self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(wrong, timeout=10)
                self.assertEqual(caught.exception.code, 404)
                caught.exception.close()
            request = urllib.request.Request(url, headers={"Range": "bytes=2-"})
            with urllib.request.urlopen(request, timeout=10) as reply:
                self.assertEqual((reply.status, reply.headers["Content-Range"], reply.read()),
                                 (206, "bytes 2-9/10", b"23456789"))
            with urllib.request.urlopen(url, timeout=10) as reply:
                self.assertEqual((reply.status, reply.read()), (200, data))
        self.assertEqual(api.ranges[:3], ["bytes=2-4", "bytes=5-7", "bytes=8-9"])
        self.assertTrue(all(r and int(r.split("-")[1]) - int(r[6:].split("-")[0]) < 3 for r in api.ranges))

    def test_refuses_partial_response_with_an_incorrect_status_or_content_range(self):
        video = drive_file(b"01234")
        for status, header in ((200, "bytes 0-2/5"), (206, "bytes 1-3/5"), (206, "bytes 0-2/6")):
            with self.subTest(status=status, content_range=header):
                handler = object.__new__(source._ProxyHandler)
                handler.source = video
                handler.window = 3
                handler.check_headroom = None
                handler.api = mock.Mock()
                handler.api.media.return_value = Response(b"012", status, {"Content-Range": header})
                handler.wfile = io.BytesIO()
                with self.assertRaisesRegex(source.SourceError, "mismatched Content-Range"):
                    handler._relay(0, 2)
                self.assertEqual(handler.wfile.getvalue(), b"")

    def test_cache_binds_revision_config_and_audio_byte_count(self):
        data = self.video("-movflags", "+faststart")
        api, cache = self.cache(data)
        original = drive_file(data)
        first = cache.audio(original, 4.0)
        receipt_path = first.path.with_suffix(".json")
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["bytes"], first.path.stat().st_size)
        self.assertEqual(receipt["extraction_config"], source.EXTRACTION_CONFIG)
        receipt["bytes"] -= 1
        receipt_path.write_text(json.dumps(receipt))
        cache.audio(original, 4.0)
        revised = drive_file(data, revision="r2")
        api.meta = revised
        self.assertNotEqual(cache.audio(revised, 4.0).path, first.path)
        self.assertEqual(api.ranges, [None, None, None])

    def test_stream_rechecks_revision_before_committing_cache(self):
        data = self.video("-movflags", "+faststart")
        api, cache = self.cache(data, revision="r2")
        with self.assertRaisesRegex(source.SourceChanged, "after the audio read"):
            cache.audio(drive_file(data), 4.0)
        self.assertEqual(list(self.root.iterdir()), [])


    def test_range_header_spans(self):
        cases = {None: (0, 9), "bytes=0-": (0, 9), "bytes=3-5": (3, 5), "bytes=8-99": (8, 9), "bytes=-4": (6, 9),
                 "bytes=10-": None, "bytes=5-3": None, "bytes=0-1,4-5": None, "items=0-1": None, "bytes=x-": None}
        for header, span in cases.items():
            with self.subTest(header):
                self.assertEqual(source._requested_span(header, 10), span)

    def test_the_seekable_read_keeps_the_capability_out_of_ffmpeg_argv(self):
        data = self.video()
        api, cache = self.cache(data)
        launched = []
        real_popen = subprocess.Popen

        def spy(argv, **kwargs):
            launched.append(argv)
            return real_popen(argv, **kwargs)

        with mock.patch.object(source.subprocess, "Popen", spy):
            cache._transcode_seekable(drive_file(data), Path(self.tmp.name) / "seek.opus")
        self.assertTrue(any("concat" in argv for argv in launched))
        self.assertFalse(any("127.0.0.1" in arg for argv in launched for arg in argv))

    def test_a_seekable_read_refuses_a_file_moved_meanwhile(self):
        data = self.video()
        api, cache = self.cache(data, parents=("elsewhere",))
        self.root.mkdir(mode=0o700)
        with self.assertRaisesRegex(source.SourceError, "changed during the seekable read"):
            cache._transcode_seekable(drive_file(data), self.root / "seek.opus")

    def test_discards_audio_whose_stream_differs_from_the_pinned_md5(self):
        data = self.video("-movflags", "+faststart")
        _, cache = self.cache(data)
        with self.assertRaisesRegex(source.SourceError, "differ"):
            cache.audio(drive_file(data, md5="0" * 32), 4.0)
        self.assertEqual([p.name for p in self.root.iterdir()], [])

    def test_refuses_to_start_below_the_free_space_floor(self):
        data = self.video("-movflags", "+faststart")
        api = FakeApi(data, drive_file(data))
        with self.assertRaises(source.LowSpace):
            source.AudioCache(self.root, api, ffmpeg=FFMPEG, min_free=1 << 62).audio(drive_file(data), 4.0)

    def test_a_decode_shorter_than_the_video_is_a_failure(self):
        data = self.video("-movflags", "+faststart")
        _, cache = self.cache(data)
        with self.assertRaisesRegex(source.TranscodeError, "decoded"):
            cache.audio(drive_file(data), 60.0)

    def test_cut_starts_at_the_offset_and_lasts_the_requested_time(self):
        data = self.video("-movflags", "+faststart")
        _, cache = self.cache(data)
        audio = cache.audio(drive_file(data), 4.0).path
        clip = source.cut(audio, 1.0, 2.0, Path(self.tmp.name) / "clip.opus", FFMPEG)
        self.assertAlmostEqual(source._duration(FFMPEG, clip), 2.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
