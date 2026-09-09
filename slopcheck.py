"""slopcheck: structural cleanliness score for a source directory.

Score = round(100 - (0.4 * C + 0.25 * D + 0.15 * L + 0.2 * F)), measured by
Lizard, where C is the percent of function lines (NLOC) inside functions with
cyclomatic complexity above CCN_THRESHOLD, D is the percent of duplicated
tokens, L is the percent of function lines inside functions longer than
LONG_FUNCTION_NLOC lines, and F is the percent of file lines inside files
longer than LARGE_FILE_NLOC lines. C and L overlap on purpose: long functions
are usually complex, and both are penalized. Weights are provisional. This is
not an AI-authorship detector and not proof of correctness.
"""

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
from collections import Counter
from importlib.metadata import version

import lizard
from pathspec import GitIgnoreSpec

CCN_THRESHOLD = 10
LONG_FUNCTION_NLOC = 100
WEIGHT_COMPLEXITY = 0.4
WEIGHT_DUPLICATION = 0.25
WEIGHT_LENGTH = 0.15
WEIGHT_FILE_SIZE = 0.2
MIN_DUPLICATE_TOKENS = 70
TOP_N = 10
TINY_FUNCTION_NLOC = 2
MAX_COMMIT_FILES = 50
MIN_SHARED_COMMITS = 5
MIN_COUPLING_RATIO = 0.8
MIN_NAME_LEN = 4
LARGE_FILE_NLOC = 750

INTERFACE_DECL = re.compile(r"\binterface\s+(I[A-Z]\w*)")
TYPE_BASES = re.compile(r"\b(?:class|record|struct)\s+(\w+)[^{;=]*?:\s*([^{;=]+)")
GENERIC_ARGS = re.compile(r"<[^<>]*>")
IDENTIFIER = re.compile(r"\b[A-Za-z_]\w*\b")

EXCLUDED_DIRS = frozenset("""
.git .hg .svn node_modules bower_components vendor packages bin obj dist build
out target .next .nuxt coverage __pycache__ .venv venv .tox .mypy_cache
.pytest_cache .idea .vs .vscode .terraform Migrations migrations
""".split())

GENERATED_PATTERNS = [
    "*.g.cs", "*.g.i.cs", "*.designer.cs", "*.generated.cs", "*.Designer.cs",
    "*.min.js", "*.min.css", "*.d.ts", "*.generated.ts", "*.pb.go",
    "*_pb2.py", "*_pb2_grpc.py", "*.bundle.js", "*.js.map", "*.lock", "*ModelSnapshot.cs",
]


def _ignored(hard, root, specs, path, is_dir):
    """Hard excludes first (never re-included); then Git precedence: deepest .gitignore with an opinion wins."""
    suffix = "/" if is_dir else ""
    if hard.match_file(os.path.relpath(path, root).replace(os.sep, "/") + suffix):
        return True
    for base, spec in reversed(specs):
        result = spec.check_file(os.path.relpath(path, base).replace(os.sep, "/") + suffix)
        if result.include is not None:
            return result.include
    return False


def select_files(root, excludes=()):
    """Deterministic read-only walk. Returns (supported, unsupported) absolute paths."""
    supported, unsupported = [], []
    hard = GitIgnoreSpec.from_lines(GENERATED_PATTERNS + list(excludes))

    def walk(directory, specs):
        gitignore = os.path.join(directory, ".gitignore")
        if os.path.isfile(gitignore) and not os.path.islink(gitignore):
            with open(gitignore, encoding="utf-8", errors="replace") as fh:
                specs = specs + [(directory, GitIgnoreSpec.from_lines(fh))]
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in EXCLUDED_DIRS and not _ignored(hard, root, specs, entry.path, True):
                    walk(entry.path, specs)
            elif entry.is_file() and not _ignored(hard, root, specs, entry.path, False):
                (unsupported if lizard.get_reader_for(entry.path) is None else supported).append(entry.path)

    walk(root, [])
    return supported, unsupported


