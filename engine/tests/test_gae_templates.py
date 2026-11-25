import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))


class TemplatesPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MSTUDIO_HOME"] = self.tmp.name
        os.environ["MSTUDIO_KEYCHAIN"] = "0"
        import appconfig, gae  # noqa: E401
        self.appconfig, self.gae = appconfig, gae
        self.old_home = appconfig.HOME
        appconfig.HOME = Path(self.tmp.name)
        self.old_env = os.environ.pop("GAE_TEMPLATES", None)
        self.old_bundled = gae.TEMPLATES
        gae.TEMPLATES = Path(self.tmp.name) / "not-bundled.json"

    def tearDown(self):
        self.appconfig.HOME = self.old_home
        self.gae.TEMPLATES = self.old_bundled
        if self.old_env is not None:
            os.environ["GAE_TEMPLATES"] = self.old_env
        self.tmp.cleanup()

    def test_missing_everywhere_gives_clear_error(self):
        with self.assertRaises(FileNotFoundError) as e:
            self.gae.templates_path()
        self.assertIn(str(self.appconfig.HOME), str(e.exception))

    def test_found_in_studio_folder(self):
        f = Path(self.tmp.name) / self.gae.TEMPLATES_NAME
        f.write_text(json.dumps({"KeywordNegative": {"localId": 1, "text": "x"}}))
        self.assertEqual(self.gae.templates_path(), f)
        self.assertEqual(self.gae._tpl("KeywordNegative")["text"], "x")

    def test_env_wins(self):
        f = Path(self.tmp.name) / "elsewhere.json"
        f.write_text("{}")
        (Path(self.tmp.name) / self.gae.TEMPLATES_NAME).write_text("{}")
        os.environ["GAE_TEMPLATES"] = str(f)
        try:
            self.assertEqual(self.gae.templates_path(), f)
        finally:
            del os.environ["GAE_TEMPLATES"]


if __name__ == "__main__":
    unittest.main()
