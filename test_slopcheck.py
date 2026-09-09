import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import slopcheck

HERE = os.path.dirname(os.path.abspath(__file__))

# 3 functions; Complex has 12 ifs -> ccn 13.
CS_SOURCE = """
class Calc {
    int Simple(int a) { return a + 1; }
    int Twice(int a) { return a * 2; }
    int Complex(int a) {
        int r = 0;
""" + "".join(f"        if (a == {i}) r += {i};\n" for i in range(12)) + """
        return r;
    }
}
"""

TS_FUNCTION = """
export function {name}(items: number[], factor: number): number {{
    let total = 0;
    for (const item of items) {{
        const scaled = item * factor + item / factor - item % factor;
        const adjusted = scaled > 100 ? scaled - 100 : scaled + 100;
        total = total + adjusted * 2 - adjusted / 3 + adjusted % 7;
    }}
    return total * factor - total / factor + total % factor;
}}
"""

TS_FUNCTION_B = """
export function {name}(names: string[], prefix: string): string[] {{
    const out: string[] = [];
    for (const name of names) {{
        if (name.length > 3 && name.startsWith(prefix)) {{
            out.push(prefix + ":" + name.toUpperCase() + "/" + name.length);
        }} else if (name.endsWith(prefix)) {{
            out.push(name.toLowerCase() + "-" + prefix.length);
        }}
    }}
    return out.filter((s) => s.length > prefix.length).map((s) => s.trim());
}}
"""

TSX_SOURCE = """
export function Greeting({ name }: { name: string }) {
    if (!name) {
        return <span>anonymous</span>;
    }
    return <div className="greeting">Hello, {name}</div>;
}
"""


def write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def base_fixture(root):
    write(root, "calc.cs", CS_SOURCE)
    write(root, "util.ts", TS_FUNCTION.format(name="alpha") + TS_FUNCTION.format(name="beta"))


