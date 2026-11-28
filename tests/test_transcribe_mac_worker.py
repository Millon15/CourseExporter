import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from course_exporter.transcribe import d2_pins, mac_worker
from course_exporter.transcribe.mac_worker import Lease, RemoteError, Scratch, Worker

AUDIO = b"OggS fake opus bytes"
AUDIO_SHA = hashlib.sha256(AUDIO).hexdigest()


def lease_json(lease_id="L1", job_id="job-1", **overrides):
    return {"lease_id": lease_id, "job_id": job_id, "expires_at": "2026-11-28T03:00:00Z",
            "audio_sha256": AUDIO_SHA, "audio_bytes": len(AUDIO), "duration_s": 10.0, "language": "uk",
            "producer_config": d2_pins.PRODUCER_CONFIG} | overrides


def result_doc(audio, language, job_id):
    sha = hashlib.sha256(Path(audio).read_bytes()).hexdigest()
    return {
        "schema": "uch-segments/1", "pipeline": "D2", "result_version": 1,
        "producer_config": d2_pins.PRODUCER_CONFIG, "language": language, "speakers": ["S1"],
        "source": {"clip_id": job_id, "audio_sha256": sha, "offset_s": 0, "duration_s": 10.0},
        "segments": [{"start": 0.0, "end": 1.0, "speaker": "S1", "text": "так", "flags": [],
                      "words": [{"start": 0.0, "end": 1.0, "text": "так", "speaker": "S1", "flag": None}]}],
        "run": {"run_id": "r", "host": "h", "warm": False, "peak_rss_mb": None, "stages_s": {"total": 1.0},
                "request_ids": [], "usage": None, "cost_usd": None},
    }


def result_doc_bytes():
    with tempfile.NamedTemporaryFile() as handle:
        handle.write(AUDIO)
        handle.flush()
        return result_doc(handle.name, "uk", "job-1")


