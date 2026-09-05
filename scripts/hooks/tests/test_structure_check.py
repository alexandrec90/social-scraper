"""Tests for `structure_check.py` -- the structural ratchet.

Two halves, like `test_untested_symbols.py`:

- **The checker's units**, against synthetic projects under `tmp_path`: which files it
  reads, how an import resolves, what a cycle or an orphan is, and the ratchet itself.
- **The live gate**, run against this project: nothing new or worse than
  `.devkit-structure.txt`, and no line in it the code no longer earns.

Vendored, so everything that varies per project is read from `sc.CFG`.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from conftest import REPO_ROOT, load_module

sc = load_module("scripts/hooks/structure_check.py")
hc = sc.harness_config
ss = sc.scan_mod


def config(**kwargs) -> object:
    """A neutral `Config` whose app tree is `src/`, with `[structure]` overrides."""
    structure_fields = {f.name for f in dataclasses.fields(hc.StructureConfig)}
    structure = hc.StructureConfig(**{k: v for k, v in kwargs.items() if k in structure_fields})
    rest = {k: v for k, v in kwargs.items() if k not in structure_fields}
    return dataclasses.replace(hc.Config(), app_dir="src/", structure=structure, **rest)


def write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def long_function(name: str, lines: int) -> str:
    return f"def {name}():\n" + "".join(f"    x{i} = {i}\n" for i in range(lines - 1))


# --- configuration --------------------------------------------------------------


def test_limits_are_the_defaults_overridden_by_the_manifest():
    lim = sc.limits(config(limits={"file_lines": 100}))
    assert lim["file_lines"] == 100
    assert lim["complexity"] == sc.DEFAULT_LIMITS["complexity"]


def test_every_rule_has_a_remedy():
    assert set(sc.REMEDIES) == set(sc.RULES)


def test_config_errors_name_an_unknown_limit_and_an_unknown_rule():
    errors = sc.config_errors(config(limits={"file_lnies": 1}, disabled=("orpan",)))
    assert any("file_lnies" in e for e in errors)
    assert any("orpan" in e for e in errors)


def test_config_errors_reject_an_incomplete_layer_and_a_bad_regex():
    cfg = config(
        layers=(hc.LayerRule(name="x", sources=("src/ui",)),),
        restrict=(hc.RestrictRule(name="y", pattern="(unclosed"),),
    )
    errors = sc.config_errors(cfg)
    assert len(errors) == 2


def test_config_errors_is_empty_for_a_well_formed_manifest():
    cfg = config(
        limits={"complexity": 5},
        disabled=("orphan",),
        layers=(hc.LayerRule(name="x", sources=("src/ui",), forbid=("src/db",)),),
        restrict=(hc.RestrictRule(name="y", pattern=r"\bfetch\(", only_in=("src/api",)),),
    )
    assert sc.config_errors(cfg) == []


def test_scan_roots_default_to_the_app_tree_scripts_and_the_tests():
    assert sc.scan_roots(config()) == ("src", "scripts", "tests")


def test_scan_roots_include_the_frontend_when_it_is_enabled():
    cfg = dataclasses.replace(config(), frontend=hc.FrontendConfig(enabled=True, src="web/src/"))
    assert sc.scan_roots(cfg) == ("src", "scripts", "web/src", "tests")


def test_configured_paths_replace_the_default_but_the_tests_stay():
    assert sc.scan_roots(config(paths=("lib/",))) == ("lib", "tests")


# --- which files --------------------------------------------------------------------


def test_vendored_paths_are_empty_where_there_is_no_stamp(tmp_path):
    write(tmp_path, "scripts/sync-devkit.py", 'MANIFEST = ["a.py"]\n')
    assert sc.vendored_paths(tmp_path) == frozenset()


def test_vendored_paths_are_read_off_the_manifest_literal(tmp_path):
    write(tmp_path, "DEVKIT_VERSION", "abc\n")
    write(
        tmp_path, "scripts/sync-devkit.py", 'X = 1\nMANIFEST = [\n    "a.py",\n    "b/c.py",\n]\n'
    )
    assert sc.vendored_paths(tmp_path) == {"a.py", "b/c.py"}


def test_vendored_paths_survive_a_script_that_does_not_parse(tmp_path):
    write(tmp_path, "DEVKIT_VERSION", "abc\n")
    write(tmp_path, "scripts/sync-devkit.py", "def (:\n")
    assert sc.vendored_paths(tmp_path) == frozenset()


def test_source_files_pick_both_languages_and_skip_tooling_and_declarations(tmp_path):
    write(tmp_path, "src/a.py")
    write(tmp_path, "src/b.ts")
    write(tmp_path, "src/c.d.ts")
    write(tmp_path, "src/vendor.min.js")
    write(tmp_path, "src/node_modules/x.js")
    write(tmp_path, "src/readme.md")
    write(tmp_path, "tests/test_a.py")
    assert sc.source_files(tmp_path, config()) == ["src/a.py", "src/b.ts", "tests/test_a.py"]


def test_source_files_honour_exclude_as_a_prefix_or_a_directory_name(tmp_path):
    write(tmp_path, "src/gen/a.py")
    write(tmp_path, "src/migrations/b.py")
    write(tmp_path, "src/c.py")
    files = sc.source_files(tmp_path, config(exclude=("src/gen/", "migrations")))
    assert files == ["src/c.py"]


def test_source_files_skip_what_devkit_vendors(tmp_path):
    write(tmp_path, "DEVKIT_VERSION", "abc\n")
    write(tmp_path, "scripts/sync-devkit.py", 'MANIFEST = ["scripts/hooks/stop.py"]\n')
    write(tmp_path, "scripts/hooks/stop.py", "x = 1\n")
    write(tmp_path, "scripts/mine.py", "x = 1\n")
    files = sc.source_files(tmp_path, config())
    assert "scripts/mine.py" in files
    assert "scripts/hooks/stop.py" not in files


def test_is_test_reads_the_configured_tests_dir_too():
    cfg = dataclasses.replace(config(), tests_dir="spec/")
    assert sc.is_test("spec/anything.py", cfg)
    assert sc.is_test("src/__tests__/a.ts", cfg)
    assert not sc.is_test("src/a.py", cfg)


def test_read_sources_skips_a_file_too_big_to_be_source(tmp_path, monkeypatch):
    write(tmp_path, "src/a.py", "x = 1\n")
    write(tmp_path, "src/bundle.js", "x" * 50)
    monkeypatch.setattr(sc, "MAX_FILE_BYTES", 20)
    assert list(sc.read_sources(tmp_path, config())) == ["src/a.py"]


# --- limits and counters --------------------------------------------------------


def test_measure_reports_only_what_is_over_a_limit():
    metrics = ss.scan("a.py", long_function("alpha", 10) + "\n" + long_function("beta", 5))
    found = sc.measure("a.py", metrics, {**sc.DEFAULT_LIMITS, "function_lines": 8}, test=False)
    assert [(f.rule, f.symbol, f.value) for f in found] == [("function_lines", "alpha", 10)]


def test_measure_reads_only_the_counters_of_a_test_file():
    metrics = ss.scan("t.py", long_function("alpha", 10) + "x = 1  # noqa\n")
    found = sc.measure("t.py", metrics, {**sc.DEFAULT_LIMITS, "function_lines": 2}, test=True)
    assert [(f.rule, f.value) for f in found] == [("suppressions", 1)]


def test_measure_uses_the_component_limit_for_a_component():
    src = "export function Alpha() {\n" + "  const a = 1;\n" * 5 + "}\n"
    metrics = ss.scan("A.tsx", src)
    lim = {**sc.DEFAULT_LIMITS, "component_lines": 3, "function_lines": 100}
    assert [f.rule for f in sc.measure("A.tsx", metrics, lim, test=False)] == ["component_lines"]


def test_measure_reports_class_size_and_method_count():
    src = "class Alpha:\n" + "".join(f"    def m{i}(self): pass\n" for i in range(4))
    metrics = ss.scan("a.py", src)
    lim = {**sc.DEFAULT_LIMITS, "class_lines": 3, "class_methods": 3}
    assert {f.rule for f in sc.measure("a.py", metrics, lim, test=False)} == {
        "class_lines",
        "class_methods",
    }


# --- the import graph ---------------------------------------------------------------


def test_resolve_python_follows_relative_imports():
    files = {"src/pkg/__init__.py", "src/pkg/a.py", "src/pkg/sub/b.py", "src/top.py"}
    assert sc.resolve_python(".a", "src/pkg/sub/b.py", ("src",), files) is None
    assert sc.resolve_python("..a", "src/pkg/sub/b.py", ("src",), files) == "src/pkg/a.py"
    assert sc.resolve_python(".", "src/pkg/a.py", ("src",), files) == "src/pkg/__init__.py"
    assert sc.resolve_python(".sub.b", "src/pkg/a.py", ("src",), files) == "src/pkg/sub/b.py"


def test_resolve_python_tries_the_root_the_scan_roots_and_their_parents():
    files = {"src/pkg/a.py", "src/top.py", "scripts/tool.py"}
    assert sc.resolve_python("pkg.a", "src/top.py", ("src",), files) == "src/pkg/a.py"
    assert sc.resolve_python("src.pkg.a", "scripts/tool.py", ("src",), files) == "src/pkg/a.py"
    assert sc.resolve_python("pkg.a.name", "src/top.py", ("src",), files) == "src/pkg/a.py"
    assert (
        sc.resolve_python("tool", "scripts/other.py", ("src", "scripts"), files)
        == "scripts/tool.py"
    )


def test_resolve_python_returns_none_for_third_party():
    assert sc.resolve_python("requests", "src/a.py", ("src",), {"src/a.py"}) is None


def test_resolve_js_infers_extensions_indexes_and_the_ts_behind_a_js_specifier():
    files = {"web/src/a.ts", "web/src/lib/index.tsx", "web/src/b.tsx", "web/src/c.jsx"}
    assert sc.resolve_js("./a", "web/src/main.ts", ("web/src",), files) == "web/src/a.ts"
    assert sc.resolve_js("./lib", "web/src/main.ts", ("web/src",), files) == "web/src/lib/index.tsx"
    assert sc.resolve_js("../b.js", "web/src/x/y.ts", ("web/src",), files) == "web/src/b.tsx"
    assert sc.resolve_js("./c.jsx", "web/src/main.ts", ("web/src",), files) == "web/src/c.jsx"


def test_resolve_js_maps_the_at_alias_onto_a_scan_root():
    files = {"web/src/a.ts"}
    assert sc.resolve_js("@/a", "web/src/deep/x.ts", ("web/src",), files) == "web/src/a.ts"
    assert sc.resolve_js("@/a", "web/src/deep/x.ts", ("web",), files) == "web/src/a.ts"


def test_resolve_js_returns_none_for_a_package_or_an_escape_above_the_root():
    assert sc.resolve_js("react", "web/src/a.ts", ("web/src",), {"web/src/a.ts"}) is None
    assert sc.resolve_js("../../../x", "web/src/a.ts", ("web/src",), {"x.ts"}) is None


def _metrics(sources: dict[str, str]) -> dict[str, object]:
    return {rel: ss.scan(rel, text) for rel, text in sources.items()}


def test_import_graph_links_resolved_imports_only():
    graph = sc.import_graph(
        _metrics({"src/a.py": "import b\nimport requests\n", "src/b.py": "from . import a\n"}),
        ("src",),
    )
    assert graph == {"src/a.py": {"src/b.py"}, "src/b.py": {"src/a.py"}}


def test_strongly_connected_finds_the_cycle_and_leaves_the_rest_alone():
    graph = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}}
    comps = sorted(sc.strongly_connected(graph))
    assert comps == [["a", "b", "c"], ["d"]]


def test_cycles_are_keyed_by_first_member_and_valued_by_size():
    found = sc.cycles({"src/b.py": {"src/a.py"}, "src/a.py": {"src/b.py"}, "src/c.py": set()})
    assert [(f.key, f.value) for f in found] == [("cycle::src/a.py", 2)]
    assert "src/b.py" in found[0].detail


def test_orphans_are_modules_nothing_outside_the_tests_imports():
    graph = {
        "src/used.py": set(),
        "src/dead.py": set(),
        "src/main.py": {"src/used.py"},
        "tests/test_dead.py": {"src/dead.py"},
    }
    found = sc.orphans(graph, {}, config())
    assert [f.key for f in found] == ["orphan::src/dead.py"]


def test_is_entrypoint_exempts_runners_by_stem_directory_guard_and_config():
    cfg = config(entrypoints=("src/plugins/",))
    assert sc.is_entrypoint("src/main.py", "", cfg)
    assert sc.is_entrypoint("web/src/app/page.tsx", "", cfg)
    assert sc.is_entrypoint("web/vite.config.ts", "", cfg)
    assert sc.is_entrypoint("scripts/anything.py", "", cfg)
    assert sc.is_entrypoint("src/tool.py", 'if __name__ == "__main__":\n    run()\n', cfg)
    assert sc.is_entrypoint("src/plugins/x.py", "", cfg)
    assert not sc.is_entrypoint("src/x.py", "", cfg)


# --- policy -------------------------------------------------------------------------------


def test_layer_violations_catch_a_resolved_path_and_a_module_prefix():
    cfg = config(
        layers=(hc.LayerRule(name="ui-pure", sources=("src/ui",), forbid=("src/db", "requests")),)
    )
    sources = {
        "src/ui/view.py": "from src.db import q\nimport requests.adapters\nimport os\n",
        "src/db/q.py": "",
        "src/other.py": "import requests\n",
    }
    metrics = _metrics(sources)
    found = sc.layer_violations(metrics, sc.import_graph(metrics, ("src",)), cfg)
    assert [(f.key, f.value) for f in found] == [("layer::src/ui/view.py::ui-pure", 2)]


def test_restrict_violations_allow_the_pattern_only_where_the_rule_says():
    cfg = config(
        restrict=(
            hc.RestrictRule(
                name="storage", pattern=r"\blocalStorage\b", only_in=("web/src/storage",)
            ),
            hc.RestrictRule(
                name="sql", pattern=r"\bexecute\(", paths=("src/",), only_in=("src/repo",)
            ),
        )
    )
    texts = {
        "web/src/storage/wrap.ts": "localStorage.getItem('x')",
        "web/src/View.tsx": "localStorage.setItem('x', 1); localStorage.clear()",
        "src/repo/q.py": "cur.execute(sql)",
        "src/svc.py": "cur.execute(sql)",
        "scripts/tool.py": "cur.execute(sql)",
    }
    found = sc.restrict_violations(texts, cfg)
    assert [(f.key, f.value) for f in found] == [
        ("restrict::web/src/View.tsx::storage", 2),
        ("restrict::src/svc.py::sql", 1),
    ]


def test_declared_dependencies_read_every_manifest_a_project_carries(tmp_path):
    write(
        tmp_path,
        "pyproject.toml",
        '[project]\ndependencies = ["Requests>=2", "pydantic[email]==2"]\n'
        '[project.optional-dependencies]\ndev = ["pytest"]\n'
        '[dependency-groups]\nlint = ["ruff", {include-group = "dev"}]\n'
        '[tool.poetry.dependencies]\npython = "^3.12"\nhttpx = "*"\n',
    )
    write(tmp_path, "requirements.txt", "# pinned\nnumpy==1\n-r base.txt\n")
    write(tmp_path, "package.json", '{"devDependencies": {"prettier": "1"}}')
    write(
        tmp_path,
        "web/package.json",
        '{"dependencies": {"react": "18"}, "peerDependencies": {"vue": "3"}}',
    )
    cfg = dataclasses.replace(config(), frontend=hc.FrontendConfig(dir="web"))
    deps = sc.declared_dependencies(tmp_path, cfg)
    assert deps == {
        "pyproject.toml": {"requests", "pydantic", "pytest", "ruff", "httpx"},
        "requirements.txt": {"numpy"},
        "package.json": {"prettier"},
        "web/package.json": {"react", "vue"},
    }


def test_a_frontend_at_the_repository_root_is_one_manifest_not_two(tmp_path):
    """`dir = "."` must produce the same key the root manifest already has.

    These strings are baseline keys, so a second spelling of one file is the worst
    shape a ratchet finding can take: every dependency in it is reported as new, and
    every line already recorded under the other spelling goes stale in the same run --
    a project that has added nothing is told to justify its entire dependency list.
    roguelike is the shape that reaches it: a Phaser game whose `package.json`,
    `vite.config.ts` and `src/` are the repository root, so it declares `dir = "."`
    rather than a subdirectory that does not exist.
    """
    write(tmp_path, "package.json", '{"dependencies": {"phaser": "4"}}')
    cfg = dataclasses.replace(config(), frontend=hc.FrontendConfig(enabled=True, dir="."))

    assert sc.declared_dependencies(tmp_path, cfg) == {"package.json": {"phaser"}}
    assert [f.key for f in sc.dependency_findings(tmp_path, cfg)] == [
        "dependency::package.json::phaser"
    ]


def test_declared_dependencies_survive_a_manifest_that_does_not_parse(tmp_path):
    write(tmp_path, "pyproject.toml", "[project\n")
    write(tmp_path, "package.json", "{")
    assert sc.declared_dependencies(tmp_path, config()) == {}


def test_dependency_findings_are_one_line_per_package(tmp_path):
    write(tmp_path, "package.json", '{"dependencies": {"zeta": "1", "alpha": "2"}}')
    keys = [f.key for f in sc.dependency_findings(tmp_path, config())]
    assert keys == ["dependency::package.json::alpha", "dependency::package.json::zeta"]


# --- the ratchet ---------------------------------------------------------------------


def test_findings_are_keyed_deduped_and_sorted(tmp_path):
    write(
        tmp_path,
        "src/main.py",
        "def alpha():\n    pass\n\n\nclass K:\n    def alpha(self, a, b, c):\n        pass\n",
    )
    found = sc.findings(tmp_path, config(limits={"function_params": 1}))
    assert [(f.key, f.value) for f in found] == [("function_params::src/main.py::alpha", 3)]


def test_findings_honour_disabled_rules(tmp_path):
    write(tmp_path, "src/dead.py", "x = 1  # noqa\n")
    assert [f.rule for f in sc.findings(tmp_path, config())] == ["orphan", "suppressions"]
    assert [f.rule for f in sc.findings(tmp_path, config(disabled=("orphan",)))] == ["suppressions"]


def test_the_finding_key_omits_an_empty_symbol():
    assert sc.Finding("orphan", "src/a.py", 1).key == "orphan::src/a.py"
    assert sc.Finding("complexity", "src/a.py", 9, symbol="f").key == "complexity::src/a.py::f"


def test_baseline_round_trips_and_the_reader_drops_junk(tmp_path):
    path = tmp_path / sc.BASELINE_NAME
    path.write_text(sc.render_baseline({"b::x = y = z": 2, "a::x": 1}), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#")
    assert text.index("a::x = 1") < text.index("b::x = y = z = 2")
    path.write_text(text + "\nnot a line\nk = notanumber\n", encoding="utf-8")
    assert sc.read_baseline(path) == {"a::x": 1, "b::x = y = z": 2}
    assert sc.read_baseline(tmp_path / "missing") == {}


def test_seed_writes_the_baseline_once(tmp_path):
    write(tmp_path, "src/dead.py", "x = 1  # noqa\n")
    assert sc.seed(tmp_path, config()) == 2
    assert sc.read_baseline(sc.baseline_path(tmp_path)) == {
        "orphan::src/dead.py": 1,
        "suppressions::src/dead.py": 1,
    }
    assert sc.seed(tmp_path, config()) is None


def test_seed_writes_lf_line_endings(tmp_path):
    write(tmp_path, "src/dead.py", "x = 1\n")
    sc.seed(tmp_path, config())
    assert b"\r" not in sc.baseline_path(tmp_path).read_bytes()


def test_verdict_flags_a_new_key_and_a_grown_value(tmp_path):
    write(tmp_path, "src/main.py", "x = 1  # noqa\ny = 2  # noqa\n")
    write(tmp_path, "src/dead.py", "x = 1\n")
    write(tmp_path, sc.BASELINE_NAME, "suppressions::src/main.py = 1\n")
    worse, stale = sc.verdict(tmp_path, config())
    assert [(f.key, f.value) for f in worse] == [
        ("orphan::src/dead.py", 1),
        ("suppressions::src/main.py", 2),
    ]
    assert stale == []


def test_verdict_flags_a_line_the_code_no_longer_earns(tmp_path):
    write(tmp_path, "src/main.py", "x = 1  # noqa\n")
    write(tmp_path, sc.BASELINE_NAME, "suppressions::src/main.py = 3\norphan::src/gone.py = 1\n")
    worse, stale = sc.verdict(tmp_path, config())
    assert worse == []
    assert stale == [("orphan::src/gone.py", 1, None), ("suppressions::src/main.py", 3, 1)]


def test_verdict_passes_when_the_baseline_is_exact(tmp_path):
    write(tmp_path, "src/main.py", "x = 1  # noqa\n")
    write(tmp_path, sc.BASELINE_NAME, "suppressions::src/main.py = 1\n")
    assert sc.verdict(tmp_path, config()) == ([], [])


def test_tighten_drops_and_lowers_but_never_adds_or_raises(tmp_path):
    write(tmp_path, "src/main.py", "x = 1  # noqa\ny = 2  # noqa\n")
    write(tmp_path, "src/dead.py", "x = 1\n")
    write(tmp_path, sc.BASELINE_NAME, "suppressions::src/main.py = 5\norphan::src/gone.py = 1\n")
    assert sc.tighten(tmp_path, config()) == (1, 1)
    assert sc.read_baseline(sc.baseline_path(tmp_path)) == {"suppressions::src/main.py": 2}
    worse, stale = sc.verdict(tmp_path, config())
    assert [f.key for f in worse] == ["orphan::src/dead.py"]
    assert stale == []


def _oversized(tmp_path, lines: int = 40) -> None:
    """A module past `file_lines`, which is the rule `--record` exists for."""
    write(tmp_path, "src/big.py", "import os\n" + "x = 1\n" * lines + "print(os)\n")


def test_record_raises_the_baseline_and_writes_the_reason_into_the_file(tmp_path):
    """The deliberate hole. A module far past `file_lines` cannot gain a line, so a bug
    whose fix belongs in one has nowhere to go -- and the split the finding asks for is a
    separate body of work. Recording says so out loud rather than silently."""
    _oversized(tmp_path)
    write(tmp_path, sc.BASELINE_NAME, "")

    recorded, refused = sc.record(
        tmp_path, config(limits={"file_lines": 10}), "the split is its own PR"
    )

    assert refused == []
    assert "file_lines::src/big.py" in {f.key for f in recorded}
    assert sc.verdict(tmp_path, config(limits={"file_lines": 10})) == ([], [])
    body = sc.baseline_path(tmp_path).read_text(encoding="utf-8")
    assert "the split is its own PR" in body
    assert "file_lines::src/big.py ->" in body


def test_recordable_splits_the_findings_by_whether_size_can_explain_them():
    """The predicate `record` refuses on, asserted directly: a limit rule is a fact about
    a split nobody has done, a counter is somebody's decision to give up."""
    big = sc.Finding("file_lines", "src/big.py", 900)
    gave_up = sc.Finding("suppressions", "src/a.py", 1)
    assert sc.recordable([big, gave_up]) == ([big], [gave_up])
    assert sc.recordable([]) == ([], [])


