import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import slopcheck
from test_slopcheck import CS_SOURCE, TS_FUNCTION, write


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        write(self.root, "calc.cs", CS_SOURCE)

    def configure(self, **settings):
        write(self.root, "slopcheck.json", json.dumps(settings))

    def test_thresholds_and_output(self):
        original = slopcheck.scan(self.root)
        self.configure(max_complexity=13)
        result = slopcheck.scan(self.root)
        self.assertEqual(result["complexity_percent"], 0)
        self.assertEqual(result["score"], 100)
        self.assertIn("CCN > 13", slopcheck.render_text(result))
        self.assertIn("CCN > 13", result["formula"])
        self.configure(max_function_lines=1)
        self.assertGreater(slopcheck.scan(self.root)["length_percent"], 0)
        self.configure(max_file_lines=1)
        self.assertEqual(slopcheck.scan(self.root)["file_size_percent"], 100)
        self.configure()
        self.assertEqual(slopcheck.scan(self.root)["score"], original["score"])

    def test_duplicates_and_exclusions(self):
        write(self.root, "util.ts", TS_FUNCTION.format(name="alpha") + TS_FUNCTION.format(name="beta"))
        self.assertGreater(slopcheck.scan(self.root)["duplication_lines_percent"], 0)
        self.configure(min_duplicate_tokens=10000)
        self.assertEqual(slopcheck.scan(self.root)["duplication_lines_percent"], 0)
        self.configure(exclude=["*.ts"])
        self.assertEqual(slopcheck.scan(self.root)["analyzed_files"], 1)
        self.assertEqual(slopcheck.scan(self.root, ["*.cs"])["analyzed_files"], 0)

    def test_bad_config_fails(self):
        for settings in ({"typo": 5}, {"max_file_lines": 0}, {"max_complexity": True},
                         {"max_function_lines": 1.5}, {"min_duplicate_tokens": 30},
                         {"exclude": "*.ts"}, {"exclude": [1]}, [], None):
            with self.subTest(settings=settings):
                write(self.root, "slopcheck.json", json.dumps(settings))
                with self.assertRaises(ValueError):
                    slopcheck.scan(self.root)
        write(self.root, "slopcheck.json", "{")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(slopcheck.run([self.root]), 2)

    def test_config_must_be_a_regular_file(self):
        path = Path(self.root, "slopcheck.json")
        path.mkdir()
        with self.assertRaisesRegex(ValueError, "regular file"):
            slopcheck.load_config(self.root)
        path.rmdir()
        write(self.root, "settings.json", "{}")
        try:
            path.symlink_to(Path(self.root, "settings.json"))
        except OSError:
            self.skipTest("symlink creation is not available")
        with self.assertRaisesRegex(ValueError, "symlink"):
            slopcheck.load_config(self.root)


class SetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.git("init", "-q")
        self.git("config", "core.hooksPath", ".githooks")
        write(self.root, "calc.cs", CS_SOURCE)
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.root, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                               "-c", "commit.gpgsign=false", *args], check=True, capture_output=True).stdout

    def initialize(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(slopcheck.run(["init", self.root]), 0)

    def test_preserves_hook_and_config_and_runs_ratchet_first(self):
        hook = Path(self.root, ".githooks", "pre-commit")
        original = b"#!/bin/sh\n# existing check\nexit 37\n"
        write(self.root, ".githooks/pre-commit", original.decode())
        write(self.root, "slopcheck.json", '{"max_file_lines": 500}\n')
        config = Path(self.root, "slopcheck.json").read_bytes()
        self.initialize()
        installed = hook.read_bytes()
        self.assertTrue(installed.endswith(original.split(b"\n", 1)[1]))
        self.initialize()
        self.assertEqual(hook.read_bytes(), installed)
        self.assertEqual(Path(self.root, "slopcheck.json").read_bytes(), config)
        self.assertEqual(self.git("diff", "--cached"), b"")
        # Use the installed package's Python entry point through the hook PATH.
        executable = shutil.which("slopcheck")
        self.assertIsNotNone(executable)
        env = dict(os.environ, PATH=os.path.dirname(executable) + os.pathsep + os.environ["PATH"])
        command = ["git", "-C", self.root, "hook", "run", "pre-commit"]
        self.assertEqual(subprocess.run(command, env=env, capture_output=True).returncode, 37)
        write(self.root, "calc.cs", CS_SOURCE.lstrip("\n"))
        self.git("add", "calc.cs")
        self.assertEqual(subprocess.run(command, env=env, capture_output=True).returncode, 1)

    def test_defaults_and_husky(self):
        self.git("config", "core.hooksPath", ".husky/_")
        write(self.root, ".husky/_/pre-commit", "generated husky wrapper\n")
        self.initialize()
        self.assertEqual(Path(self.root, ".husky/_/pre-commit").read_text(), "generated husky wrapper\n")
        self.assertIn("--ratchet HEAD --staged", Path(self.root, ".husky/pre-commit").read_text())
        self.assertEqual(slopcheck.load_config(self.root), slopcheck.DEFAULT_CONFIG)

    def test_default_hooks_in_linked_worktree(self):
        self.git("config", "--unset", "core.hooksPath")
        with tempfile.TemporaryDirectory() as parent:
            linked = os.path.join(parent, "linked")
            self.git("worktree", "add", "--detach", linked)
            with contextlib.redirect_stdout(io.StringIO()):
                slopcheck.initialize(linked)
            self.assertTrue(Path(self.root, ".git/hooks/pre-commit").exists())
            self.assertTrue(Path(linked, "slopcheck.json").exists())

    def test_refuses_unsafe_hooks_without_modifying_files(self):
        hook = Path(self.root, ".githooks/pre-commit")
        write(self.root, ".githooks/pre-commit", "#!/usr/bin/env python3\nraise SystemExit(1)\n")
        original = hook.read_bytes()
        with self.assertRaisesRegex(ValueError, "shell script"):
            slopcheck.initialize(self.root)
        self.assertEqual(hook.read_bytes(), original)
        self.assertFalse(Path(self.root, "slopcheck.json").exists())
        hook.write_bytes(b"\x7fELF\0binary hook")
        with self.assertRaisesRegex(ValueError, "shell script"):
            slopcheck.initialize(self.root)
        self.assertEqual(hook.read_bytes(), b"\x7fELF\0binary hook")
        with tempfile.TemporaryDirectory() as outside:
            self.git("config", "core.hooksPath", outside)
            with self.assertRaisesRegex(ValueError, "outside this repository"):
                slopcheck.initialize(self.root)
            self.assertEqual(os.listdir(outside), [])

    def test_ratchet_uses_staged_config_for_both_snapshots(self):
        write(self.root, "slopcheck.json", '{"max_complexity": 13}\n')
        self.git("add", "slopcheck.json")
        write(self.root, "slopcheck.json", '{"max_complexity": 1}\n')
        result = slopcheck.ratchet(self.root, "HEAD", staged=True)
        self.assertEqual(result["config"]["max_complexity"], 13)
        self.assertEqual(result["base"]["score"], 100)
        self.assertEqual(result["candidate"]["score"], 100)
        self.assertEqual(slopcheck.ratchet(self.root, "HEAD")["config"]["max_complexity"], 10)
        write(self.root, "slopcheck.json", '{"max_file_lines": false}\n')
        self.git("add", "slopcheck.json")
        with self.assertRaisesRegex(ValueError, "positive integer"):
            slopcheck.ratchet(self.root, "HEAD", staged=True)


if __name__ == "__main__":
    unittest.main()