class FakeRemote:
    def __init__(self, answers=None, leases=None, audio=AUDIO, submit_errors=(), renew_error=None):
        self.answers = list(answers or [{"state": "ready"}, {"state": "empty"}])
        self.leases = list(leases or [lease_json()])
        self.audio_bytes = audio
        self.submit_errors = list(submit_errors)
        self.renew_error = renew_error
        self.calls = []
        self.submitted = []

    def request(self):
        self.calls.append("request")
        return self.answers.pop(0)

    def lease(self):
        self.calls.append("lease")
        return Lease.parse(self.leases.pop(0)) if self.leases else None

    def audio(self, lease, target):
        self.calls.append("audio")
        target.write_bytes(self.audio_bytes)
        return hashlib.sha256(self.audio_bytes).hexdigest()

    def renew(self, lease):
        self.calls.append("renew")
        if self.renew_error:
            raise RemoteError("renew", self.renew_error, "fenced")

    def submit(self, lease_id, data):
        self.calls.append("submit")
        self.submitted.append((lease_id, data))
        if self.submit_errors:
            raise RemoteError("submit", self.submit_errors.pop(0), "lost")
        doc = json.loads(data)
        return {"receipt_id": "R1", "job_id": doc["source"]["clip_id"],
                "result_sha256": hashlib.sha256(data).hexdigest(), "state": "received"}


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scratch = Scratch(Path(self.tmp.name) / "scratch")
        self.scratch.open()
        self.sleeps = []
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def worker(self, remote, transcriber=result_doc, renew_every=60.0):
        return Worker(remote, self.scratch, transcriber, log=self.lines.append, sleep=self.sleeps.append,
                      renew_every=renew_every)

    def test_one_item_end_to_end_then_an_empty_queue(self):
        remote = FakeRemote()
        self.assertEqual(self.worker(remote).run(), 0)
        self.assertEqual(remote.calls, ["request", "lease", "audio", "submit", "request"])
        lease_id, data = remote.submitted[0]
        self.assertEqual((lease_id, json.loads(data)["source"]["clip_id"]), ("L1", "job-1"))
        self.assertEqual(sorted(p.name for p in self.scratch.root.iterdir()), [])

    def test_waits_while_the_vps_prepares(self):
        remote = FakeRemote(answers=[{"state": "preparing", "retry_after_s": 20}, {"state": "ready"},
                                     {"state": "deferred", "reason": "maintenance window"}])
        self.worker(remote).run()
        self.assertEqual(self.sleeps, [20])
        self.assertEqual(remote.calls.count("submit"), 1)
        self.assertIn("nothing to do: deferred (maintenance window)", self.lines[-1])

    def test_deferred_or_empty_takes_no_lease(self):
        remote = FakeRemote(answers=[{"state": "deferred", "reason": "fleet lock"}])
        self.worker(remote).run()
        self.assertEqual(remote.calls, ["request"])

    def test_once_stops_after_one_item(self):
        remote = FakeRemote(answers=[{"state": "ready"}, {"state": "ready"}], leases=[lease_json(), lease_json()])
        self.worker(remote).run(once=True)
        self.assertEqual(remote.calls.count("submit"), 1)

    def test_refuses_audio_the_lease_does_not_name(self):
        remote = FakeRemote(audio=b"other bytes!")
        with self.assertRaises(RemoteError):
            self.worker(remote).run()
        self.assertNotIn("submit", remote.calls)
        self.assertFalse(self.scratch.audio.exists() or self.scratch.partial.exists())

    def test_reuses_scratch_audio_whose_hash_matches(self):
        self.scratch.write(self.scratch.audio, AUDIO)
        remote = FakeRemote()
        self.worker(remote).run()
        self.assertNotIn("audio", remote.calls)

    def test_resubmits_the_identical_result_after_a_lost_connection(self):
        remote = FakeRemote(submit_errors=[mac_worker.SSH_TRANSPORT_ERROR])
        self.worker(remote).run()
        self.assertEqual(len(remote.submitted), 2)
        self.assertEqual(remote.submitted[0], remote.submitted[1])
        self.assertEqual(self.sleeps, [mac_worker.SUBMIT_RETRY_S])

    def test_keeps_the_result_when_every_submit_fails_and_resubmits_it_on_the_next_start(self):
        failing = FakeRemote(submit_errors=[255, 255, 255])
        with self.assertRaises(RemoteError):
            self.worker(failing).run()
        kept = self.scratch.result.read_bytes()
        remote = FakeRemote(answers=[{"state": "empty"}])
        self.worker(remote).run()
        self.assertEqual(remote.calls, ["submit", "request"])
        self.assertEqual(remote.submitted, [("L1", kept)])
        self.assertFalse(self.scratch.result.exists())

    def test_once_counts_a_recovered_receipt_as_its_item(self):
        self.scratch.write(self.scratch.lease, json.dumps(lease_json()).encode())
        self.scratch.write(self.scratch.result, json.dumps(result_doc_bytes()).encode())
        remote = FakeRemote()
        self.worker(remote).run(once=True)
        self.assertEqual(remote.calls, ["submit"])

    def test_drops_a_kept_result_the_vps_refuses(self):
        self.scratch.write(self.scratch.lease, json.dumps(lease_json()).encode())
        self.scratch.write(self.scratch.result, b"{}")
        remote = FakeRemote(answers=[{"state": "empty"}], submit_errors=[mac_worker.EXIT_LEASE_FENCED])
        self.worker(remote).run()
        self.assertFalse(self.scratch.result.exists())

    def test_a_lease_fenced_during_compute_is_not_submitted(self):
        def slow(audio, language, job_id):
            time.sleep(0.2)
            return result_doc(audio, language, job_id)

        remote = FakeRemote(renew_error=mac_worker.EXIT_LEASE_FENCED)
        with self.assertRaises(mac_worker.LeaseLost):
            self.worker(remote, slow, renew_every=0.02).run()
        self.assertIn("renew", remote.calls)
        self.assertNotIn("submit", remote.calls)
        self.assertTrue(self.scratch.audio.exists())

    def test_renews_while_d2_runs(self):
        def slow(audio, language, job_id):
            time.sleep(0.2)
            return result_doc(audio, language, job_id)

        remote = FakeRemote()
        self.worker(remote, slow, renew_every=0.02).run()
        self.assertGreaterEqual(remote.calls.count("renew"), 2)
        self.assertEqual(remote.calls.count("submit"), 1)