def test_existing_notes_reads_back_only_the_record_log(tmp_path):
    """Read out of the file rather than kept beside it: a second state file would be one
    more thing that can disagree with the baseline it annotates."""
    path = tmp_path / sc.BASELINE_NAME
    path.write_text(sc.render_baseline({"a::x": 1}), encoding="utf-8")
    assert sc.existing_notes(path) == []
    assert sc.existing_notes(tmp_path / "missing") == []

    path.write_text(sc.render_baseline({"a::x": 1}, ["# one", "# two"]), encoding="utf-8")
    assert sc.existing_notes(path) == ["# one", "# two"]


def test_run_record_reports_the_refusal_and_the_missing_reason(tmp_path, monkeypatch, capsys):
    """`main`'s branch, which is where the exit codes are decided: 2 for a blank reason
    and 2 for a counter rule, so neither can be mistaken for a recorded success."""
    monkeypatch.setattr(sc, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sc, "CFG", config(limits={"file_lines": 10}))
    write(tmp_path, "src/main.py", "x = 1  # noqa\n")
    write(tmp_path, sc.BASELINE_NAME, "")

    assert sc.run_record("") == 2
    assert "needs a reason" in capsys.readouterr().out

    assert sc.run_record("a reason") == 2
    assert "do not record" in capsys.readouterr().out


