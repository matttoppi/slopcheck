import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import slopcheck
from test_slopcheck import CS_SOURCE, write


class RatchetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.git("init", "-q")
        self.git("config", "core.hooksPath", os.path.join(self.root, "no-hooks"))
        write(self.root, "code with spaces.cs", CS_SOURCE)
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")

    def git(self, *args, env=None):
        return subprocess.run(["git", "-C", self.root, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                               "-c", "commit.gpgsign=false", *args], env=env, check=True, capture_output=True).stdout

    def test_staged_regression_and_moving_baseline(self):
        index = self.git("ls-files", "--stage", "-z")
        self.assertTrue(slopcheck.ratchet(self.root, "HEAD", staged=True)["passed"])
        self.assertEqual(self.git("ls-files", "--stage", "-z"), index)
        # An unstaged improvement must not rescue a staged regression.
        write(self.root, "code with spaces.cs", CS_SOURCE.replace("class Calc", "class Calc2").lstrip("\n"))
        self.git("add", ".")
        write(self.root, "code with spaces.cs", "class Calc { int Simple(int a) { return a + 1; } }\n")
        self.assertFalse(slopcheck.ratchet(self.root, "HEAD", staged=True)["passed"])
        self.assertTrue(slopcheck.ratchet(self.root, "HEAD")["passed"])
        self.git("add", ".")
        improved = slopcheck.ratchet(self.root, "HEAD", staged=True)
        self.assertTrue(improved["passed"])
        self.assertGreater(improved["candidate"]["score"], improved["base"]["score"])
        self.git("commit", "-qm", "improve")
        self.assertTrue(slopcheck.ratchet(self.root, "HEAD~1")["passed"])
        write(self.root, "code with spaces.cs", CS_SOURCE)
        self.git("add", ".")
        self.assertFalse(slopcheck.ratchet(self.root, "HEAD", staged=True)["passed"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(slopcheck.run([self.root, "--ratchet", "HEAD", "--staged"]), 1)

    def test_rejects_drop_hidden_by_rounding(self):
        before = {"failures": [], "score": 68, "total_lines": 10000, "unclean_lines": 3184}
        after = dict(before, unclean_lines=3185)
        with patch.object(slopcheck, "scan", side_effect=[before, after]):
            self.assertFalse(slopcheck.ratchet(self.root, "HEAD")["passed"])

    def test_snapshot_uses_blobs_and_index_gitignore(self):
        write(self.root, ".gitattributes", "*.cs export-ignore\n")
        self.git("add", ".")
        self.git("commit", "-qm", "attributes")
        # git archive would incorrectly hide the source; raw blobs preserve it.
        self.assertTrue(slopcheck.ratchet(self.root, "HEAD", staged=True)["passed"])
        write(self.root, ".gitignore", "bad.cs\n")
        self.git("add", ".gitignore")
        write(self.root, "bad.cs", CS_SOURCE)
        self.git("add", "-f", "bad.cs")
        os.remove(os.path.join(self.root, ".gitignore"))
        self.assertTrue(slopcheck.ratchet(self.root, "HEAD", staged=True)["passed"])

    def test_alternate_index_and_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            alternate = os.path.join(directory, "index")
            env = dict(os.environ, GIT_INDEX_FILE=alternate)
            self.git("read-tree", "HEAD", env=env)
            write(self.root, "code with spaces.cs", "class Calc { int Simple() { return 1; } }\n")
            self.git("add", ".", env=env)
            with patch.dict(os.environ, {"GIT_INDEX_FILE": alternate}):
                result = slopcheck.ratchet(self.root, "HEAD", staged=True)
            self.assertEqual(result["candidate"]["score"], 100)
            self.assertLess(slopcheck.ratchet(self.root, "HEAD", staged=True)["candidate"]["score"], 100)
            linked = os.path.join(directory, "worktree")
            self.git("worktree", "add", "--detach", linked, "HEAD")
            self.assertTrue(slopcheck.ratchet(linked, "HEAD", staged=True)["passed"])

    def test_invalid_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            slopcheck.ratchet(self.root, "missing-revision")
        os.mkdir(os.path.join(self.root, "sub"))
        with self.assertRaisesRegex(ValueError, "worktree root"):
            slopcheck.ratchet(os.path.join(self.root, "sub"), "HEAD")
        with patch.object(slopcheck, "scan", return_value={"failures": [{"file": "bad.cs"}]}):
            with self.assertRaisesRegex(ValueError, "parser failures"):
                slopcheck.ratchet(self.root, "HEAD")
        self.git("rm", "--cached", "code with spaces.cs")
        with self.assertRaisesRegex(ValueError, "no score"):
            slopcheck.ratchet(self.root, "HEAD", staged=True)


if __name__ == "__main__":
    unittest.main()
