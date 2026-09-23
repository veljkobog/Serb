import os
import tempfile
import unittest
from pathlib import Path

from odte.env import load_env, masked, parse_env, write_env


class TestParse(unittest.TestCase):
    def test_reads_plain_export_and_quoted_forms(self):
        text = (
            "# a comment\n"
            "\n"
            'export TRADIER_TOKEN="abc123"\n'
            "TRADIER_ENV = production \n"
            "OTHER='x y'\n"
            "not a pair\n"
        )
        self.assertEqual(
            parse_env(text),
            {"TRADIER_TOKEN": "abc123", "TRADIER_ENV": "production", "OTHER": "x y"},
        )


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("TRADIER_TOKEN", "TRADIER_ENV")}
        for k in self.saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_file_values_land_in_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_env(Path(tmp) / ".env", {"TRADIER_TOKEN": "filetoken", "TRADIER_ENV": "sandbox"})
            load_env([path])
        self.assertEqual(os.environ["TRADIER_TOKEN"], "filetoken")
        self.assertEqual(os.environ["TRADIER_ENV"], "sandbox")

    def test_a_real_environment_variable_wins(self):
        os.environ["TRADIER_TOKEN"] = "from-shell"
        with tempfile.TemporaryDirectory() as tmp:
            path = write_env(Path(tmp) / ".env", {"TRADIER_TOKEN": "from-file"})
            loaded = load_env([path])
        self.assertEqual(os.environ["TRADIER_TOKEN"], "from-shell")
        self.assertEqual(loaded["TRADIER_TOKEN"], "from-file")

    def test_missing_and_unreadable_files_are_ignored(self):
        self.assertEqual(load_env([Path("/nope/.env")]), {})


class TestWrite(unittest.TestCase):
    def test_written_file_is_owner_only_and_skips_blanks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_env(Path(tmp) / "sub" / ".env", {"TRADIER_TOKEN": "t", "EMPTY": ""})
            text = path.read_text()
            self.assertIn("TRADIER_TOKEN=t", text)
            self.assertNotIn("EMPTY", text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_masked_never_shows_the_middle(self):
        self.assertEqual(masked(""), "(none)")
        out = masked("abcdefghijklmnop")
        self.assertTrue(out.startswith("abcd"))
        self.assertNotIn("efghijkl", out)


if __name__ == "__main__":
    unittest.main()