def test_record_refuses_without_a_reason(tmp_path):
    """Mandatory, exactly as `harness_triage --resolve` requires `--note`: a number that
    got bigger with no account of why is the laundering this must not become."""
    _oversized(tmp_path)
    write(tmp_path, sc.BASELINE_NAME, "")
    for blank in ("", "   ", "\n"):
        with pytest.raises(ValueError, match="needs a reason"):
            sc.record(tmp_path, config(limits={"file_lines": 10}), blank)


def test_record_refuses_a_counter_rule_whatever_the_reason(tmp_path):
    """A `# noqa`, a `TODO`, a skipped test: every counter is somebody's decision to give
    up, so "the module is already large" is never the explanation and the fix is always
    available. Nothing is written -- not even the recordable findings beside it."""
    _oversized(tmp_path)
    write(tmp_path, "src/main.py", "x = 1  # noqa\n")
    path = write(tmp_path, sc.BASELINE_NAME, "")
    before = path.read_text(encoding="utf-8")

    recorded, refused = sc.record(tmp_path, config(limits={"file_lines": 10}), "a good reason")

    assert recorded == []
    assert [f.key for f in refused] == ["suppressions::src/main.py"]
    assert path.read_text(encoding="utf-8") == before


def test_recording_twice_keeps_both_reasons(tmp_path):
    """The log is append-only for the reason the reap ledger is: a record of a decision
    that the next decision overwrites is not a record."""
    _oversized(tmp_path)
    write(tmp_path, sc.BASELINE_NAME, "")
    sc.record(tmp_path, config(limits={"file_lines": 10}), "first reason")
    _oversized(tmp_path, lines=80)
    sc.record(tmp_path, config(limits={"file_lines": 10}), "second reason")

    body = sc.baseline_path(tmp_path).read_text(encoding="utf-8")
    assert "first reason" in body and "second reason" in body


