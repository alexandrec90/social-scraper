#!/usr/bin/env python3
"""Structural ratchet: the shape of the code may not get worse than `.devkit-structure.txt`.

A repository written by agents accumulates a particular kind of debt: the function
that grew a branch per request until it is 300 lines, the module every other module
imports, the `# noqa` that made a finding go away, the `it.skip` nobody revisited, the
dependency added for one call. None of it fails a test, and every piece of it is the
cheapest thing an agent can do in the moment. This gate makes the shape visible and
holds it to a committed baseline, on the same terms as `untested_symbols.py`: existing
debt is recorded once, and from then on the file may only shrink.

## What is measured

`structure_scan.py` reads each file; this module judges. Three kinds of rule:

- **Limits** -- a measurement above `DEFAULT_LIMITS[rule]` (or the project's override
  in `[structure.limits]`) is a finding, recorded with its value. `file_lines`,
  `imports`, `definitions`, `react_state`, `react_effects` per file;
  `function_lines`, `function_params`, `complexity`, `nesting_depth` per function;
  `component_lines` per React component; `class_lines`, `class_methods` per class.
- **Counters** -- `suppressions`, `any_types`, `todos`, `skipped_tests`,
  `focused_tests`, `swallowed_errors`: any non-zero count is a finding. These are
  the only rules that read test files too, because that is where skips live.
- **Graph and policy** -- `cycle` (an import cycle, keyed by its first member),
  `orphan` (a module nothing outside the tests imports), `layer` and `restrict` (the
  boundaries `[[structure.layers]]` and `[[structure.restrict]]` declare), and
  `dependency` (every declared third-party package, so adding one is a baseline diff
  a reviewer sees).

Every finding is one line, `rule::path[::symbol] = value`. The gate fails when a key
is new, when a value grew, when a line is no longer true (the function shrank, the
dependency went), or when the baseline is missing a line the code no longer earns --
`--tighten` rewrites the file for the last two cases and refuses to do anything else.

## What is not

A symbol is keyed by name, not line, so a file with two functions of the same name
records the larger. A vendored path (anything in `sync-devkit.py`'s `MANIFEST`, in a
project that carries a `DEVKIT_VERSION` stamp) is skipped: it is devkit's debt, held
to devkit's baseline, and a `--pull` must not move a consumer's numbers. Failures go
to `logs/structure-check.log`; the terminal gets a status line and that path.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness_config
import structure_scan as scan_mod

REPO_ROOT = (Path(__file__).parent / "../..").resolve()
CFG = harness_config.load(REPO_ROOT)

BASELINE_NAME = ".devkit-structure.txt"
ARTIFACT = "logs/structure-check.log"

DEFAULT_LIMITS: dict[str, int] = {
    "file_lines": 500,
    "function_lines": 80,
    "function_params": 6,
    "complexity": 15,
    "nesting_depth": 4,
    "class_lines": 300,
    "class_methods": 20,
    "imports": 20,
    "definitions": 20,
    "component_lines": 250,
    "react_state": 8,
    "react_effects": 5,
}
GRAPH_RULES = ("cycle", "orphan")
POLICY_RULES = ("layer", "restrict", "dependency")
RULES = frozenset((*DEFAULT_LIMITS, *scan_mod.COUNTERS, *GRAPH_RULES, *POLICY_RULES))

REMEDIES: dict[str, str] = {
    "file_lines": "split the module along the seam its imports already show",
    "function_lines": "extract the branches into named helpers",
    "function_params": "pass a dataclass or an options object, or split the function",
    "complexity": "one function per branch family; early returns over nested ifs",
    "nesting_depth": "invert the condition and return early, or extract the inner loop",
    "class_lines": "this class has more than one job; move a job out",
    "class_methods": "group the methods that share state into their own class",
    "imports": "the module depends on too much; split it or introduce a facade",
    "definitions": "the module defines too much; split it by responsibility",
    "component_lines": "extract child components and hooks",
    "react_state": "fold related useState calls into a reducer or a custom hook",
    "react_effects": "move effects into custom hooks, or derive the value instead",
    "suppressions": "fix what the tool found, or name the reason on the suppression",
    "any_types": "give it a type; `unknown` if the shape is truly open",
    "todos": "do it, or file it and delete the comment",
    "skipped_tests": "un-skip it, or delete it with the reason in the commit",
    "focused_tests": "remove `.only` -- it silences every other test in the file",
    "swallowed_errors": "catch the specific exception, or re-raise after logging",
    "cycle": "break the cycle: move the shared piece below both modules",
    "orphan": "delete it, or import it from the code that should be using it",
    "layer": "this layer may not import that one; go through the boundary",
    "restrict": "this call belongs behind the wrapper the rule names",
    "dependency": "a new dependency needs a reason in the PR; then re-seed the line",
}

BASELINE_HEADER = """\
# Structural findings the code already had when it adopted the gate.
#
# Debt, not configuration. This file SHRINKS on its own: a new key fails the gate, a
# value that grew fails the gate, and a line the code no longer earns fails it too,
# so fixing a finding forces its line out (`structure_check.py --tighten` does that).
# Never add or raise a line to make new code pass; fix the code. `dependency::` is one
# exception -- adding a package is a line here, so the PR diff shows it.
#
# `--record --reason "..."` is the other, and it is the deliberate hole. A module far
# past `file_lines` cannot gain a line, so a bug whose fix belongs in one has nowhere
# to go, and the split the finding asks for is a separate body of work. Recording says
# so out loud: the reason is mandatory, counter rules (`suppressions`, `todos`,
# `skipped_tests`, ...) are refused because those are always somebody giving up rather
# than a module's size, and every move is logged below with what it moved and why.
#
# Regenerate only when adopting the gate: python scripts/hooks/structure_check.py --seed
"""

# What `existing_notes` reads back, and what separates the machine-readable lines above
# it from the `--record` log below. Both halves are comments to `read_baseline`.
RECORD_MARKER = "# --- recorded growth ------------------------------------------------\n"

TOOLING_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".worktrees",
        "dist",
        "build",
        ".next",
        "coverage",
    }
)

# Files run by name rather than imported: the orphan rule cannot see the runner.
ENTRY_STEMS = frozenset(
    {
        "__init__",
        "__main__",
        "main",
        "manage",
        "wsgi",
        "asgi",
        "conftest",
        "setup",
        "env",
        "index",
        "App",
        "_app",
        "_document",
        "layout",
        "page",
        "route",
        "middleware",
        "setupTests",
        "server",
        "cli",
        "vite-env",
    }
)
ENTRY_PARTS = frozenset(
    {"scripts", "bin", "migrations", "alembic", "management", "commands", "pages", "routes"}
)
_MAIN_GUARD = re.compile(r"__name__\s*==\s*['\"]__main__['\"]")
_DEP_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    value: int
    symbol: str = ""
    detail: str = ""

    @property
    def key(self) -> str:
        return "::".join(part for part in (self.rule, self.path, self.symbol) if part)


# --- Configuration -----------------------------------------------------------------


def limits(cfg: harness_config.Config) -> dict[str, int]:
    return {**DEFAULT_LIMITS, **cfg.structure.limits}


def config_errors(cfg: harness_config.Config) -> list[str]:
    """What `[structure]` got wrong. Reported, never ignored: a mistyped limit that
    silently fell back to the default would be a limit the project believes it set."""
    errors = []
    for key in cfg.structure.limits:
        if key not in DEFAULT_LIMITS:
            errors.append(
                f"[structure.limits] {key}: not a limit (known: {', '.join(DEFAULT_LIMITS)})"
            )
    for name in cfg.structure.disabled:
        if name not in RULES:
            errors.append(
                f"[structure] disabled = {name!r}: not a rule (known: {', '.join(sorted(RULES))})"
            )
    for rule in cfg.structure.layers:
        if not rule.name or not rule.sources or not rule.forbid:
            errors.append(f"[[structure.layers]] {rule.name or '?'}: needs name, from and forbid")
    for restrict in cfg.structure.restrict:
        if not restrict.name or not restrict.pattern:
            errors.append(f"[[structure.restrict]] {restrict.name or '?'}: needs name and pattern")
            continue
        try:
            re.compile(restrict.pattern)
        except re.error as exc:
            errors.append(f"[[structure.restrict]] {restrict.name}: bad pattern ({exc})")
    return errors


def scan_roots(cfg: harness_config.Config) -> tuple[str, ...]:
    """The trees measured, in a stable order, each without its trailing slash."""
    if cfg.structure.paths:
        raw = list(cfg.structure.paths)
    else:
        raw = [cfg.app_dir, "scripts/"]
        if cfg.frontend.enabled:
            raw.append(cfg.frontend.src)
    raw.append(cfg.tests_dir)
    roots: list[str] = []
    for entry in raw:
        normalised = entry.strip().strip("/").replace("\\", "/") or "."
        if normalised not in roots:
            roots.append(normalised)
    return tuple(roots)


def vendored_paths(root: Path) -> frozenset[str]:
    """Paths `sync-devkit.py` owns, in a project that has adopted devkit.

    Read off the script's `MANIFEST` literal rather than imported: the script is not a
    module of this tree, and a consumer's copy may be older than this one.
    """
    if not (root / "DEVKIT_VERSION").is_file():
        return frozenset()
    script = root / "scripts" / "sync-devkit.py"
    if not script.is_file():
        return frozenset()
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError, OSError):
        return frozenset()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "MANIFEST" for t in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return frozenset()
            return frozenset(str(v) for v in value if isinstance(v, str))
    return frozenset()


def _excluded(rel: str, cfg: harness_config.Config) -> bool:
    parts = PurePosixPath(rel).parts
    if any(part in TOOLING_DIRS for part in parts):
        return True
    for entry in cfg.structure.exclude:
        entry = entry.replace("\\", "/")
        if "/" in entry.strip("/"):
            if rel.startswith(entry.strip("/") + "/") or rel == entry.strip("/"):
                return True
        elif entry.strip("/") in parts:
            return True
    return False


def source_files(root: Path, cfg: harness_config.Config) -> list[str]:
    """Every file the scanners read, as sorted repo-relative POSIX paths."""
    vendored = vendored_paths(root)
    found: list[str] = []
    for directory in scan_roots(cfg):
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            suffix = path.suffix
            if suffix not in scan_mod.PY_SUFFIXES and suffix not in scan_mod.JS_SUFFIXES:
                continue
            if path.name.endswith((".d.ts", ".min.js")) or rel in vendored:
                continue
            if _excluded(rel, cfg) or rel in found:
                continue
            found.append(rel)
    return sorted(found)


def is_test(rel: str, cfg: harness_config.Config) -> bool:
    tests = cfg.tests_dir.strip("/")
    return scan_mod.is_test_file(rel) or rel.startswith(tests + "/")


def read_sources(root: Path, cfg: harness_config.Config) -> dict[str, str]:
    """`rel -> text` for every scanned file, skipping what no scanner should read
    (a bundle, a fixture that happens to end in `.js`)."""
    texts: dict[str, str] = {}
    for rel in source_files(root, cfg):
        path = root / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            texts[rel] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return texts


# --- Limits and counters -------------------------------------------------------------


def measure(
    rel: str, metrics: scan_mod.FileMetrics, lim: dict[str, int], test: bool
) -> list[Finding]:
    """The limit and counter findings for one file."""
    found: list[Finding] = []
    for counter in scan_mod.COUNTERS:
        count = metrics.counts.get(counter, 0)
        if count > 0:
            found.append(Finding(counter, rel, count))
    if test:
        return found
    for rule, value in (
        ("file_lines", metrics.lines),
        ("imports", metrics.imports),
        ("definitions", metrics.definitions),
        ("react_state", metrics.react_state),
        ("react_effects", metrics.react_effects),
    ):
        if value > lim[rule]:
            found.append(Finding(rule, rel, value))
    for sym in metrics.symbols:
        checks: tuple[tuple[str, int], ...]
        if sym.kind == "class":
            checks = (("class_lines", sym.lines), ("class_methods", sym.methods))
        else:
            size_rule = "component_lines" if sym.kind == "component" else "function_lines"
            checks = (
                (size_rule, sym.lines),
                ("function_params", sym.params),
                ("complexity", sym.complexity),
                ("nesting_depth", sym.depth),
            )
        for rule, value in checks:
            if value > lim[rule]:
                found.append(Finding(rule, rel, value, symbol=sym.name, detail=f"line {sym.line}"))
    return found


# --- The import graph ----------------------------------------------------------------


def _candidates_py(base: str, dotted: list[str]) -> list[str]:
    """Paths a dotted name could denote under `base`, longest prefix first, so that
    `from pkg.mod import name` resolves to `pkg/mod.py` before `pkg/__init__.py`."""
    out = []
    for cut in range(len(dotted), 0, -1):
        stem = "/".join(dotted[:cut])
        prefix = f"{base}/" if base and base != "." else ""
        out.append(f"{prefix}{stem}.py")
        out.append(f"{prefix}{stem}/__init__.py")
    return out


def resolve_python(spec: str, importer: str, roots: tuple[str, ...], files: set[str]) -> str | None:
    """The scanned file an import specifier denotes, or `None` for a third-party or
    unresolvable one. Absolute names are tried from the importer's directory, the
    repo root, each scan root and each scan root's parent, which covers a `src/`
    layout, a package that is the root, and a script importing its sibling."""
    here = PurePosixPath(importer).parent.as_posix()
    if spec.startswith("."):
        dots = len(spec) - len(spec.lstrip("."))
        base = PurePosixPath(here)
        for _ in range(dots - 1):
            base = base.parent
        dotted = [p for p in spec[dots:].split(".") if p]
        base_str = base.as_posix()
        if not dotted:
            init = f"{base_str}/__init__.py" if base_str != "." else "__init__.py"
            return init if init in files else None
        for cand in _candidates_py(base_str, dotted):
            if cand in files:
                return cand
        return None
    dotted = [p for p in spec.split(".") if p]
    if not dotted:
        return None
    bases: list[str] = [here, "."]
    for r in roots:
        bases.append(r)
        bases.append(PurePosixPath(r).parent.as_posix())
    seen: set[str] = set()
    for base_dir in bases:
        if base_dir in seen:
            continue
        seen.add(base_dir)
        for cand in _candidates_py(base_dir, dotted):
            if cand in files:
                return cand
    return None


_JS_TRY = tuple(sorted(scan_mod.JS_SUFFIXES))


def resolve_js(spec: str, importer: str, roots: tuple[str, ...], files: set[str]) -> str | None:
    """The scanned file a JS specifier denotes. Relative and the `@/`, `~/` aliases
    only; a bare package name is third-party and never a graph edge."""
    if spec.startswith(("./", "../", ".")):
        here = PurePosixPath(importer).parent
        target = os.path.normpath((here / spec).as_posix()).replace("\\", "/")
        bases = [target]
    elif spec.startswith(("@/", "~/")):
        rest = spec[2:]
        bases = []
        for r in roots:
            bases.append(f"{r}/{rest}" if r != "." else rest)
            bases.append(f"{r}/src/{rest}" if r != "." else f"src/{rest}")
    else:
        return None
    for base in bases:
        if base.startswith("../"):
            continue
        candidates = [base]
        candidates.extend(f"{base}{sfx}" for sfx in _JS_TRY)
        candidates.extend(f"{base}/index{sfx}" for sfx in _JS_TRY)
        stem, dot, ext = base.rpartition(".")
        if dot and ext in ("js", "jsx", "mjs", "cjs"):
            candidates.extend(f"{stem}{sfx}" for sfx in (".ts", ".tsx", ".mts", ".cts"))
        for cand in candidates:
            if cand in files:
                return cand
    return None


def import_graph(
    metrics: dict[str, scan_mod.FileMetrics], roots: tuple[str, ...]
) -> dict[str, set[str]]:
    """`importer -> {imported}` over the scanned files; edges to third-party code
    are dropped, self-imports too."""
    files = set(metrics)
    graph: dict[str, set[str]] = {rel: set() for rel in files}
    for rel, m in metrics.items():
        py = PurePosixPath(rel).suffix in scan_mod.PY_SUFFIXES
        for spec in m.modules:
            target = (
                resolve_python(spec, rel, roots, files)
                if py
                else resolve_js(spec, rel, roots, files)
            )
            if target and target != rel:
                graph[rel].add(target)
    return graph


def strongly_connected(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan, iterative: a frontend's import graph outruns the recursion limit."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0
    for start in sorted(graph):
        if start in index:
            continue
        work: list[tuple[str, list[str]]] = [(start, sorted(graph[start]))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, todo = work[-1]
            if todo:
                child = todo.pop()
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, sorted(graph.get(child, ()))))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    comp.append(member)
                    if member == node:
                        break
                components.append(sorted(comp))
    return components


def cycles(graph: dict[str, set[str]]) -> list[Finding]:
    """One finding per cycle, keyed by its first member, valued by its size -- so
    the key survives a member being added, and the value catches it."""
    found = []
    for comp in strongly_connected(graph):
        if len(comp) > 1:
            found.append(Finding("cycle", comp[0], len(comp), detail=" -> ".join(comp)))
    return sorted(found, key=lambda f: f.key)


def is_entrypoint(rel: str, text: str, cfg: harness_config.Config) -> bool:
    path = PurePosixPath(rel)
    stem = path.name.split(".", 1)[0]
    if (
        stem in ENTRY_STEMS
        or path.name.split(".")[0].endswith((".config",))
        or ".config." in path.name
    ):
        return True
    if any(part in ENTRY_PARTS for part in path.parts[:-1]):
        return True
    if any(
        rel.startswith(e.strip("/") + "/") or rel == e.strip("/")
        for e in cfg.structure.entrypoints
        if e
    ):
        return True
    return bool(_MAIN_GUARD.search(text))


def orphans(
    graph: dict[str, set[str]], texts: dict[str, str], cfg: harness_config.Config
) -> list[Finding]:
    """Modules with no importer outside the tests. A test is not a user: a module
    only a test imports is dead code with a test that keeps it warm."""
    imported: set[str] = set()
    for importer, targets in graph.items():
        if not is_test(importer, cfg):
            imported |= targets
    found = []
    for rel in sorted(graph):
        if rel in imported or is_test(rel, cfg) or is_entrypoint(rel, texts.get(rel, ""), cfg):
            continue
        found.append(Finding("orphan", rel, 1))
    return found


# --- Policy ------------------------------------------------------------------------------


def _under(rel: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        rel.startswith(p.strip("/") + "/") or rel == p.strip("/") for p in prefixes if p.strip("/")
    )


def layer_violations(
    metrics: dict[str, scan_mod.FileMetrics],
    graph: dict[str, set[str]],
    cfg: harness_config.Config,
) -> list[Finding]:
    found = []
    for rule in cfg.structure.layers:
        forbid = tuple(f.strip("/") for f in rule.forbid if f.strip("/"))
        for rel in sorted(metrics):
            if not _under(rel, rule.sources):
                continue
            hits = []
            for spec in metrics[rel].modules:
                if any(spec == f or spec.startswith((f + ".", f + "/")) for f in forbid):
                    hits.append(spec)
            for target in sorted(graph.get(rel, ())):
                if _under(target, forbid):
                    hits.append(target)
            if hits:
                found.append(
                    Finding(
                        "layer",
                        rel,
                        len(hits),
                        symbol=rule.name,
                        detail=", ".join(dict.fromkeys(hits)),
                    )
                )
    return found


def restrict_violations(texts: dict[str, str], cfg: harness_config.Config) -> list[Finding]:
    found = []
    for rule in cfg.structure.restrict:
        try:
            pattern = re.compile(rule.pattern)
        except re.error:
            continue
        for rel in sorted(texts):
            if rule.paths and not _under(rel, rule.paths):
                continue
            if _under(rel, rule.only_in):
                continue
            count = len(pattern.findall(texts[rel]))
            if count:
                found.append(Finding("restrict", rel, count, symbol=rule.name))
    return found


def _pep508_name(entry: Any) -> str | None:
    if not isinstance(entry, str):
        return None
    m = _DEP_NAME.match(entry)
    return m.group(1).lower().replace("_", "-") if m else None


def _pyproject_dependencies(root: Path) -> set[str]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return set()
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A broken manifest is the packager's finding, not this gate's.
        return set()
    names: set[str] = set()
    project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
    for entry in project.get("dependencies", []) or []:
        if (n := _pep508_name(entry)) is not None:
            names.add(n)
    for group in (project.get("optional-dependencies", {}) or {}).values():
        for entry in group or []:
            if (n := _pep508_name(entry)) is not None:
                names.add(n)
    for group in (data.get("dependency-groups", {}) or {}).values():
        for entry in group or []:
            if (n := _pep508_name(entry)) is not None:
                names.add(n)
    poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
    if isinstance(poetry, dict):
        tables = [poetry.get("dependencies", {})]
        for grp in (poetry.get("group", {}) or {}).values():
            if isinstance(grp, dict):
                tables.append(grp.get("dependencies", {}))
        for table in tables:
            if isinstance(table, dict):
                names.update(k.lower().replace("_", "-") for k in table if k.lower() != "python")
    return names


def _requirements_dependencies(root: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in sorted(root.glob("requirements*.txt")):
        names: set[str] = set()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            if (n := _pep508_name(line)) is not None:
                names.add(n)
        if names:
            out[path.name] = names
    return out


def _package_json_dependencies(path: Path) -> set[str]:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        table = data.get(key, {}) if isinstance(data, dict) else {}
        if isinstance(table, dict):
            names.update(str(k) for k in table)
    return names


def declared_dependencies(root: Path, cfg: harness_config.Config) -> dict[str, set[str]]:
    """`manifest -> {package}` across the manifests a project of this shape carries."""
    out: dict[str, set[str]] = {}
    if names := _pyproject_dependencies(root):
        out["pyproject.toml"] = names
    out.update(_requirements_dependencies(root))
    for rel in dict.fromkeys(("package.json", f"{cfg.frontend.dir.strip('/')}/package.json")):
        path = root / rel
        if path.is_file() and (names := _package_json_dependencies(path)):
            out[str(PurePosixPath(rel))] = names  # `dir = "."` spells one file two ways
    return out


def dependency_findings(root: Path, cfg: harness_config.Config) -> list[Finding]:
    found: list[Finding] = []
    for manifest, names in sorted(declared_dependencies(root, cfg).items()):
        found.extend(Finding("dependency", manifest, 1, symbol=name) for name in sorted(names))
    return found


# --- The ratchet ---------------------------------------------------------------------


def findings(root: Path, cfg: harness_config.Config) -> list[Finding]:
    """Every current finding, one per key (the larger value wins a collision), sorted."""
    disabled = set(cfg.structure.disabled)
    lim = limits(cfg)
    texts = read_sources(root, cfg)
    metrics: dict[str, scan_mod.FileMetrics] = {}
    for rel, text in texts.items():
        m = scan_mod.scan(rel, text)
        if m is not None:
            metrics[rel] = m
    roots = scan_roots(cfg)
    raw: list[Finding] = []
    for rel, m in metrics.items():
        raw.extend(measure(rel, m, lim, is_test(rel, cfg)))
    graph = import_graph({r: m for r, m in metrics.items()}, roots)
    raw.extend(cycles({r: t for r, t in graph.items() if not is_test(r, cfg)}))
    raw.extend(orphans(graph, texts, cfg))
    raw.extend(layer_violations(metrics, graph, cfg))
    raw.extend(restrict_violations(texts, cfg))
    raw.extend(dependency_findings(root, cfg))
    by_key: dict[str, Finding] = {}
    for f in raw:
        if f.rule in disabled:
            continue
        if f.key not in by_key or f.value > by_key[f.key].value:
            by_key[f.key] = f
    return [by_key[k] for k in sorted(by_key)]


def baseline_path(root: Path) -> Path:
    return root / BASELINE_NAME


def read_baseline(path: Path) -> dict[str, int]:
    """`key -> value`; comments, blanks and malformed lines dropped."""
    if not path.exists():
        return {}
    out: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.rpartition(" = ")
        if sep and value.strip().isdigit():
            out[key.strip()] = int(value)
    return out


def render_baseline(entries: dict[str, int], notes: list[str] | None = None) -> str:
    """The whole file: header, the sorted findings, then the `--record` log.

    The log goes *after* the entries so the sorted block stays a clean diff -- a note
    inserted between two lines would move every line under it.
    """
    body = BASELINE_HEADER + "".join(f"{k} = {entries[k]}\n" for k in sorted(entries))
    if not notes:
        return body
    return body + "\n" + RECORD_MARKER + "\n" + "\n\n".join(notes) + "\n"


def seed(root: Path, cfg: harness_config.Config | None = None) -> int | None:
    """Write the baseline for a project adopting the gate. `None` if one exists:
    re-seeding would launder everything added since adoption into the debt list."""
    path = baseline_path(root)
    if path.exists():
        return None
    entries = {f.key: f.value for f in findings(root, cfg or harness_config.load(root))}
    path.write_text(render_baseline(entries), encoding="utf-8", newline="\n")
    return len(entries)


def verdict(
    root: Path, cfg: harness_config.Config
) -> tuple[list[Finding], list[tuple[str, int, int | None]]]:
    """`(worse, stale)`. `worse` is every finding that is new or grew; `stale` is
    `(key, recorded, current)` for every line the code no longer earns, `current`
    being `None` when the finding is gone. Both empty is the passing state."""
    current = {f.key: f for f in findings(root, cfg)}
    recorded = read_baseline(baseline_path(root))
    worse = [f for k, f in current.items() if k not in recorded or f.value > recorded[k]]
    stale: list[tuple[str, int, int | None]] = []
    for key, value in sorted(recorded.items()):
        now = current.get(key)
        if now is None:
            stale.append((key, value, None))
        elif now.value < value:
            stale.append((key, value, now.value))
    return worse, stale


# Rules `--record` will not move, whatever the reason given. Every one of them counts a
# thing somebody chose to write -- a lint suppression, a deferred-work comment, a skipped
# test, a swallowed exception -- so "the module is already large" is never the
# explanation, and the fix is always available. The limit rules are different in kind:
# `file_lines` on a module twelve times its limit is a fact about a split nobody has
# done yet, and a bug fix landing in that module cannot be asked to do the split first.
#
# Written without the literal tokens those rules match, because this module is scanned
# by the scanner it configures: naming them here would make this comment a finding.
UNRECORDABLE = frozenset(scan_mod.COUNTERS)


def recordable(worse: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """`worse`, split into what `--record` may move and what it must refuse."""
    allowed = [f for f in worse if f.rule not in UNRECORDABLE]
    refused = [f for f in worse if f.rule in UNRECORDABLE]
    return allowed, refused


def record(
    root: Path, cfg: harness_config.Config, reason: str
) -> tuple[list[Finding], list[Finding]]:
    """Move the baseline up to what the code now earns, with `reason` written down.

    The deliberate hole in the ratchet, and it exists because the ratchet had none. The
    baseline pins the *current* `file_lines`, `definitions` and `imports` of modules that
    are 2x-12x their limits, so those modules cannot gain a line, a function or an import
    -- and a bug whose fix belongs in one of them has nowhere to go. A `/triage-harness`
    sweep verified eight live defects and could land two: six were in modules the gate had
    sealed, one of them blocked by a single `import` statement. The advice the finding
    prints ("split the module along the seam its imports already show") is right and is
    also a separate body of work; asking a bug fix to carry a 6000-line module's split is
    how a gate gets switched off instead.

    Three things keep this from being the laundering `--seed`'s refusal exists to stop.
    The reason is **mandatory** and non-blank, exactly as `harness_triage --resolve`
    requires `--note`. Counter rules are refused outright (see `UNRECORDABLE`) -- those
    are always somebody's decision to give up, never a module's size. And every move is
    written into the file as a dated comment naming what moved and why, so the PR diff
    carries the justification rather than a bare number that got bigger.

    Returns `(recorded, refused)`.
    """
    if not reason.strip():
        raise ValueError("--record needs a reason: say why the growth is the honest answer")
    path = baseline_path(root)
    worse, _stale = verdict(root, cfg)
    allowed, refused = recordable(worse)
    if refused or not allowed:
        return [], refused
    entries = read_baseline(path)
    entries.update({f.key: f.value for f in allowed})
    stamp = datetime.datetime.now(datetime.UTC).date().isoformat()
    note = "\n".join(
        [f"# {stamp}  recorded: {reason.strip()}"]
        + [f"#   {f.key} -> {f.value}" for f in sorted(allowed, key=lambda f: f.key)]
    )
    path.write_text(
        render_baseline(entries, [*existing_notes(path), note]), encoding="utf-8", newline="\n"
    )
    return allowed, []


def existing_notes(path: Path) -> list[str]:
    """The `--record` notes already in the file, so a later one appends rather than wins.

    Read back out of the file rather than kept beside it: a second state file would be
    one more thing that can disagree with the baseline it annotates, and the whole point
    of this tier is that one file is the record.
    """
    if not path.exists():
        return []
    body = path.read_text(encoding="utf-8")
    _, marker, tail = body.partition(RECORD_MARKER)
    if not marker:
        return []
    blocks = [b.strip("\n") for b in tail.split("\n\n") if b.strip()]
    return [b for b in blocks if b.startswith("#")]


def tighten(root: Path, cfg: harness_config.Config) -> tuple[int, int]:
    """Rewrite the baseline so it is exactly as loose as the code needs and no looser:
    drop lines whose finding is gone, lower values that shrank. `(dropped, lowered)`.
    Never adds a line or raises a value -- that is what `worse` is for."""
    path = baseline_path(root)
    recorded = read_baseline(path)
    current = {f.key: f.value for f in findings(root, cfg)}
    kept: dict[str, int] = {}
    dropped = lowered = 0
    for key, value in recorded.items():
        if key not in current:
            dropped += 1
        elif current[key] < value:
            kept[key] = current[key]
            lowered += 1
        else:
            kept[key] = value
    if dropped or lowered:
        # The `--record` log is carried through: it explains lines that are still here,
        # and a tighten that silently dropped it would leave the next reader with raised
        # numbers and no account of who raised them.
        path.write_text(render_baseline(kept, existing_notes(path)), encoding="utf-8", newline="\n")
    return dropped, lowered


def describe(f: Finding, lim: dict[str, int]) -> str:
    limit = f" (limit {lim[f.rule]})" if f.rule in lim else ""
    where = f" [{f.detail}]" if f.detail else ""
    return f"{f.key} = {f.value}{limit}{where} -- {REMEDIES.get(f.rule, '')}"


def report(
    worse: list[Finding], stale: list[tuple[str, int, int | None]], lim: dict[str, int]
) -> str:
    """The artifact body: everything an agent needs to fix the gate, one line each."""
    lines = []
    if worse:
        lines.append(f"{len(worse)} finding(s) new or worse than {BASELINE_NAME}:")
        lines.extend(f"  {describe(f, lim)}" for f in worse)
    if stale:
        lines.append(f"{len(stale)} line(s) in {BASELINE_NAME} no longer earned (run --tighten):")
        for key, recorded, now in stale:
            state = "gone" if now is None else f"now {now}"
            lines.append(f"  {key} = {recorded} ({state})")
    if not lines:
        lines.append("structure-check: clean.")
    return "\n".join(lines) + "\n"


def write_artifact(root: Path, body: str) -> Path:
    path = root / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def run_record(reason: str) -> int | None:
    """`--record`'s whole branch: an exit code to return, or None to carry on checking.

    Its own function because `main` is one branch under its complexity limit and this is
    four -- which is the gate doing its job on the change that adds the escape to it.
    """
    try:
        recorded, refused = record(REPO_ROOT, CFG, reason)
    except ValueError as exc:
        print(f"structure-check: {exc}")
        return 2
    if refused:
        print("structure-check: these are never a module's size -- fix them, do not record:")
        for f in refused:
            print(f"  {describe(f, limits(CFG))}")
        return 2
    if not recorded:
        print(f"structure-check: nothing to record; {BASELINE_NAME} already covers the code.")
        return 0
    for f in recorded:
        print(f"structure-check: recorded {f.key} = {f.value}")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed", action="store_true", help=f"write {BASELINE_NAME}; refuses to overwrite"
    )
    parser.add_argument(
        "--tighten",
        action="store_true",
        help="drop and lower baseline lines the code no longer earns",
    )
    parser.add_argument(
        "--list", action="store_true", help="print every current finding, recorded or not"
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="raise the baseline to what the code now earns; needs --reason",
    )
    parser.add_argument(
        "--reason", default="", help="with --record: why the growth is the honest answer"
    )
    args = parser.parse_args(argv)

    errors = config_errors(CFG)
    if errors:
        for err in errors:
            print(f"structure-check: {err}")
        return 2

    if args.seed:
        count = seed(REPO_ROOT, CFG)
        if count is None:
            print(f"structure-check: {BASELINE_NAME} already exists; refusing to overwrite.")
            return 1
        print(f"structure-check: recorded {count} finding(s) in {BASELINE_NAME}.")
        return 0

    if args.list:
        for f in findings(REPO_ROOT, CFG):
            print(describe(f, limits(CFG)))
        return 0

    if args.record and (code := run_record(args.reason)) is not None:
        return code

    if args.tighten:
        dropped, lowered = tighten(REPO_ROOT, CFG)
        print(f"structure-check: dropped {dropped} line(s), lowered {lowered} in {BASELINE_NAME}.")

    worse, stale = verdict(REPO_ROOT, CFG)
    body = report(worse, stale, limits(CFG))
    path = write_artifact(REPO_ROOT, body)
    if worse or stale:
        print(
            f"structure-check: {len(worse)} worse, {len(stale)} stale -- see {path.relative_to(REPO_ROOT).as_posix()}"
        )
        return 1
    print(
        f"structure-check: clean ({len(read_baseline(baseline_path(REPO_ROOT)))} known finding(s))."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
