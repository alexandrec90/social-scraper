"""Unit tests for the per-project harness config loader.

Covers the pure `from_dict` mapping and its tolerance of malformed/partial
manifests, plus a contract test that the committed `.devkit.toml` is
internally coherent (so a manifest edit that would silently change harness
behaviour fails here instead of at runtime).

**This file is vendored into every consuming project**, so nothing here may assert
a value that is specific to one project. It previously pinned carameli's literal
credentials, paths, and skill list, which made the whole vendored suite red in any
repo whose manifest differed — the exact opposite of the portability the harness
exists for. Assert *invariants* of a manifest, never one project's contents.
"""

import os
import sys
from pathlib import Path

from conftest import REPO_ROOT, load_module

cfg = load_module("scripts/hooks/harness_config.py")


def test_defaults_are_a_minimal_neutral_harness():
    c = cfg.Config()
    assert c.env_prefix == "DEVKIT"
    assert c.db.enabled is False
    assert c.frontend.enabled is False
    # Neutral defaults describe a generic Python project, not carameli.
    assert c.app_dir == "app/" and c.unit_tests == "tests/unit"


def test_env_prefixes_control_vars():
    c = cfg.Config(env_prefix="CARAMELI")
    assert c.env("SKIP_STOP_VERIFY") == "CARAMELI_SKIP_STOP_VERIFY"
    assert c.env("STOP_TESTS_AUTOSTART") == "CARAMELI_STOP_TESTS_AUTOSTART"


def test_from_dict_empty_is_defaults():
    assert cfg.from_dict({}) == cfg.Config()


def test_from_dict_maps_full_manifest():
    c = cfg.from_dict(
        {
            "project": {"env_prefix": "FOO"},
            "paths": {"app": "src/", "tests": "t/", "unit_tests": "t/unit"},
            "db": {
                "enabled": True,
                "services": ["db", "cache"],
                "db_service": "db",
                "db_port": 6000,
                "redis_service": "cache",
                "redis_port": 7000,
                "user": "u",
                "password": "p",
                "name": "n",
                "url_scheme": "postgresql+asyncpg",
                "url_env": ["DATABASE_URL", "DIRECT_DATABASE_URL"],
                "redis_env": "CACHE_URL",
                "test_env": {"API_KEY_SECRET": "k"},
            },
            "frontend": {"enabled": True, "dir": "web", "src": "web/src/"},
        }
    )
    assert c.env_prefix == "FOO"
    assert c.app_dir == "src/" and c.unit_tests == "t/unit"
    assert c.db.enabled is True
    assert c.db.services == ("db", "cache")
    assert c.db.db_port == 6000 and c.db.redis_service == "cache"
    assert c.db.url_env == ("DATABASE_URL", "DIRECT_DATABASE_URL")
    assert c.db.redis_env == "CACHE_URL"
    assert c.db.test_env == {"API_KEY_SECRET": "k"}
    assert c.frontend.enabled is True and c.frontend.dir == "web"


def test_from_dict_tolerates_wrong_types():
    # Non-dict sections and wrong-typed fields fall back to defaults, never raise.
    c = cfg.from_dict(
        {
            "project": "nope",
            "db": {"services": "not-a-list"},
            "frontend": [1, 2],
        }
    )
    assert c.env_prefix == "DEVKIT"
    assert c.db.services == ("db", "redis")  # bad `services` -> default
    assert c.frontend.enabled is False


def test_load_missing_manifest_returns_defaults(tmp_path: Path):
    assert cfg.load(tmp_path) == cfg.Config()


def test_load_parses_real_toml(tmp_path: Path):
    (tmp_path / cfg.MANIFEST_NAME).write_text(
        '[project]\nenv_prefix = "ZED"\n[db]\nenabled = true\nservices = ["db"]\n'
    )
    c = cfg.load(tmp_path)
    assert c.env_prefix == "ZED"
    assert c.db.enabled is True and c.db.services == ("db",)