def test_tighten_carries_the_record_log_through(tmp_path):
    """A tighten that dropped the log would leave the next reader with raised numbers and
    no account of who raised them."""
    _oversized(tmp_path)
    write(tmp_path, sc.BASELINE_NAME, "orphan::src/gone.py = 1\n")
    sc.record(tmp_path, config(limits={"file_lines": 10}), "kept through a tighten")

    assert sc.tighten(tmp_path, config(limits={"file_lines": 10}))[0] == 1
    assert "kept through a tighten" in sc.baseline_path(tmp_path).read_text(encoding="utf-8")


def test_a_recorded_baseline_still_reads_back_as_numbers(tmp_path):
    """The log is comments, so `read_baseline` must be blind to it."""
    _oversized(tmp_path)
    write(tmp_path, sc.BASELINE_NAME, "")
    sc.record(tmp_path, config(limits={"file_lines": 10}), "why")
    entries = sc.read_baseline(sc.baseline_path(tmp_path))
    assert entries and all(isinstance(v, int) for v in entries.values())


def test_tighten_leaves_an_exact_baseline_untouched(tmp_path):
    write(tmp_path, "src/main.py", "x = 1  # noqa\n")
    path = write(tmp_path, sc.BASELINE_NAME, "suppressions::src/main.py = 1\n")
    before = path.stat().st_mtime_ns
    assert sc.tighten(tmp_path, config()) == (0, 0)
    assert path.stat().st_mtime_ns == before