class LeaseTest(unittest.TestCase):
    def test_refuses_a_lease_it_cannot_honour(self):
        cases = {
            "config": lease_json(producer_config="d2-other"),
            "lease id": lease_json(lease_id="L1; rm -rf ~"),
            "sha": lease_json(audio_sha256="xyz"),
            "language": lease_json(language="en"),
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                Lease.parse(payload)


class RemoteTest(unittest.TestCase):
    def test_runs_each_verb_through_the_restricted_alias(self):
        seen = []

        def run(argv, input, capture_output, check):
            seen.append((argv, input))
            return SimpleNamespace(returncode=0, stdout=b'{"lease_id": "L1"}', stderr=b"")

        remote = mac_worker.Remote("jarvis-transcribe", run=run)
        remote.renew(Lease.parse(lease_json()))
        remote.submit("L1", b"{}")
        self.assertEqual(seen[0][0][-3:], ["jarvis-transcribe", "renew", "L1"])
        self.assertIn("BatchMode=yes", seen[0][0])
        self.assertEqual(seen[0][0][1:3], ["-F", str(mac_worker.SSH_CONFIG)])
        self.assertIn("ControlMaster=no", seen[0][0])
        self.assertIn("ControlPath=none", seen[0][0])
        self.assertEqual(seen[1], (seen[1][0], b"{}"))
        self.assertEqual(seen[1][0][-2:], ["submit", "L1"])

    def test_nothing_ready_is_no_lease(self):
        def run(argv, input, capture_output, check):
            return SimpleNamespace(returncode=4, stdout=b"", stderr=b"error: empty: nothing ready")

        self.assertIsNone(mac_worker.Remote(run=run).lease())


class FakeProcess:
    def __init__(self, data):
        self.stdout, self.stderr = io.BytesIO(data), io.BytesIO(b"")
        self.returncode = 0
        self.killed = False

    def kill(self):
        self.killed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RemoteAudioTest(unittest.TestCase):
    def test_stops_before_writing_past_the_lease_size(self):
        process = FakeProcess(AUDIO + b"x" * (mac_worker.CHUNK + 1))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "item.opus.part"
            remote = mac_worker.Remote(popen=lambda *a, **k: process)
            with self.assertRaises(RemoteError):
                remote.audio(Lease.parse(lease_json()), target)
            self.assertTrue(process.killed)
            self.assertLessEqual(target.stat().st_size, len(AUDIO))

    def test_never_writes_through_a_planted_link(self):
        for make in (lambda link, outside: link.symlink_to(outside), lambda link, outside: os.link(outside, link)):
            with self.subTest(make), tempfile.TemporaryDirectory() as tmp:
                outside = Path(tmp) / "outside"
                outside.write_bytes(b"keep")
                part = Path(tmp) / "item.opus.part"
                make(part, outside)
                remote = mac_worker.Remote(popen=lambda *a, **k: FakeProcess(AUDIO))
                remote.audio(Lease.parse(lease_json()), part)
                self.assertEqual(outside.read_bytes(), b"keep")
                self.assertEqual((part.read_bytes(), part.lstat().st_nlink), (AUDIO, 1))


class IdentityTest(unittest.TestCase):
    """Runs the real `ssh -G`, which resolves the config without touching the network."""

    def check(self, extra=""):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            config.write_text(f"Host jarvis-transcribe\n  HostName 127.0.0.1\n  User openclaw\n"
                              f"  IdentityFile {tmp}/restricted\n  IdentitiesOnly yes\n  IdentityAgent none\n{extra}")
            mac_worker.Remote("jarvis-transcribe", config, Path(tmp) / "restricted").check_identity()

    def test_accepts_exactly_the_restricted_identity(self):
        self.check()

    def test_refuses_a_wildcard_identity_or_an_agent(self):
        for extra in (f"Host *\n  IdentityFile ~/.ssh/operator\n", "Host *\n  IdentityAgent /tmp/agent.sock\n"):
            with self.subTest(extra), self.assertRaises(ValueError):
                with tempfile.TemporaryDirectory() as tmp:
                    config = Path(tmp) / "config"
                    config.write_text(f"Host jarvis-transcribe\n  HostName 127.0.0.1\n  IdentityFile {tmp}/r\n"
                                      f"  IdentitiesOnly yes\n{extra}")
                    mac_worker.Remote("jarvis-transcribe", config).check_identity()

    def test_refuses_a_single_identity_that_is_not_the_restricted_key(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            config = Path(tmp) / "config"
            config.write_text(f"Host jarvis-transcribe\n  HostName 127.0.0.1\n  IdentityFile {tmp}/operator\n"
                              "  IdentitiesOnly yes\n  IdentityAgent none\n")
            mac_worker.Remote("jarvis-transcribe", config, Path(tmp) / "restricted").check_identity()

    def test_refuses_a_config_without_an_identity_file(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            config = Path(tmp) / "config"
            config.write_text("Host jarvis-transcribe\n  HostName 127.0.0.1\n  IdentitiesOnly yes\n  IdentityAgent none\n")
            mac_worker.Remote("jarvis-transcribe", config).check_identity()


class ScratchTest(unittest.TestCase):
    def test_refuses_a_foreign_entry_or_a_link(self):
        for name, make in (("notes.txt", lambda p: p.write_text("x")),
                           ("item.result.json.tmp", lambda p: p.symlink_to("/etc/hosts"))):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                scratch = Scratch(Path(tmp) / "scratch")
                scratch.open()
                make(scratch.root / name)
                with self.assertRaises(ValueError):
                    scratch.open()

    def test_a_link_planted_after_open_is_replaced_never_written_through(self):
        for make in (lambda link, outside: link.symlink_to(outside), lambda link, outside: os.link(outside, link)):
            for name in ("item.result.json.tmp", "item.result.json"):
                with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                    outside = Path(tmp) / "outside"
                    outside.write_bytes(b"keep")
                    scratch = Scratch(Path(tmp) / "scratch")
                    scratch.open()
                    make(scratch.root / name, outside)
                    scratch.write(scratch.result, b"{}")
                    self.assertEqual(outside.read_bytes(), b"keep")
                    self.assertEqual(scratch.result.read_bytes(), b"{}")

    def test_refuses_a_hard_linked_or_shared_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside"
            outside.write_bytes(b"keep")
            scratch = Scratch(Path(tmp) / "scratch")
            scratch.open()
            os.link(outside, scratch.audio)
            with self.assertRaises(ValueError):
                scratch.open()
            scratch.audio.unlink()
            scratch.write(scratch.lease, b"{}")
            os.chmod(scratch.lease, 0o644)
            with self.assertRaises(ValueError):
                scratch.open()

    def test_the_lock_must_be_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Scratch(Path(tmp) / "scratch")
            scratch.open()
            outside = Path(tmp) / "outside"
            outside.write_bytes(b"")
            os.link(outside, scratch.root / ".lock")
            with self.assertRaises(ValueError), scratch.locked():
                pass

    def test_clear_removes_only_its_own_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Scratch(Path(tmp) / "scratch")
            scratch.open()
            scratch.write(scratch.result, b"{}")
            scratch.write(scratch.audio, AUDIO)
            scratch.clear()
            self.assertEqual(sorted(p.name for p in scratch.root.iterdir()), [])

    def test_refuses_a_directory_other_users_can_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "scratch"
            root.mkdir()
            os.chmod(root, 0o755)
            with self.assertRaises(ValueError):
                Scratch(root).open()

    def test_one_worker_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Scratch(Path(tmp) / "scratch")
            scratch.open()
            with scratch.locked() as first, scratch.locked() as second:
                self.assertEqual((first, second), (True, False))

    def test_files_it_writes_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Scratch(Path(tmp) / "scratch")
            scratch.open()
            scratch.write(scratch.result, b"{}")
            self.assertEqual(scratch.result.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