def analyze(files):
    """Returns (file_infos, duplicate_extension, failures)."""
    exts = lizard.get_extensions(["duplicate"])
    dup_ext = exts[-1]
    analyzer = lizard.FileAnalyzer(exts)
    infos, failures = [], []
    for path in files:
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                info = analyzer.analyze_source_code(path, lizard.auto_read(path))
        except Exception as exc:  # noqa: BLE001 - any parser failure excludes the file
            failures.append((path, f"{type(exc).__name__}: {exc}"))
            continue
        if err.getvalue():
            failures.append((path, err.getvalue().strip()))
            continue
        infos.append(info)
    for ext in exts:
        if hasattr(ext, "cross_file_process"):
            infos = ext.cross_file_process(infos)
    return list(infos), dup_ext, failures


def _clamp(value):
    return None if value is None else max(0.0, min(100.0, value))


def single_implementation_interfaces(cs_files, rel):
    """C#-only regex heuristic: interfaces implemented by exactly one class/record/struct."""
    if not cs_files:
        return None
    declared, implementers = {}, {}
    for path in cs_files:
        code = lizard.auto_read(path)
        for name in INTERFACE_DECL.findall(code):
            declared.setdefault(name, rel(path))
        for impl, bases in TYPE_BASES.findall(code):
            for token in GENERIC_ARGS.sub("", bases).split(","):
                implementers.setdefault(token.strip(), []).append((rel(path), impl))
    single = {n: implementers[n] for n in declared if len(implementers.get(n, ())) == 1}
    return {
        "total": len(declared),
        "single": len(single),
        "percent": round(100.0 * len(single) / len(declared), 2) if declared else None,
        "examples": sorted(({"name": n, "file": declared[n], "implementation": impls[0][1]}
                            for n, impls in single.items()), key=lambda e: e["name"])[:TOP_N],
    }


def git_history(root, analyzed):
    """Returns (git_info, churn Counter, pair Counter). git_info is a reason str on failure."""
    def git(*args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, errors="replace")
    try:
        top = git("rev-parse", "--show-toplevel")
    except FileNotFoundError:
        return "git not found", None, None
    if top.returncode != 0:
        return (top.stderr.strip().splitlines() or ["git failed"])[0], None, None
    top = top.stdout.strip()
    log = git("-c", "core.quotepath=false", "log", "--name-only", "--no-renames", "--format=%x00", "--", ".")
    if log.returncode != 0:
        return (log.stderr.strip().splitlines() or ["git log failed"])[0], None, None
    commits = log.stdout.split("\0")[1:]
    churn, pairs = Counter(), Counter()
    real_root = os.path.realpath(root)  # git resolves symlinks (macOS /var -> /private/var)
    for commit in commits:
        files = {os.path.relpath(os.path.join(top, p), real_root).replace(os.sep, "/") for p in commit.split("\n") if p}
        files = sorted(files & analyzed)
        if len(files) > MAX_COMMIT_FILES:
            continue
        churn.update(files)
        pairs.update((a, b) for i, a in enumerate(files) for b in files[i + 1:])
    return {"toplevel": top, "commits": len(commits)}, churn, pairs


def single_caller_chains(infos, functions, rel):
    """Functions with exactly one call site whose caller also has exactly one call site."""
    short = lambda fn: fn.name.rsplit("::", 1)[-1]  # noqa: E731
    by_name = {}
    for fn in functions:
        name = short(fn)
        if name != "(anonymous)" and len(name) >= MIN_NAME_LEN:
            by_name.setdefault(name, []).append(fn)
    by_name = {n: fns[0] for n, fns in by_name.items() if len(fns) == 1}
    if not by_name:
        return None
    refs = {name: [] for name in by_name}
    for info in infos:
        in_file = sorted(info.function_list, key=lambda f: f.end_line - f.start_line)  # smallest span first
        for line_no, text in enumerate(lizard.auto_read(info.filename).splitlines(), start=1):
            for name in IDENTIFIER.findall(text):
                fn = by_name.get(name)
                if fn is None:
                    continue
                if fn.filename == info.filename and fn.start_line == line_no:
                    continue
                caller = next((f for f in in_file if f.start_line <= line_no <= f.end_line), None)
                refs[name].append(caller)
    examples = []
    for name, fn in by_name.items():
        if len(refs[name]) != 1 or refs[name][0] is None:
            continue
        caller = short(refs[name][0])
        if by_name.get(caller) is refs[name][0] and len(refs[caller]) == 1:
            examples.append({"file": rel(fn.filename), "line": fn.start_line, "name": name, "caller": caller})
    examples.sort(key=lambda e: (e["file"], e["line"]))
    return {
        "total_named": len(by_name),
        "chains": len(examples),
        "percent": round(100.0 * len(examples) / len(by_name), 2),
        "examples": examples[:TOP_N],
    }


