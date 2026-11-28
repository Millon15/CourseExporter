import io
import json
import unittest
from unittest import mock

from course_exporter.transcribe import ssh_verbs
from tests.test_transcribe_journal import JournalFixture, document


class VerbsTest(JournalFixture, unittest.TestCase):
    def call(self, command, data=b""):
        output, errors = io.BytesIO(), io.StringIO()
        code = ssh_verbs.serve(command, lambda: self.db, self.root, lambda: None, io.BytesIO(data), output, errors)
        return code, output.getvalue(), errors.getvalue()

    def test_stream_renew_submit_and_lost_reply(self):
        self.pilot()
        self.ready()
        code, line, _ = self.call("lease")
        self.assertEqual(code, 0)
        lease = json.loads(line)["lease_id"]
        self.assertEqual(self.call(f"audio {lease}"), (0, b"synthetic opus", ""))
        self.assertEqual(self.call(f"renew {lease}")[0], 0)
        data = json.dumps(document(self.db.leased_job(lease))).encode()
        receipt = self.call(f"submit {lease}", data)
        self.assertEqual(receipt[0], 0, receipt)
        self.now += 1000
        self.assertEqual(self.call(f"submit {lease}", data), receipt)
        self.assertEqual(self.call(f"submit {lease}", b"{}")[0], 5)

    def test_non_verbs_and_upload_limit(self):
        for entry in ("bash", "status ;", "status\n", "audio -x", "request x", "status --alert"):
            self.assertEqual(self.call(entry)[0], 2)
        with mock.patch.object(ssh_verbs, "MAX_RESULT_BYTES", 20):
            self.assertEqual(self.call("submit " + "f" * 64, b" " * 21)[0], 7)

    def test_config_and_length_fence(self):
        self.pilot()
        job = self.ready()
        _, lease_data, _ = self.call("lease")
        lease = json.loads(lease_data)["lease_id"]
        leased = self.db.leased_job(lease)
        for key, bad in (("clip_id", "other"), ("duration_s", 1e2)):
            with self.subTest(key=key):
                doc = document(leased)
                doc["source"][key] = bad
                with self.assertRaises(ValueError):
                    ssh_verbs._validate(leased, json.dumps(doc).encode())
        self.assertEqual(self.call(f"submit {lease}", json.dumps(doc).encode())[0], 6)
        self.assertEqual(self.db.status()["counts"]["failed"], 1)
        self.assertNotEqual(self.db.next_prepare().id, job.id)

    def test_audio_mutation_does_not_stream(self):
        self.pilot()
        self.ready()
        lease = self.db.lease()["lease_id"]
        self.audio_path.write_bytes(b"SYNTHETIC OPUS")
        self.assertEqual(self.call(f"audio {lease}")[0:2], (1, b""))