def test_describe_names_the_limit_the_place_and_the_remedy():
    line = sc.describe(
        sc.Finding("complexity", "src/a.py", 20, symbol="f", detail="line 3"), sc.DEFAULT_LIMITS
    )
    assert line.startswith("complexity::src/a.py::f = 20 (limit 15) [line 3] -- ")
    assert sc.REMEDIES["complexity"] in line


def test_report_covers_both_failure_kinds_and_the_clean_case():
    worse = [sc.Finding("orphan", "src/a.py", 1)]
    stale = [("cycle::src/b.py", 3, 2), ("todos::src/c.py", 1, None)]
    body = sc.report(worse, stale, sc.DEFAULT_LIMITS)
    assert "1 finding(s) new or worse" in body
    assert "orphan::src/a.py = 1" in body
    assert "cycle::src/b.py = 3 (now 2)" in body
    assert "todos::src/c.py = 1 (gone)" in body
    assert "--tighten" in body
    assert sc.report([], [], sc.DEFAULT_LIMITS).strip() == "structure-check: clean."


def test_write_artifact_creates_the_logs_directory(tmp_path):
    path = sc.write_artifact(tmp_path, "body\n")
    assert path == tmp_path / sc.ARTIFACT
    assert path.read_text(encoding="utf-8") == "body\n"


