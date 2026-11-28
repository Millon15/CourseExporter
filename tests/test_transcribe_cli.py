import tempfile
import unittest
from pathlib import Path

from course_exporter import cli, config
from course_exporter.transcribe import cli as transcribe


class OperatorCliTest(unittest.TestCase):
    def test_top_level_dispatches_to_transcriber_without_changing_export(self):
        parser = cli._parser(config.Settings())
        self.assertIs(parser.parse_args(["transcribe", "status"]).command, transcribe.dispatch)
        self.assertIs(parser.parse_args(["status", "--out", "/tmp/example"]).command, cli._status)

    def test_source_identity_and_course_allowlist_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "course").mkdir()
            path = root / "config.toml"
            common = f'staging_root = "{root}"\ncourses_root_id="root"\n'
            path.write_text(common + 'source_remote="reader:Courses"\n[courses]\ncourse="uk"\n')
            settings = transcribe.Settings.load(path)
            self.assertEqual(settings.courses["course"], transcribe.Course(root / "course", "uk"))
            path.write_text(common + 'source_remote="reader:Elsewhere"\n[courses]\ncourse="uk"\n')
            with self.assertRaisesRegex(ValueError, "Courses"):
                transcribe.Settings.load(path)
