"""Tests for .github/scripts/release_check.py, plus a check that the repo's versions agree."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SCRIPT = os.path.join(REPO, ".github", "scripts", "release_check.py")


def load():
    spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = module.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestThisRepo(unittest.TestCase):
    """Runs on every PR: plugin.json, SKILL.md and CHANGELOG.md must agree."""

    def test_versions_agree_and_changelog_has_the_section(self):
        rc = load()
        version, problems = rc.check()
        self.assertEqual(problems, [])
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

    def test_notes_for_the_matching_tag(self):
        rc = load()
        version = rc.plugin_version()
        notes = os.path.join(tempfile.mkdtemp(), "notes.md")
        self.addCleanup(shutil.rmtree, os.path.dirname(notes))
        code, out, _ = run(rc, ["--tag", "v" + version, "--notes-out", notes])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])
        with open(notes, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.strip())
        self.assertNotIn("## [", text)  # only this version's section


class TestMismatches(unittest.TestCase):
    """Runs the script against a scratch copy of the three files."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        for rel in (".claude-plugin/plugin.json", "skills/context7-add/SKILL.md", "CHANGELOG.md"):
            os.makedirs(os.path.dirname(os.path.join(self.root, rel)), exist_ok=True)
            shutil.copy(os.path.join(REPO, rel), os.path.join(self.root, rel))
        self.rc = load()
        self.rc.ROOT = self.root
        self.version = self.rc.plugin_version()

    def rewrite(self, rel, old, new):
        path = os.path.join(self.root, rel)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(old, text)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new, 1))

    def test_wrong_tag(self):
        code, _, err = run(self.rc, ["--tag", "v0.0.1"])
        self.assertEqual(code, 1)
        self.assertIn("does not match", err)

    def test_malformed_tag(self):
        self.assertEqual(run(self.rc, ["--tag", "1.2"])[0], 2)

    def test_skill_version_differs(self):
        self.rewrite("skills/context7-add/SKILL.md", f'version: "{self.version}"', 'version: "9.9.9"')
        code, _, err = run(self.rc, [])
        self.assertEqual(code, 1)
        self.assertIn("SKILL.md", err)

    def test_missing_changelog_section(self):
        self.rewrite("CHANGELOG.md", f"## [{self.version}]", "## [0.0.0]")
        code, _, err = run(self.rc, [])
        self.assertEqual(code, 1)
        self.assertIn("CHANGELOG.md", err)


if __name__ == "__main__":
    unittest.main()
