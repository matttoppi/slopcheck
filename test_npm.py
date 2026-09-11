import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_slopcheck import CS_SOURCE, HERE, write


@unittest.skipUnless(all(shutil.which(tool) for tool in ("npm", "node", "uv")), "npm, node, and uv are required")
class NpmTests(unittest.TestCase):
    def test_installed_archive_and_launcher_errors(self):
        with tempfile.TemporaryDirectory(prefix="slopcheck npm ") as directory:
            packed = subprocess.run([shutil.which("npm"), "pack", "--json", "--pack-destination", directory],
                                    cwd=HERE, check=True, capture_output=True, text=True)
            archive = json.loads(packed.stdout)[0]
            self.assertEqual({f["path"] for f in archive["files"]},
                             {"package.json", "README.md", "LICENSE", "cli/slopcheck.cjs",
                              "pyproject.toml", "uv.lock", "slopcheck.py"})
            subprocess.run([shutil.which("npm"), "install", "--prefix", directory, "--ignore-scripts",
                            "--no-audit", "--no-fund", os.path.join(directory, archive["filename"])],
                           check=True, capture_output=True)
            package = Path(directory, "node_modules", "@toppi", "slopcheck")
            cli = [shutil.which("node"), str(package / "cli" / "slopcheck.cjs")]
            executable = shutil.which("slopcheck", path=os.path.join(directory, "node_modules", ".bin"))
            self.assertIsNotNone(executable)
            source = os.path.join(directory, "source with spaces")
            write(source, "calc.cs", CS_SOURCE)
            # A caller's Python module must not replace the packaged scanner.
            write(source, "slopcheck.py", "raise RuntimeError('wrong scanner')\n")
            result = subprocess.run([executable, ".", "--json", "--exclude", "slopcheck.py"], cwd=source,
                                    check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)["analyzed_files"], 1)
            self.assertEqual(json.loads(result.stdout)["analyzed_functions"], 3)
            self.assertTrue(os.path.samefile(json.loads(result.stdout)["path"], source))
            self.assertEqual(subprocess.run(cli + [source, "--fail-under", "101"], capture_output=True).returncode, 1)
            self.assertEqual(subprocess.run(cli + [source + "-missing"], capture_output=True).returncode, 2)
            write(source, "slopcheck.json", '{"max_complexity": 13, "exclude": ["slopcheck.py"]}\n')
            configured = subprocess.run(cli + [source, "--json"], check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(configured.stdout)["score"], 100)
            help_result = subprocess.run(cli + ["init", "--help"], check=True, capture_output=True, text=True)
            self.assertIn("install a pre-commit ratchet", help_result.stdout)
            env = dict(os.environ, PATH=directory)
            missing = subprocess.run(cli + [source], env=env, capture_output=True, text=True)
            self.assertEqual(missing.returncode, 2)
            self.assertIn("requires uv", missing.stderr)


if __name__ == "__main__":
    unittest.main()