def diagnostics(root, rel, infos, functions):
    tiny = [fn for fn in functions if fn.nloc <= TINY_FUNCTION_NLOC]
    cs_files = [info.filename for info in infos if info.filename.lower().endswith(".cs")]
    git_info, churn, pairs = git_history(root, {rel(info.filename) for info in infos})
    coupling = hotspots = None
    if churn is not None:
        coupling = [
            {"a": a, "b": b, "shared": n, "ratio": round(n / max(churn[a], churn[b]), 2)}
            for (a, b), n in pairs.items() if n >= MIN_SHARED_COMMITS
        ]
        coupling = {"pairs": sorted((p for p in coupling if p["ratio"] >= MIN_COUPLING_RATIO),
                                    key=lambda p: (-p["shared"], -p["ratio"], p["a"], p["b"]))[:TOP_N]}
        hotspots = sorted(
            ({"file": rel(fn.filename), "line": fn.start_line, "name": fn.name, "ccn": fn.cyclomatic_complexity,
              "churn": churn[rel(fn.filename)], "score": churn[rel(fn.filename)] * fn.cyclomatic_complexity}
             for fn in functions if churn[rel(fn.filename)] * fn.cyclomatic_complexity > 0),
            key=lambda h: (-h["score"], h["file"], h["line"], h["name"]),
        )[:TOP_N]
    return {
        "tiny_functions_percent": round(100.0 * len(tiny) / len(functions), 2) if functions else None,
        "single_implementation_interfaces": single_implementation_interfaces(cs_files, rel),
        "single_caller_chains": single_caller_chains(infos, functions, rel),
        "change_coupling": coupling,
        "hotspots": hotspots,
        "git": git_info,
    }