def test_load_malformed_toml_returns_defaults(tmp_path: Path):
    (tmp_path / cfg.MANIFEST_NAME).write_text("this is = = not valid toml [[[")
    assert cfg.load(tmp_path) == cfg.Config()


# --- contract: the committed manifest reproduces carameli's constants ---------


def test_repo_manifest_loads_and_is_coherent():
    """This repo's own `.devkit.toml` must load and hold together.

    Portable replacement for a test that pinned carameli's literal values. It still
    catches the failures that matter — a manifest that no longer parses, or a
    half-filled block — without asserting anything project-specific.
    """
    c = cfg.load(REPO_ROOT)

    # A blank prefix would make every control var `_SKIP_STOP_VERIFY`, colliding
    # across projects on one machine.
    assert c.env_prefix and c.env_prefix.isupper()
    assert c.env("SKIP_STOP_VERIFY").startswith(f"{c.env_prefix}_")

    # Directory-ish fields must be usable as path prefixes: `stop.py` decides which
    # checks to run with `path.startswith(CFG.app_dir)`, so a missing trailing slash
    # makes `apps/` match `app/` and silently widens the test selection.
    assert c.app_dir.endswith("/")
    assert c.tests_dir.endswith("/")
    assert c.unit_tests

    if c.db.enabled:
        # A half-filled DB block yields a URL like `postgresql://:@host:5432/`,
        # which fails at connect time with a message that points nowhere.
        assert c.db.user and c.db.name and c.db.password
        assert c.db.url_scheme and c.db.url_env
        assert c.db.db_service in c.db.services

    if c.frontend.enabled:
        assert c.frontend.dir and c.frontend.test_cmd
        # `src` gates the vitest tier by prefix match, same trailing-slash logic.
        assert c.frontend.src.endswith("/")


# --- [python] install_command: the dependency-model escape hatch ---------------
# session-start.sh normally *detects* the model from files on disk; this field is
# only for a project that fits none of the known shapes. Its job is to be absent
# (and harmless) far more often than it is set.


def test_python_install_command_defaults_to_empty_so_detection_wins():
    assert cfg.Config().python.install_command == ""
    assert cfg.from_dict({}).python.install_command == ""


def test_python_install_command_is_read_from_the_manifest():
    c = cfg.from_dict({"python": {"install_command": "make deps"}})
    assert c.python.install_command == "make deps"


def test_malformed_python_section_degrades_to_default():
    # Same never-raises contract as every other section: a typo must not break
    # provisioning, it must fall back to detection.
    for bad in ({"python": "uv sync"}, {"python": []}, {"python": {"install_command": 7}}):
        assert isinstance(cfg.from_dict(bad).python.install_command, str)


# --- [python] version: the interpreter the marker files do not name -----------
# A lockfile pins packages, not the interpreter. Empty means "use whatever is
# running the provisioner", which is what every project got unconditionally before
# this field existed.


def test_python_version_defaults_to_empty_so_the_running_interpreter_is_used():
    assert cfg.Config().python.version == ""
    assert cfg.from_dict({}).python.version == ""


def test_python_version_is_read_from_the_manifest():
    assert cfg.from_dict({"python": {"version": "3.12"}}).python.version == "3.12"


def test_an_unquoted_python_version_still_reads_as_a_string():
    """`version = 3.12` in TOML parses as a float, and it is the spelling an author
    reaches for first. It has to reach `uv venv --python` as "3.12", not as a float
    that would blow up argv construction."""
    assert cfg.from_dict({"python": {"version": 3.12}}).python.version == "3.12"


def test_python_version_is_addressable_from_the_shell_lookup():
    c = cfg.from_dict({"python": {"version": "3.12"}})
    assert cfg.lookup(c, "python.version") == "3.12"