def files_in(result):
    files = {f["file"] for f in result["top_complexity"]}
    files |= {loc["file"] for d in result["duplicates"] for loc in d["locations"]}
    return files


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_formula(self):
        write(self.root, "calc.cs", CS_SOURCE)
        r = slopcheck.scan(self.root)
        self.assertEqual(r["analyzed_functions"], 3)
        self.assertEqual(r["analyzer"]["version"], importlib.metadata.version("lizard"))
        self.assertAlmostEqual(r["complex_functions_percent"], 100 / 3, places=2)
        self.assertTrue(0 < r["complexity_percent"] < 100)
        self.assertEqual(r["length_percent"], 0.0)
        c, d, l = r["complexity_percent"], r["duplication_percent"], r["length_percent"]
        self.assertEqual(r["score"], round(100 - (0.5 * c + 0.3 * d + 0.2 * l)))
        self.assertEqual(r["top_complexity"][0]["name"], "Calc::Complex")
        self.assertEqual(r["top_complexity"][0]["ccn"], 13)
        self.assertEqual(r["total_ccn"], sum(f["ccn"] for f in r["top_complexity"]))
        self.assertEqual(r["decision_points"], r["total_ccn"] - 3)

    def test_long_function(self):
        long_fn = "export function longOne(): number {\n    let total = 0;\n" + "    total = total + 1;\n" * 120
        long_fn += "    return total;\n}\n"
        write(self.root, "long.ts", long_fn + "export function shortOne(): number { return 1; }\n")
        r = slopcheck.scan(self.root)
        self.assertGreater(r["length_percent"], 0)
        self.assertEqual(r["long_functions"][0]["name"], "longOne")
        self.assertGreater(r["long_functions"][0]["nloc"], 100)

    def test_duplicate_detection(self):
        base_fixture(self.root)
        r = slopcheck.scan(self.root)
        self.assertTrue(r["duplicates"])
        self.assertGreater(r["duplication_percent"], 0)
        locs = r["duplicates"][0]["locations"]
        self.assertEqual(locs, sorted(locs, key=lambda l: (l["file"], l["start_line"])))
        for loc in locs:
            self.assertFalse(os.path.isabs(loc["file"]))
            self.assertEqual(loc["file"], "util.ts")

    def test_duplicate_blocks_merged(self):
        write(self.root, "util.ts", TS_FUNCTION.format(name="alpha") + TS_FUNCTION.format(name="beta")
              + TS_FUNCTION_B.format(name="gamma") + TS_FUNCTION_B.format(name="delta"))
        dups = slopcheck.scan(self.root)["duplicates"]
        self.assertGreaterEqual(len(dups), 2)
        for i, a in enumerate(dups):
            for b in dups[i + 1:]:
                self.assertFalse(slopcheck._overlaps(a["locations"], b["locations"]), (a, b))

    def test_fail_under(self):
        write(self.root, "calc.cs", CS_SOURCE)
        cmd = [sys.executable, "slopcheck.py", self.root, "--fail-under"]
        self.assertEqual(subprocess.run(cmd + ["101"], cwd=HERE, capture_output=True).returncode, 1)
        self.assertEqual(subprocess.run(cmd + ["0"], cwd=HERE, capture_output=True).returncode, 0)

    def test_exclusions(self):
        base_fixture(self.root)
        huge = "class G { int F(int a) { int r = 0;\n" + "if (a == 1) r++;\n" * 30 + "return r; } }"
        write(self.root, "node_modules/x.ts", TS_FUNCTION.format(name="nm"))
        write(self.root, "bin/y.cs", huge)
        write(self.root, "gen.g.cs", huge)
        write(self.root, ".gitignore", "ignored/\n")
        write(self.root, "ignored/z.ts", TS_FUNCTION.format(name="ig"))
        write(self.root, "sub/.gitignore", "local.ts\n")
        write(self.root, "sub/local.ts", TS_FUNCTION.format(name="loc"))
        write(self.root, "sub/kept.ts", TS_FUNCTION.format(name="kept"))
        write(self.root, "skip/s.ts", TS_FUNCTION.format(name="skipped"))
        r = slopcheck.scan(self.root, ["skip/"])
        self.assertEqual(r["analyzed_files"], 3)  # calc.cs, util.ts, sub/kept.ts
        found = files_in(r)
        self.assertIn("sub/kept.ts", found)
        for bad in ("node_modules/x.ts", "bin/y.cs", "gen.g.cs", "ignored/z.ts", "sub/local.ts", "skip/s.ts"):
            self.assertNotIn(bad, found)
        self.assertEqual(r["top_complexity"][0]["ccn"], 13)

    def test_hard_excludes_beat_gitignore_negation(self):
        base_fixture(self.root)
        write(self.root, ".gitignore", "!keep.d.ts\n!a.spec.ts\n")
        write(self.root, "keep.d.ts", TS_FUNCTION.format(name="dts"))
        write(self.root, "a.spec.ts", TS_FUNCTION.format(name="spec"))
        r = slopcheck.scan(self.root, ["*.spec.ts"])
        self.assertEqual(r["analyzed_files"], 2)  # calc.cs, util.ts
        found = files_in(r)
        self.assertNotIn("keep.d.ts", found)
        self.assertNotIn("a.spec.ts", found)

    def test_unsupported(self):
        base_fixture(self.root)
        write(self.root, "notes.md", "# notes\n")
        write(self.root, "data.json", "{}\n")
        r = slopcheck.scan(self.root)
        self.assertEqual(r["skipped_unsupported"], {".json": 1, ".md": 1})
        self.assertEqual(r["failures"], [])

    def test_nested_gitignore_negation(self):
        write(self.root, "calc.cs", CS_SOURCE)
        write(self.root, ".gitignore", "*.ts\n")
        write(self.root, "a.ts", TS_FUNCTION.format(name="a"))
        write(self.root, "sub/.gitignore", "!keep.ts\n")
        write(self.root, "sub/keep.ts", TS_FUNCTION.format(name="keep"))
        write(self.root, "sub/other.ts", TS_FUNCTION.format(name="other"))
        r = slopcheck.scan(self.root)
        self.assertEqual(r["analyzed_files"], 2)
        found = files_in(r)
        self.assertIn("sub/keep.ts", found)
        self.assertNotIn("a.ts", found)
        self.assertNotIn("sub/other.ts", found)

    def test_failures(self):
        write(self.root, "calc.cs", CS_SOURCE)
        locked = os.path.join(self.root, "locked.ts")
        write(self.root, "locked.ts", TS_FUNCTION.format(name="locked"))
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o644)
        if os.access(locked, os.R_OK):
            self.skipTest("running as root; cannot make file unreadable")
        r = slopcheck.scan(self.root)
        self.assertEqual(len(r["failures"]), 1)
        self.assertEqual(r["failures"][0]["file"], "locked.ts")
        self.assertTrue(r["failures"][0]["error"].startswith("PermissionError"), r["failures"][0]["error"])
        self.assertEqual(r["analyzed_files"], 1)
        self.assertEqual(r["analyzed_functions"], 3)
        self.assertNotIn("locked.ts", files_in(r))
        only = os.path.join(self.root, "only")
        write(only, "locked.ts", TS_FUNCTION.format(name="locked"))
        os.chmod(os.path.join(only, "locked.ts"), 0)
        self.addCleanup(os.chmod, os.path.join(only, "locked.ts"), 0o644)
        r = slopcheck.scan(only)
        self.assertEqual(r["analyzed_files"], 0)
        self.assertIsNone(r["score"])
        self.assertEqual(len(r["failures"]), 1)

    def test_empty_input(self):
        r = slopcheck.scan(self.root)
        self.assertIsNone(r["score"])
        self.assertEqual(r["analyzed_files"], 0)
        proc = subprocess.run([sys.executable, "slopcheck.py", self.root], cwd=HERE, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Score: n/a", proc.stdout)
        proc = subprocess.run([sys.executable, "slopcheck.py", os.path.join(self.root, "nope")], cwd=HERE,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_repeatable(self):
        base_fixture(self.root)
        self.assertEqual(slopcheck.scan(self.root), slopcheck.scan(self.root))
        cmd = [sys.executable, "slopcheck.py", self.root, "--json"]
        first = subprocess.run(cmd, cwd=HERE, capture_output=True).stdout
        second = subprocess.run(cmd, cwd=HERE, capture_output=True).stdout
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["path"], os.path.abspath(self.root))

    def test_tsx(self):
        write(self.root, "Greeting.tsx", TSX_SOURCE)
        r = slopcheck.scan(self.root)
        self.assertEqual(r["analyzed_files"], 1)
        self.assertGreaterEqual(r["analyzed_functions"], 1)
        self.assertEqual({f["file"] for f in r["top_complexity"]}, {"Greeting.tsx"})
        self.assertTrue(any("Greeting" in f["name"] for f in r["top_complexity"]))

    def test_tiny_functions(self):
        write(self.root, "calc.cs", CS_SOURCE)
        r = slopcheck.scan(self.root)
        self.assertAlmostEqual(r["diagnostics"]["tiny_functions_percent"], 200 / 3, places=2)

    def test_single_impl_interfaces(self):
        write(self.root, "ifaces.cs", "interface IOne {}\ninterface ITwo {}\ninterface IThree<T> {}\n"
              "class A : IOne {}\nclass B : Base, ITwo {}\nrecord C : ITwo, IThree<int> {}\n")
        sii = slopcheck.scan(self.root)["diagnostics"]["single_implementation_interfaces"]
        self.assertEqual(sii["total"], 3)
        self.assertEqual(sii["single"], 2)
        self.assertEqual([e["name"] for e in sii["examples"]], ["IOne", "IThree"])
        self.assertEqual(sii["examples"][1]["implementation"], "C")
        ts_root = os.path.join(self.root, "tsdir")
        write(ts_root, "util.ts", TS_FUNCTION.format(name="alpha"))
        self.assertIsNone(slopcheck.scan(ts_root)["diagnostics"]["single_implementation_interfaces"])

    def test_single_caller_chains(self):
        write(self.root, "chain.ts", "export function entry() { return alpha(); }\n"
              "function alpha() { return bravo() + shared(); }\n"
              "function bravo() { return charlie() + shared(); }\n"
              "function charlie() { return 1; }\n"
              "function shared() { return 2; }\n")
        scc = slopcheck.scan(self.root)["diagnostics"]["single_caller_chains"]
        self.assertEqual(scc["total_named"], 5)
        self.assertEqual(scc["chains"], 2)
        self.assertEqual([(e["name"], e["caller"]) for e in scc["examples"]],
                         [("bravo", "alpha"), ("charlie", "bravo")])

    def test_git_unavailable(self):
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        write(self.root, "calc.cs", CS_SOURCE)
        diag = slopcheck.scan(self.root)["diagnostics"]
        self.assertIsNone(diag["change_coupling"])
        self.assertIsNone(diag["hotspots"])
        self.assertIsInstance(diag["git"], str)

    def test_git_coupling_and_hotspots(self):
        if shutil.which("git") is None:
            self.skipTest("git not installed")

        def git(*args):
            subprocess.run(["git", "-C", self.root, "-c", "user.name=t", "-c", "user.email=t@t",
                            "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)

        git("init", "-q")
        sub = os.path.join(self.root, "sub")
        write(sub, "x.ts", TS_FUNCTION.format(name="x"))
        write(sub, "y.ts", TS_FUNCTION.format(name="y"))
        write(sub, "z.ts", TS_FUNCTION.format(name="z"))
        git("add", "."); git("commit", "-q", "-m", "init")
        for i in range(4):
            for name in ("x.ts", "y.ts"):
                with open(os.path.join(sub, name), "a") as fh:
                    fh.write(f"// change {i}\n")
            git("add", "."); git("commit", "-q", "-m", f"change {i}")
        with open(os.path.join(sub, "z.ts"), "a") as fh:
            fh.write("// z only\n")
        git("add", "."); git("commit", "-q", "-m", "z only")
        diag = slopcheck.scan(sub)["diagnostics"]
        self.assertEqual(diag["git"]["commits"], 6)
        self.assertEqual(diag["change_coupling"]["pairs"], [{"a": "x.ts", "b": "y.ts", "shared": 5, "ratio": 1.0}])
        self.assertEqual(diag["hotspots"][0]["churn"], 5)
        self.assertIn(diag["hotspots"][0]["file"], ("x.ts", "y.ts"))


if __name__ == "__main__":
    unittest.main()