def scan(root, excludes=()):
    root = os.path.abspath(root)
    rel = lambda p: os.path.relpath(p, root).replace(os.sep, "/")  # noqa: E731
    supported, unsupported = select_files(root, excludes)
    infos, dup_ext, failures = analyze(supported)

    functions = [fn for info in infos for fn in info.function_list]
    total_nloc = sum(fn.nloc for fn in functions)
    total_ccn = sum(fn.cyclomatic_complexity for fn in functions)
    complex_fns = [fn for fn in functions if fn.cyclomatic_complexity > CCN_THRESHOLD]
    long_fns = [fn for fn in functions if fn.nloc > LONG_FUNCTION_NLOC]
    share = lambda fns: _clamp(100.0 * sum(fn.nloc for fn in fns) / total_nloc) if total_nloc else None  # noqa: E731
    complexity, length = share(complex_fns), share(long_fns)
    complex_functions = _clamp(100.0 * len(complex_fns) / len(functions)) if functions else None

    large = sorted((info for info in infos if info.nloc > LARGE_FILE_NLOC), key=lambda i: (-i.nloc, rel(i.filename)))
    file_nloc = sum(info.nloc for info in infos)
    file_size = _clamp(100.0 * sum(i.nloc for i in large) / file_nloc) if file_nloc else (0.0 if infos else None)

    blocks = list(dup_ext.get_duplicates(min_duplicate_tokens=MIN_DUPLICATE_TOKENS))
    duplication = _clamp(100.0 * dup_ext.duplicate_rate()) if infos else None

    score = None
    if None not in (complexity, duplication, length, file_size):
        score = round(100 - (WEIGHT_COMPLEXITY * complexity + WEIGHT_DUPLICATION * duplication
                             + WEIGHT_LENGTH * length + WEIGHT_FILE_SIZE * file_size))

    def ranked(metric, attr):
        rows = ({"file": rel(fn.filename), "line": fn.start_line, "name": fn.name, metric: getattr(fn, attr)}
                for fn in functions)
        return sorted(rows, key=lambda f: (-f[metric], f["file"], f["line"], f["name"]))[:TOP_N]

    duplicates = []
    for block in blocks:
        locs = sorted(
            ({"file": rel(s.file_name), "start_line": s.start_line, "end_line": s.end_line} for s in block),
            key=lambda l: (l["file"], l["start_line"]),
        )
        duplicates.append({"lines": locs[0]["end_line"] - locs[0]["start_line"] + 1, "locations": locs})
    duplicates.sort(key=lambda d: (-d["lines"], d["locations"][0]["file"], d["locations"][0]["start_line"]))
    duplicates = _merge_overlapping(duplicates)

    skipped = {}
    for path in unsupported:
        ext = os.path.splitext(path)[1].lower()
        skipped[ext] = skipped.get(ext, 0) + 1

    pct = lambda v: None if v is None else round(v, 2)  # noqa: E731
    return {
        "analyzer": {"name": "lizard", "version": version("lizard")},
        "path": root,
        "score": score,
        "formula": "round(100 - (0.4 * C + 0.25 * D + 0.15 * L + 0.2 * F))",
        "complexity_percent": pct(complexity),
        "complex_functions_percent": pct(complex_functions),
        "duplication_percent": pct(duplication),
        "length_percent": pct(length),
        "file_size_percent": pct(file_size),
        "analyzed_files": len(infos),
        "analyzed_functions": len(functions),
        "total_ccn": total_ccn,
        "decision_points": total_ccn - len(functions),
        "top_complexity": ranked("ccn", "cyclomatic_complexity"),
        "long_functions": ranked("nloc", "nloc"),
        "largest_files": [{"file": rel(i.filename), "nloc": i.nloc} for i in large[:TOP_N]],
        "duplicates": duplicates[:TOP_N],
        "skipped_unsupported": dict(sorted(skipped.items())),
        "diagnostics": diagnostics(root, rel, infos, functions),
        "failures": sorted(({"file": rel(p), "error": e} for p, e in failures), key=lambda f: f["file"]),
    }


def _overlaps(a, b):
    """Same location count, and every i-th location is in the same file with overlapping lines."""
    return len(a) == len(b) and all(
        x["file"] == y["file"] and x["start_line"] <= y["end_line"] and y["start_line"] <= x["end_line"]
        for x, y in zip(a, b))


def _merge_overlapping(duplicates):
    """Greedy merge of duplicate blocks that overlap at every location (reporting only; D is unchanged)."""
    kept = []
    for block in duplicates:
        for other in kept:
            if _overlaps(other["locations"], block["locations"]):
                for x, y in zip(other["locations"], block["locations"]):
                    x["start_line"], x["end_line"] = min(x["start_line"], y["start_line"]), max(x["end_line"], y["end_line"])
                first = other["locations"][0]
                other["lines"] = first["end_line"] - first["start_line"] + 1
                break
        else:
            kept.append(block)
    kept.sort(key=lambda d: (-d["lines"], d["locations"][0]["file"], d["locations"][0]["start_line"]))
    return kept