# --- [bash]: the PreToolUse output cap ----------------------------------------
# Read by both enforce-capped-bash.py (the number it quotes when blocking) and
# invoke-capped.py (the cap it actually applies). They must agree, which is why
# there is one field and not two constants.


def test_bash_defaults_are_a_usable_cap():
    assert cfg.Config().bash.max_bytes == 4000
    assert cfg.Config().bash.head_bytes == 2000
    # head must fit inside the cap or the tail window is negative.
    assert cfg.Config().bash.head_bytes <= cfg.Config().bash.max_bytes


def test_bash_values_are_read_from_the_manifest():
    c = cfg.from_dict({"bash": {"max_bytes": 12000, "head_bytes": 3000}})
    assert c.bash.max_bytes == 12000
    assert c.bash.head_bytes == 3000


def test_bash_partial_block_keeps_the_other_default():
    c = cfg.from_dict({"bash": {"max_bytes": 8000}})
    assert c.bash.max_bytes == 8000
    assert c.bash.head_bytes == 2000


def test_malformed_bash_section_degrades_to_defaults():
    # Same never-raises contract as every other section.
    for bad in ({"bash": "4000"}, {"bash": []}, {"bash": {"max_bytes": "wide"}}):
        c = cfg.from_dict(bad)
        assert c.bash.max_bytes == 4000


def test_bash_rejects_bool_which_is_an_int_subclass():
    """`max_bytes = true` must not silently become a 1-byte cap.

    bool is a subclass of int, so a naive int() accepts it and every Bash call
    would come back as a truncation marker with no output.
    """
    c = cfg.from_dict({"bash": {"max_bytes": True}})
    assert c.bash.max_bytes == 4000


def test_bash_accepts_a_numeric_string():
    # TOML gives an int, but a hand-edited manifest may quote it.
    assert cfg.from_dict({"bash": {"max_bytes": "9000"}}).bash.max_bytes == 9000


def test_lookup_returns_scalars_and_empty_for_anything_else():
    c = cfg.from_dict({"python": {"install_command": "uv sync"}, "project": {"env_prefix": "X"}})
    assert cfg.lookup(c, "python.install_command") == "uv sync"
    assert cfg.lookup(c, "env_prefix") == "X"
    # Booleans come out lowercase: the only consumer is shell, which wants
    # `[ "$v" = "true" ]`, not Python's "True".
    assert cfg.lookup(c, "db.enabled") == "false"
    assert cfg.lookup(cfg.from_dict({"db": {"enabled": True}}), "db.enabled") == "true"
    # Unknown path and non-scalar both yield "" so a shell caller can use one
    # `[ -n "$v" ]` test for "no value" and "no such field" alike.
    assert cfg.lookup(c, "nope") == ""
    assert cfg.lookup(c, "python.nope.deeper") == ""