# --- main ---------------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    def install(**kwargs):
        monkeypatch.setattr(sc, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(sc, "CFG", config(**kwargs))
        return tmp_path

    return install


def test_main_passes_on_a_project_whose_debt_is_recorded(project, capsys):
    root = project()
    write(root, "src/main.py", "x = 1  # noqa\n")
    sc.seed(root, sc.CFG)
    assert sc.main([]) == 0
    assert "clean (1 known" in capsys.readouterr().out
    assert (root / sc.ARTIFACT).read_text(encoding="utf-8").startswith("structure-check: clean")


def test_main_fails_and_points_at_the_artifact(project, capsys):
    root = project()
    write(root, "src/main.py", "x = 1  # noqa\n")
    write(root, sc.BASELINE_NAME, "")
    assert sc.main([]) == 1
    out = capsys.readouterr().out
    assert "1 worse, 0 stale" in out
    assert sc.ARTIFACT in out
    assert "suppressions::src/main.py = 1" in (root / sc.ARTIFACT).read_text(encoding="utf-8")


def test_main_seed_writes_and_then_refuses(project, capsys):
    root = project()
    write(root, "src/main.py", "x = 1  # noqa\n")
    assert sc.main(["--seed"]) == 0
    assert "recorded 1 finding" in capsys.readouterr().out
    assert sc.main(["--seed"]) == 1
    assert "refusing to overwrite" in capsys.readouterr().out


def test_main_list_prints_every_finding_recorded_or_not(project, capsys):
    root = project()
    write(root, "src/main.py", "x = 1  # noqa\n")
    write(root, sc.BASELINE_NAME, "suppressions::src/main.py = 1\n")
    assert sc.main(["--list"]) == 0
    assert capsys.readouterr().out.startswith("suppressions::src/main.py = 1")


def test_main_tighten_then_judges(project, capsys):
    root = project()
    write(root, "src/main.py", "x = 1\n")
    write(root, sc.BASELINE_NAME, "suppressions::src/main.py = 1\n")
    assert sc.main(["--tighten"]) == 0
    out = capsys.readouterr().out
    assert "dropped 1 line(s), lowered 0" in out
    assert "clean (0 known" in out


def test_main_exits_two_on_a_configuration_error(project, capsys):
    project(limits={"nope": 1})
    assert sc.main([]) == 2
    assert "[structure.limits] nope" in capsys.readouterr().out


# --- the live gate -----------------------------------------------------------------


BASELINE = sc.baseline_path(REPO_ROOT)
adopted = pytest.mark.skipif(
    not BASELINE.exists(),
    reason=f"no {sc.BASELINE_NAME}; adopt with `python scripts/hooks/structure_check.py --seed`",
)


def test_the_manifest_configures_the_gate_correctly():
    """Config errors are exit 2 from the script; here they are a red test, so a typo in
    `[structure]` cannot silently widen or narrow what the gate holds."""
    assert sc.config_errors(sc.CFG) == []


@adopted
def test_nothing_is_new_or_worse_than_the_baseline():
    worse, _ = sc.verdict(REPO_ROOT, sc.CFG)
    lim = sc.limits(sc.CFG)
    assert not worse, "fix the code, do not add to the baseline:\n" + "\n".join(
        sc.describe(f, lim) for f in worse
    )


@adopted
def test_the_baseline_holds_only_what_the_code_still_earns():
    _, stale = sc.verdict(REPO_ROOT, sc.CFG)
    assert not stale, (
        f"run `python scripts/hooks/structure_check.py --tighten` to drop these from {sc.BASELINE_NAME}: "
        + ", ".join(key for key, _, _ in stale)
    )


@adopted
def test_the_baseline_is_sorted_and_unique():
    lines = [
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines == sorted(set(lines))