def render_text(result):
    lines = [f"slopcheck — structural cleanliness score (lizard {result['analyzer']['version']})", ""]
    if result["score"] is None:
        reason = "no analyzed files" if result["analyzed_files"] == 0 else "no functions found"
        lines.append(f"Score: n/a ({reason})")
    else:
        lines.append(f"Score: {result['score']}/100")
    pct = lambda v: "n/a" if v is None else f"{v:.2f}%"  # noqa: E731
    lines += [
        f"Complexity (lines in functions with CCN > {CCN_THRESHOLD}): {pct(result['complexity_percent'])}"
        f"   ({pct(result['complex_functions_percent'])} of functions)",
        f"Duplication (tokens in blocks >= {MIN_DUPLICATE_TOKENS} tokens): {pct(result['duplication_percent'])}",
        f"Length (lines in functions > {LONG_FUNCTION_NLOC} lines): {pct(result['length_percent'])}",
        f"File size (lines in files > {LARGE_FILE_NLOC} lines): {pct(result['file_size_percent'])}",
        f"Analyzed: {result['analyzed_files']} files, {result['analyzed_functions']} functions",
        f"Decision points: {result['decision_points']} (total CCN {result['total_ccn']})",
    ]
    if result["top_complexity"]:
        lines += ["", "Top complexity:"]
        lines += [f"  {f['ccn']:>4}  {f['file']}:{f['line']}  {f['name']}" for f in result["top_complexity"]]
    if result["long_functions"]:
        lines += ["", "Longest functions:"]
        lines += [f"  {f['nloc']:>4}  {f['file']}:{f['line']}  {f['name']}" for f in result["long_functions"]]
    if result["largest_files"]:
        lines += ["", "Largest files:"]
        lines += [f"  {e['nloc']:>5}  {e['file']}" for e in result["largest_files"]]
    if result["duplicates"]:
        lines += ["", "Duplicate blocks:"]
        for d in result["duplicates"]:
            lines.append(f"  {d['lines']} lines x {len(d['locations'])}:")
            lines += [f"    {l['file']}:{l['start_line']}-{l['end_line']}" for l in d["locations"]]
    diag = result["diagnostics"]
    lines += ["", "Diagnostics (not in score):",
              f"  Tiny functions (<= {TINY_FUNCTION_NLOC} lines): {pct(diag['tiny_functions_percent'])}"]
    scc = diag["single_caller_chains"]
    if scc is None:
        lines.append("  Single-caller chain candidates: n/a (no named functions)")
    else:
        lines.append(f"  Single-caller chain candidates: {scc['chains']} of {scc['total_named']} named functions"
                     f" ({pct(scc['percent'])})")
        lines += [f"    {e['file']}:{e['line']}  {e['name']}  <-  {e['caller']}" for e in scc["examples"]]
    sii = diag["single_implementation_interfaces"]
    if sii is None:
        lines.append("  C# interfaces with one implementation: n/a (no C# files)")
    else:
        lines.append(f"  C# interfaces with one implementation: {sii['single']} of {sii['total']}"
                     f" ({pct(sii['percent'])})")
        lines += [f"    {e['name']}  {e['file']}  ->  {e['implementation']}" for e in sii["examples"]]
    if diag["change_coupling"] is None:
        lines.append(f"  Change coupling: n/a ({diag['git']})")
    else:
        lines.append(f"  Change coupling (>= {MIN_SHARED_COMMITS} shared commits, ratio >= {MIN_COUPLING_RATIO}):")
        lines += [f"    {p['shared']:>4}  {p['ratio']:.2f}  {p['a']}  <->  {p['b']}" for p in diag["change_coupling"]["pairs"]]
        lines.append("  Hotspots (churn x CCN):")
        lines += [f"    {h['score']:>5}  {h['churn']:>4}  {h['ccn']:>3}  {h['file']}:{h['line']}  {h['name']}"
                  for h in diag["hotspots"]]
    if result["skipped_unsupported"]:
        lines += ["", "Skipped (no Lizard reader):"]
        lines += [f"  {ext or '(no extension)'}: {n}" for ext, n in result["skipped_unsupported"].items()]
    if result["failures"]:
        lines += ["", "Failures (excluded from measurements):"]
        lines += [f"  {f['file']}: {f['error']}" for f in result["failures"]]
    return "\n".join(lines) + "\n"


def run(argv=None):
    parser = argparse.ArgumentParser(prog="slopcheck", description=__doc__.split("\n\n")[1])
    parser.add_argument("path", help="directory to scan (read-only)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                        help="extra gitignore-style pattern relative to PATH (repeatable)")
    parser.add_argument("--fail-under", type=int, metavar="N", help="exit 1 when the score is below N")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.path):
        print(f"slopcheck: not a directory: {args.path}", file=sys.stderr)
        return 2
    result = scan(args.path, args.exclude)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        sys.stdout.write(render_text(result))
    if result["score"] is None:
        return 1
    return 1 if args.fail_under is not None and result["score"] < args.fail_under else 0


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