def test_cli_prints_the_value_for_shell_callers(tmp_path):
    """session-start.sh shells out to this; it must exit 0 and print a bare value."""
    import subprocess
    import sys

    (tmp_path / ".devkit.toml").write_text(
        '[python]\ninstall_command = "poetry install --with dev"\n', encoding="utf-8"
    )
    script = REPO_ROOT / "scripts" / "hooks" / "harness_config.py"
    run = subprocess.run(
        [sys.executable, str(script), "python.install_command"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0
    assert run.stdout.strip() == "poetry install --with dev"


def test_cli_exits_zero_with_no_manifest_and_no_args(tmp_path):
    # A hook must never die over config. No manifest, no argv -> empty line, rc 0.
    import subprocess
    import sys

    script = REPO_ROOT / "scripts" / "hooks" / "harness_config.py"
    for argv in ([], ["python.install_command"], ["totally.unknown"]):
        run = subprocess.run(
            [sys.executable, str(script), *argv], cwd=tmp_path, capture_output=True, text=True
        )
        assert run.returncode == 0, run.stderr
        assert run.stdout.strip() == ""


# --- [docker]: the opt-in the workspace's stop-idle pass reads ------------------


def test_docker_auto_stop_defaults_off():
    """Opt-in: the key licenses stopping this project's stack unattended, so absence
    -- of the key, the table, or the whole manifest -- must read as "keep it up"."""
    assert cfg.Config().docker.auto_stop is False
    assert cfg.from_dict({}).docker.auto_stop is False
    assert cfg.from_dict({"docker": {}}).docker.auto_stop is False


def test_docker_auto_stop_requires_the_literal_true():
    """`1`, `"yes"` and `"true"` are typos, and a typo in a key that licenses stopping
    a stack must land on the safe side."""
    assert cfg.from_dict({"docker": {"auto_stop": True}}).docker.auto_stop is True
    for junk in ("yes", "true", 1, [True], None):
        assert cfg.from_dict({"docker": {"auto_stop": junk}}).docker.auto_stop is False


def test_a_docker_table_of_the_wrong_shape_is_ignored():
    assert cfg.from_dict({"docker": "auto_stop"}).docker.auto_stop is False


# --- [worktree]: the `.env` values a box must derive for itself -----------------


def test_worktree_env_defaults_to_nothing():
    """Absence means "the seeded `.env` is already right", which is true of every
    project that has no value derived from a port."""
    assert cfg.Config().worktree.env == {}
    assert cfg.from_dict({}).worktree.env == {}
    assert cfg.from_dict({"worktree": {}}).worktree.env == {}


def test_worktree_env_is_carried_through_verbatim():
    """The templates are expanded by the caller that knows the box's ports, not here."""
    raw = {"worktree": {"env": {"CORS_ORIGINS": "http://localhost:${FRONTEND_HOST_PORT}"}}}
    assert cfg.from_dict(raw).worktree.env == {
        "CORS_ORIGINS": "http://localhost:${FRONTEND_HOST_PORT}"
    }


def test_worktree_env_values_are_coerced_to_strings():
    """TOML gives an int for `PORT = 5176`; a non-string reaching the `.env` writer
    would render by luck of repr and never match a template."""
    assert cfg.from_dict({"worktree": {"env": {"PORT": 5176}}}).worktree.env == {"PORT": "5176"}


def test_a_worktree_table_of_the_wrong_shape_is_ignored():
    for junk in ("env", {"env": "CORS_ORIGINS"}, {"env": ["a"]}, None):
        assert cfg.from_dict({"worktree": junk}).worktree.env == {}


def test_the_default_worktree_env_is_not_shared_between_configs():
    """`dict` default on a frozen dataclass: a mutable default shared across instances
    would let one project's manifest leak into the next box devkit cuts."""
    first = cfg.Config()
    first.worktree.env["X"] = "1"
    assert cfg.Config().worktree.env == {}


def test_ui_services_default_to_none_which_means_no_ui_only_mode():
    """Absence is the opt-out: a project that never declared a UI tier cannot have a
    UI-only preview cut for it, rather than getting one that starts nothing."""
    assert cfg.Config().worktree.ui_services == ()
    assert cfg.from_dict({"worktree": {}}).worktree.ui_services == ()


def test_ui_services_are_read_as_a_string_tuple():
    raw = {"worktree": {"ui_services": ["frontend", "storybook"]}}
    assert cfg.from_dict(raw).worktree.ui_services == ("frontend", "storybook")


def test_ui_services_of_the_wrong_shape_fall_back_to_the_default():
    for junk in ("frontend", {"a": 1}, 5, None):
        assert cfg.from_dict({"worktree": {"ui_services": junk}}).worktree.ui_services == ()


def test_ui_env_is_carried_through_verbatim_and_coerced_like_env():
    """Same contract as `env`: templates expand downstream, values become strings."""
    raw = {"worktree": {"ui_env": {"VITE_PROXY_TARGET": "http://host:${APP_HOST_PORT}", "N": 1}}}
    assert cfg.from_dict(raw).worktree.ui_env == {
        "VITE_PROXY_TARGET": "http://host:${APP_HOST_PORT}",
        "N": "1",
    }


def test_ui_env_of_the_wrong_shape_falls_back_to_the_default():
    for junk in ("VITE_X", ["a"], 5, None):
        assert cfg.from_dict({"worktree": {"ui_env": junk}}).worktree.ui_env == {}


# --- [structure] ------------------------------------------------------------------
#
# The structural ratchet's manifest table. Every field is optional and every default
# is "the script decides", so a project that never writes the table still gets the
# gate at the script's limits.


def test_structure_defaults_leave_everything_to_the_script():
    st = cfg.Config().structure
    assert st == cfg.StructureConfig()
    assert st.limits == {}
    assert st.layers == ()
    assert st.restrict == ()


def test_structure_table_is_mapped_including_from_which_python_cannot_name():
    st = cfg.from_dict(
        {
            "structure": {
                "paths": ["app/", "lib/"],
                "exclude": ["app/generated/"],
                "entrypoints": ["app/plugins/"],
                "disabled": ["orphan"],
                "limits": {"file_lines": 400, "complexity": 10},
                "layers": [{"name": "ui", "from": ["app/ui"], "forbid": ["app/db", "sqlalchemy"]}],
                "restrict": [
                    {
                        "name": "storage",
                        "pattern": "localStorage",
                        "only_in": ["web/storage"],
                        "paths": ["web"],
                    }
                ],
            }
        }
    ).structure
    assert st.paths == ("app/", "lib/")
    assert st.exclude == ("app/generated/",)
    assert st.entrypoints == ("app/plugins/",)
    assert st.disabled == ("orphan",)
    assert st.limits == {"file_lines": 400, "complexity": 10}
    assert st.layers == (
        cfg.LayerRule(name="ui", sources=("app/ui",), forbid=("app/db", "sqlalchemy")),
    )
    assert st.restrict == (
        cfg.RestrictRule(
            name="storage", pattern="localStorage", only_in=("web/storage",), paths=("web",)
        ),
    )


def test_structure_limits_drop_what_is_not_a_whole_number():
    limits = cfg.from_dict(
        {"structure": {"limits": {"file_lines": 400, "complexity": "10", "imports": True}}}
    )
    assert limits.structure.limits == {"file_lines": 400}


def test_structure_rules_of_the_wrong_shape_are_skipped_not_fatal():
    st = cfg.from_dict(
        {
            "structure": {
                "layers": ["ui", {"name": "ok", "from": ["a"], "forbid": ["b"]}],
                "restrict": "x",
            }
        }
    ).structure
    assert [r.name for r in st.layers] == ["ok"]
    assert st.restrict == ()


def test_a_structure_table_of_the_wrong_shape_is_ignored():
    assert cfg.from_dict({"structure": ["nope"]}).structure == cfg.StructureConfig()


# --- harness provenance -------------------------------------------------------
#
# What a hook stamps on the messages it sends an agent. The failure this exists to
# stop is not a crash: it is an agent tripping over a gate, reporting it upstream as
# a defect, and the report being closed as already-fixed because the copy that
# blocked it was months behind. Only the version distinguishes the two cases, so
# these tests pin that the stamp is either accurate or absent -- never a guess.


# A fabricated commit SHA, kept in one place: detect-secrets scores any long hex run as
# a high-entropy string and cannot tell a git SHA from a key. A SHA is public by nature,
# and this one names no commit that exists.
FAKE_SHA = "8a1894e2c3d4f5061728394a5b6c7d8e9f001122"  # pragma: allowlist secret


def stamped(tmp_path: Path, contents: str | None, *, name: str = "someproject") -> str:
    root = tmp_path / name
    root.mkdir(exist_ok=True)  # callers reuse one tmp_path across several stamps
    stamp = root / "DEVKIT_VERSION"
    if contents is None:
        stamp.unlink(missing_ok=True)
    else:
        stamp.write_text(contents, encoding="utf-8")
    return cfg.harness_version(root)


def test_a_vendored_copy_reports_its_pinned_sha(tmp_path):
    assert stamped(tmp_path, FAKE_SHA + "\n") == FAKE_SHA[:12]


def test_a_short_sha_is_reported_as_written(tmp_path):
    """`sync-devkit.py` writes a full SHA, but a hand-edited stamp is still a fact
    about this copy -- truncating to 12 must not become a floor of 12."""
    assert stamped(tmp_path, FAKE_SHA[:7]) == FAKE_SHA[:7]


def test_the_stamp_is_read_from_the_first_field(tmp_path):
    """The file has carried a trailing annotation before now; the SHA is field one."""
    assert stamped(tmp_path, f"{FAKE_SHA[:16]} v0.10.2 2026-08-14") == FAKE_SHA[:12]


def test_devkit_itself_reports_source_rather_than_a_version(tmp_path):
    """devkit vendors *out* of itself, so it carries no stamp. Saying `source` tells
    the agent this copy cannot be behind -- a defect here is worth reporting."""
    assert stamped(tmp_path, None, name="devkit") == "source"


def test_the_source_checkout_is_recognised_by_its_project_name_not_its_directory(tmp_path):
    """devkit develops itself in boxes under `.worktrees/devkit--<slug>/`, so the
    directory is named `devkit` in exactly the checkout nobody is working in."""
    root = tmp_path / "devkit--some-slug-0820"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "devkit"\n', encoding="utf-8")
    assert cfg.harness_version(root) == "source"


def test_a_project_that_merely_lives_in_a_directory_named_devkit_is_not_the_source(tmp_path):
    """The directory name is only the fallback; a manifest that names another project
    outranks it, or every consumer cloned to `~/devkit` would claim to be upstream."""
    root = tmp_path / "devkit"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "carameli"\n', encoding="utf-8")
    assert cfg.harness_version(root) == ""


def test_a_checkout_named_devkit_that_does_carry_a_stamp_is_a_consumer(tmp_path):
    """A stamp is positive evidence of vendoring and outranks either name check."""
    assert stamped(tmp_path, FAKE_SHA[:12], name="devkit") == FAKE_SHA[:12]


def test_an_unparseable_pyproject_falls_back_to_the_directory_name(tmp_path):
    """Never raises: this runs while a hook is already reporting something else."""
    root = tmp_path / "devkit"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project\nname = ", encoding="utf-8")
    assert cfg.harness_version(root) == "source"


def test_no_stamp_and_no_devkit_name_reports_nothing(tmp_path):
    """Callers omit the footer entirely rather than print a placeholder: a stamp that
    says `unknown` reads as a fact about the harness instead of a gap in it."""
    assert stamped(tmp_path, None) == ""


def test_junk_in_the_stamp_is_refused_rather_than_echoed(tmp_path):
    """This value lands in an agent's context verbatim. Anything that is not a SHA is
    someone else's file, and printing it would be the hook's own prompt injection."""
    for junk in ("", "   \n", "not-a-sha", "v0.10.2", "ghijklm", "abcdef"):
        assert stamped(tmp_path, junk) == "", junk


def test_a_missing_root_never_raises(tmp_path):
    """Hooks call this while already reporting something else; an exception here
    would replace a useful block message with a traceback."""
    assert cfg.harness_version(tmp_path / "nope") == ""


def test_the_footer_names_the_version_and_the_check_that_settles_it(tmp_path):
    root = tmp_path / "consumer"
    root.mkdir()
    (root / "DEVKIT_VERSION").write_text(FAKE_SHA[:16], encoding="utf-8")
    footer = cfg.provenance(root)
    assert FAKE_SHA[:12] in footer
    assert "sync-devkit.py --check" in footer
    assert footer.count("\n") == 0  # one line: it rides on every block


def test_the_footer_says_a_source_checkout_cannot_be_behind(tmp_path):
    root = tmp_path / "devkit"
    root.mkdir()
    footer = cfg.provenance(root)
    assert "source" in footer
    # Nothing to pull, so pointing at the drift check here would be a wasted turn.
    assert "sync-devkit.py --check" not in footer


def test_no_version_means_no_footer(tmp_path):
    assert cfg.provenance(tmp_path / "nope") == ""


# --- The harness kill switch (`DEVKIT_HOOKS_OFF`) ----------------------------------


def test_an_unset_switch_leaves_every_hook_running():
    """The default has to be "on" for a variable nobody has ever heard of."""
    for name in cfg.SWITCHABLE_HOOKS:
        assert cfg.hooks_off(name, {}) is False, name


def test_a_bare_one_switches_every_hook_off():
    """`DEVKIT_HOOKS_OFF=1` is what an operator reaches for first, so it has to mean
    the obvious thing rather than name a hook called `1`."""
    for name in cfg.SWITCHABLE_HOOKS:
        assert cfg.hooks_off(name, {cfg.HOOKS_OFF_ENV: "1"}) is True, name


def test_the_all_aliases_all_mean_every_hook():
    for value in ("all", "*", "true", "yes", "ON", " All "):
        assert cfg.hooks_off("stop", {cfg.HOOKS_OFF_ENV: value}) is True, value


def test_a_value_that_reads_as_off_to_a_human_never_switches_the_harness_off():
    """The same asymmetry `git_policy.SKIP_ENV_VAR` documents: someone writing
    `DEVKIT_HOOKS_OFF=0` means "leave the hooks running", and reading that as "off"
    would disable the harness for the person trying to turn it back on."""
    for value in ("", "0", "false", "no", "off", "  OFF  "):
        assert cfg.hooks_off("stop", {cfg.HOOKS_OFF_ENV: value}) is False, value


def test_a_list_switches_off_only_the_hooks_it_names():
    """Selective is the state the harness comes back through: the Stop gate is the
    expensive one and `lint-fix` is nearly free, so they are re-enabled apart."""
    env = {cfg.HOOKS_OFF_ENV: "stop, capped-bash"}
    assert cfg.hooks_off("stop", env) is True
    assert cfg.hooks_off("capped-bash", env) is True
    assert cfg.hooks_off("lint-fix", env) is False
    assert cfg.hooks_off("session-start", env) is False


def test_separators_and_casing_a_human_would_write_are_all_accepted():
    for value in ("Stop;lint-fix", " STOP , lint-fix ", "stop,lint-fix"):
        assert cfg.hooks_off("stop", {cfg.HOOKS_OFF_ENV: value}) is True, value
        assert cfg.hooks_off("lint-fix", {cfg.HOOKS_OFF_ENV: value}) is True, value


def test_an_unknown_name_in_the_list_switches_nothing_else_off():
    """A typo must fail closed -- towards the harness running -- never silently widen
    to every hook, which is the one direction that loses a guarantee."""
    env = {cfg.HOOKS_OFF_ENV: "stopp"}
    for name in cfg.SWITCHABLE_HOOKS:
        assert cfg.hooks_off(name, env) is False, name


def test_the_switch_reads_the_process_environment_when_none_is_passed(monkeypatch):
    """Hooks call it with no argument; the env is where a `settings.json` puts it."""
    monkeypatch.setenv(cfg.HOOKS_OFF_ENV, "stop")
    assert cfg.hooks_off("stop") is True
    assert cfg.hooks_off("lint-fix") is False


def test_the_shell_arm_answers_with_an_exit_code(monkeypatch):
    """`session-start.sh` and the `PostToolUseFailure` one-liner ask through the CLI
    rather than re-implementing the parse in shell -- a second copy of the aliases and
    the off-values asymmetry is exactly the drift this arm exists to prevent."""
    import subprocess

    script = REPO_ROOT / "scripts/hooks/harness_config.py"
    for value, expected in (("1", 0), ("session-start", 0), ("stop", 1), ("0", 1)):
        run = subprocess.run(
            [sys.executable, str(script), "--hook-off", "session-start"],
            capture_output=True,
            text=True,
            env={**os.environ, cfg.HOOKS_OFF_ENV: value},
        )
        assert run.returncode == expected, (value, run.returncode)


def test_the_lookup_arm_still_never_exits_non_zero():
    """Only the `--hook-off` arm may fail. The lookup arm is read by shell that has no
    handler for a failure, and a hook must not die over config."""
    import subprocess

    script = REPO_ROOT / "scripts/hooks/harness_config.py"
    for argv in ([], ["python.install_command"], ["no.such.field"]):
        run = subprocess.run([sys.executable, str(script), *argv], capture_output=True, text=True)
        assert run.returncode == 0, (argv, run.stderr)


def _explodes(*_args, **_kwargs):
    raise AssertionError("the hook did work after being switched off")


def test_the_stop_gate_returns_before_it_reads_anything(monkeypatch):
    """The reversion check for the switch's whole point: with it on, the Stop gate must
    not read stdin, shell out to git, or run a single check. This is the hook the switch
    was added for -- it reproduces the PR gate locally and blocks the session on what it
    finds, which is the cost an operator turning the harness off is turning off."""
    stop = load_module("scripts/hooks/stop.py")
    monkeypatch.setenv(cfg.HOOKS_OFF_ENV, "stop")
    monkeypatch.setattr(stop, "_read_stdin", _explodes)
    assert stop.main() == 0


def test_the_formatter_returns_before_it_reads_anything(monkeypatch):
    lint_fix = load_module("scripts/hooks/lint-fix.py")
    monkeypatch.setenv(cfg.HOOKS_OFF_ENV, "lint-fix")
    monkeypatch.setattr(lint_fix, "_read_stdin", _explodes)
    assert lint_fix.main() == 0


def test_the_bash_cap_returns_before_it_reads_anything(monkeypatch):
    capped = load_module("scripts/hooks/enforce-capped-bash.py")
    monkeypatch.setenv(cfg.HOOKS_OFF_ENV, "capped-bash")
    monkeypatch.setattr(capped, "_read_stdin", _explodes)
    assert capped.main([]) == 0


def test_the_branch_tier_shim_returns_before_it_resolves_devkit(monkeypatch):
    """`branch-tier` is the one switch name whose hook is a *shim*: it forwards the
    payload to a guard that lives in devkit. Standing it down has to happen before that
    subprocess, or an operator who switched the tier off still pays a Python start on
    every mutating call in every consuming project."""
    shim = load_module("scripts/hooks/worktree-guard-launch.py")
    monkeypatch.setenv(cfg.HOOKS_OFF_ENV, "branch-tier")
    monkeypatch.setattr(shim, "devkit_root", _explodes)
    assert shim.main([]) == 0


def test_every_switchable_hook_is_a_hook_that_actually_consults_the_switch():
    """`SWITCHABLE_HOOKS` is the documented vocabulary, so a name in it that no hook
    reads is a value an operator can set and watch do nothing. The two call sites that
    are not Python -- `session-start` and `failure-retro` -- go through the `--hook-off`
    arm, so search for either spelling."""
    sources = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (
            REPO_ROOT / "scripts/hooks/stop.py",
            REPO_ROOT / "scripts/hooks/lint-fix.py",
            REPO_ROOT / "scripts/hooks/enforce-capped-bash.py",
            REPO_ROOT / "scripts/hooks/worktree-guard-launch.py",
            REPO_ROOT / ".claude/hooks/session-start.sh",
            REPO_ROOT / ".claude/settings.json",
        )
    )
    for name in cfg.SWITCHABLE_HOOKS:
        assert f'hooks_off("{name}")' in sources or f"--hook-off {name}" in sources, name
