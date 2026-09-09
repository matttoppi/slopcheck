# slopcheck

`slopcheck` gives a source directory a structural cleanliness score from 0 to 100.
It measures two things with [Lizard](https://github.com/terryyin/lizard):
how many functions have high cyclomatic complexity, and how many tokens are
duplicated.

It is **not** an AI-authorship detector and it is **not** proof that the code is
correct. It only reports structure.

`slopcheck` never modifies or executes the scanned directory. It only reads files.

## Setup

Install as a tool:

```
uv tool install /path/to/slopcheck
# or
pipx install /path/to/slopcheck
```

Or run it without installation:

```
uv run --project /path/to/slopcheck slopcheck PATH
```

## Usage

```
slopcheck PATH
slopcheck PATH --json
slopcheck PATH --exclude 'fixtures/' --exclude '*.spec.ts'
slopcheck PATH --fail-under 80
```

- `PATH` must be an existing directory.
- `--json` prints the full result as JSON.
- `--exclude PATTERN` adds a gitignore-style pattern, relative to `PATH`. Repeat the flag for more patterns.
- `--fail-under N` exits with code 1 when the score is below `N`. Useful in CI.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | A score was reported (and is not below `--fail-under`). |
| 1 | No score (no analyzed files or no functions found), or score below `--fail-under`. |
| 2 | `PATH` is not a directory. |

## Scoring

```
score = round(100 * clean_lines / total_lines)
```

`total_lines` is the number of physical lines in all analyzed files. A line is
**unclean** when it is inside at least one of:

1. a function with cyclomatic complexity greater than 10 (`CCN_THRESHOLD`);
2. a function longer than 100 lines of code (`LONG_FUNCTION_NLOC`);
3. a file longer than 750 lines of code (`LARGE_FILE_NLOC`), every line of it;
4. a duplicate block reported by Lizard's `duplicate` extension, minimum 70
   tokens (`MIN_DUPLICATE_TOKENS`), all blocks, not only the 10 shown.

Causes overlap. A line that is in a complex function inside a large file counts
once. The four `*_percent` values report each cause on its own as a share of
`total_lines`, so they can add up to more than `100 - score`.

The score is a structural cleanliness index. It is not a grade and not proof of
quality or correctness. It is `null` when there are no analyzed files or no
functions.

`duplication_percent` (Lizard's token duplication rate) and
`complex_functions_percent` (percent of functions with CCN > 10, by count) are
reference values and are not part of the score.

### Gaming the score

Splitting a big function into many small ones removes lines from causes 1 and 2
without removing any logic. It also raises `total_ccn` by 1 per new function,
and raises the tiny-function and chain-candidate counts in `diagnostics`. Use
`decision_points` (`total_ccn - analyzed_functions`, the number of branches and
conditions) as the split-proof total: a refactor that does not remove behavior
should not increase it. In review, compare `decision_points` between base and
head.

## JSON fields

| Field | Description |
|-------|-------------|
| `analyzer` | `{"name": "lizard", "version": ...}` |
| `path` | Absolute path that was scanned. |
| `score` | Integer 0..100, or `null`. |
| `formula` | The formula string. |
| `total_lines` | Physical lines in all analyzed files. |
| `unclean_lines` | Lines in the union of all causes. |
| `clean_lines_percent` | `100 * (total_lines - unclean_lines) / total_lines`, 2 decimals. |
| `complexity_percent` | Lines in functions with CCN > 10, as percent of `total_lines`. |
| `length_percent` | Lines in functions longer than 100 lines, as percent of `total_lines`. |
| `file_size_percent` | Lines in files longer than 750 lines, as percent of `total_lines`. |
| `duplication_lines_percent` | Lines in duplicate blocks, as percent of `total_lines`. |
| `duplication_percent` | Lizard token duplication rate. Reference only. |
| `complex_functions_percent` | Percent of functions with CCN > 10 (by count). Reference only. |
| `analyzed_files` | Number of files Lizard parsed. |
| `analyzed_functions` | Number of functions found. |
| `total_ccn` | Sum of cyclomatic complexity over all functions. |
| `decision_points` | `total_ccn - analyzed_functions`: number of branches and conditions. Does not change when a function is split. |
| `top_complexity` | Up to 10 functions: `file`, `line`, `name`, `ccn`. |
| `long_functions` | Up to 10 functions: `file`, `line`, `name`, `nloc`. |
| `largest_files` | Up to 10 files with more than 750 lines: `file`, `nloc`. |
| `duplicates` | Up to 10 duplicate blocks: `lines` and `locations` (`file`, `start_line`, `end_line`). |
| `skipped_unsupported` | Count of files without a Lizard reader, by extension. |
| `diagnostics` | Supplemental metrics, not part of the score. See "Diagnostics". |
| `failures` | Files Lizard could not parse: `file`, `error`. |

All file paths are POSIX-style and relative to `PATH`.

## Diagnostics

The `diagnostics` object reports common footprints of over-engineering. None of
these values affect the score. They do not measure over-engineering itself;
they measure patterns that often come with it.

| Field | Description |
|-------|-------------|
| `tiny_functions_percent` | Percent of functions with 2 or fewer lines of code. Approximates pass-through and plumbing functions. `null` when there are no functions. |
| `single_implementation_interfaces` | C# only. `{"total", "single", "percent", "examples"}`: interfaces (`I` + capital letter) that exactly one `class`, `record`, or `struct` implements. Interfaces with no implementer are not counted as single. Regex heuristic over the analyzed `.cs` files. `null` when there are no C# files. |
| `single_caller_chains` | `{"total_named", "chains", "percent", "examples"}`: named functions that have exactly one call site, where the calling function also has exactly one call site. One helper with one caller is normal decomposition and is not flagged; a chain of one-caller functions is. Functions with zero references are excluded on purpose (entry points, exports, tests). Names shorter than 4 characters and overloaded names (one name, several functions) are skipped. References are whole-word token matches, not resolved symbols. A match in a comment, a string, or an unrelated property name counts as a reference. Extra matches hide chains; a single false match can report a chain with the wrong caller. Treat every entry as a candidate to review, not a finding. `null` when there are no named functions. |
| `change_coupling` | `{"pairs": [...]}`: up to 10 file pairs that change together. `shared` is the number of commits that touch both files; `ratio` is `shared / max(commits of a, commits of b)`. Only pairs with `shared >= 5` and `ratio >= 0.8` are listed. |
| `hotspots` | Up to 10 functions ranked by `churn x CCN`, where `churn` is the number of commits that touch the file. |
| `git` | `{"toplevel", "commits"}` when Git history is available; otherwise a short reason string, and the two Git metrics are `null`. |

The Git metrics need `PATH` to be inside a Git repository and use only
`git rev-parse` and `git log`. Only analyzed files are counted. Commits that
touch more than 50 analyzed files are skipped as bulk edits.

## File selection

- The walk is recursive, sorted, and skips symlinks.
- Every `.gitignore` in the tree is honored, relative to its own directory. As
  in Git, the deepest `.gitignore` with an opinion wins, so `!pattern` can
  re-include a file. Ignored directories are pruned, so a file inside an
  ignored directory cannot be re-included.
- Built-in generated-file patterns and `--exclude` patterns are hard exclusions;
  a `.gitignore` negation cannot re-include them.
- These directory names are always excluded: `.git .hg .svn node_modules
  bower_components vendor packages bin obj dist build out target .next .nuxt
  coverage __pycache__ .venv venv .tox .mypy_cache .pytest_cache .idea .vs
  .vscode .terraform Migrations migrations`.
- These generated-file patterns are always excluded: `*.g.cs *.g.i.cs
  *.designer.cs *.generated.cs *.Designer.cs *.min.js *.min.css *.d.ts
  *.generated.ts *.pb.go *_pb2.py *_pb2_grpc.py *.bundle.js *.js.map *.lock
  *ModelSnapshot.cs`.
- Database migrations and EF model snapshots are excluded by default because
  they are schema history, not maintained code.
- Test files are included.
- Only files that have a Lizard reader are analyzed. Other files are counted in
  `skipped_unsupported`.

## Limitations

- Lizard uses language heuristics, not full parsers. Complexity and function
  boundaries can be wrong for unusual syntax.
- Supported languages are C#, TypeScript, TSX, and whatever else Lizard 1.24.0
  reads. It is not universal.
- Files that cannot be decoded, or that make Lizard fail (for example with
  `RecursionError`), are listed in `failures` and excluded from all measurements.
- There is no unused-code detection.
