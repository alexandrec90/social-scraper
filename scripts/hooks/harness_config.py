#!/usr/bin/env python3
"""Per-project configuration for the shared agent-harness hook scripts.

The hook scripts (`stop.py`, and later the rest of `scripts/hooks/`) are meant to
be **vendored unchanged into every project**. Everything that differs between
projects -- the control-env prefix, the DB credentials/ports/service names, the
frontend layout, and the source-tree shape --
lives here, read from a committed `.devkit.toml` at the repo root. The
scripts stay shape-agnostic; a new project drops in a manifest instead of forking
the code.

Design contract:
  - **stdlib only** (`tomllib`, 3.11+). Hooks run before the venv is active.
  - **Never raises.** A missing/unparseable manifest, or an interpreter without
    `tomllib`, falls back to `Config()` defaults -- a minimal but valid harness
    (lint + script-tests, no DB tier, no frontend). A config typo must never
    break the Stop hook.
  - **Neutral defaults.** Defaults describe a generic Python project, not
    carameli; carameli's specifics come from its own `.devkit.toml`.

Pure and unit-tested in `scripts/hooks/tests/test_harness_config.py`.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

MANIFEST_NAME = ".devkit.toml"


@dataclass(frozen=True)
class DbConfig:
    """The DB-backed test tier (Tier 2b). `enabled=False` skips the tier entirely."""

    enabled: bool = False
    services: tuple[str, ...] = ("db", "redis")  # what "reachable"/"up" means
    db_service: str = "db"
    db_port: int = 5432
    redis_service: str = "redis"
    redis_port: int = 6379
    user: str = ""
    password: str = ""
    name: str = ""
    url_scheme: str = "postgresql+asyncpg"
    # Env var names the DB URL is exposed under (carameli needs two).
    url_env: tuple[str, ...] = ("DATABASE_URL",)
    redis_env: str = "REDIS_URL"
    # Extra env for host pytest: name -> default (an already-set os.environ wins).
    test_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FrontendConfig:
    """The frontend (vitest/tsc) tier. `enabled=False` skips all frontend checks."""

    enabled: bool = False
    dir: str = "frontend"
    src: str = "frontend/src/"  # prefix that gates the vitest tier
    skin: str = "frontend/src/skins"  # subtree whose change triggers a typecheck
    test_cmd: tuple[str, ...] = ("run", "test:run")
    typecheck_cmd: tuple[str, ...] = ("run", "typecheck")


# Floor for `invoke-capped.py --max-bytes`, enforced there and quoted by
# `enforce-capped-bash.py`'s block message. Below this a cap costs more than it saves:
# the truncation marker alone is ~30 bytes, and a window too small to hold one error
# line defeats the purpose. It lives here rather than in either hook because both need
# it and a second literal is how the message and the wrapper drift apart -- the same
# failure `test_block_message_quotes_the_configured_cap` already pins for the cap.
# Not a `BashConfig` field on purpose: it is a property of the wrapper, not a knob a
# project should turn down.
MIN_MAX_BYTES = 512


@dataclass(frozen=True)
class BashConfig:
    """The PreToolUse Bash output cap (`enforce-capped-bash.py`).

    There is no `enabled` flag on purpose: wiring the hook in
    `.claude/settings.json` *is* the opt-in, the same way `lint-fix.py` works. A
    project that does not want the gate does not wire it.

    `head_bytes` is how much of the cap goes to the *start* of the output; the
    remainder is the tail. Both windows are kept because the two useful parts of a
    long command's output are the first lines (what it was doing) and the last
    (how it failed) -- the middle is what an agent can afford to lose.
    """

    max_bytes: int = 4000
    head_bytes: int = 2000


@dataclass(frozen=True)
class PythonConfig:
    """How to provision this project's Python toolchain.

    Deliberately just an escape hatch. `session-start.sh` *detects* the dependency
    model from the files on disk (`uv.lock` -> uv sync, `requirements-dev.txt` ->
    pip-tools locks, else `pyproject.toml`), because a lockfile cannot drift from
    reality the way a manifest field can. Set `install_command` only for a project
    that fits none of those shapes; it then wins over detection.

    `version` is an override for the same reason. A lockfile pins packages, not the
    interpreter that resolves them, so `worktree.py provision` built every box's `.venv`
    from whatever interpreter happened to be running it -- the workstation default, not
    the version the project is pinned to in its `FROM python:` tag, its compiled locks,
    its type-checker config and CI. The box came out announcing itself provisioned with a
    venv the container does not match, and the mismatch surfaced later as an install or a
    type-check failure that reads as a broken branch rather than as the wrong interpreter.
    Provisioning now reads an exact pin out of `.python-version` or a `FROM python:` tag
    when this field is empty, so set it only where those disagree with what the box should
    run, or where the pin lives somewhere else entirely (`"3.12"`, or a full `"3.12.7"`).
    """

    install_command: str = ""
    version: str = ""


@dataclass(frozen=True)
class DockerConfig:
    """How the workspace's unattended Docker maintenance may treat this stack.

    Read by devkit's `docker-maint.py stop-idle`, which runs from the devkit
    checkout rather than from this repo -- the field lives here to give it a schema,
    a neutral default, and a place the contract test can verify the spelling.

    `auto_stop` is opt-in on purpose: False keeps the nightly pass away from this
    project's stack, and a collector-style project -- one whose containers do
    scheduled work with no client connected, which no connection check can tell
    apart from idle -- must never set it.
    """

    auto_stop: bool = False


@dataclass(frozen=True)
class TestContractConfig:
    """Which of this project's code the untested-symbol ratchet scans.

    `sources` defaults to the app directory plus `scripts/`, which is every project's
    own code and nothing it vendored. A project that keeps code elsewhere -- a second
    package, a `lib/` -- names those directories here rather than accepting a gate that
    silently covers half of it. `exclude` is for subtrees inside those directories that
    are genuinely not this project's to test: generated clients, a vendored library.

    Both are prefixes matched against the repo-relative POSIX path, so `scripts/hooks/`
    excludes the vendored tier wholesale and `scripts/hooks/harness_config.py` excludes
    one file.
    """

    sources: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayerRule:
    """One import boundary: files under `sources` may not import anything `forbid`
    names. `forbid` entries are module prefixes as written in the import (`app.db`,
    `axios`) or repo-relative path prefixes of the resolved file (`app/db/`)."""

    name: str = ""
    sources: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()


@dataclass(frozen=True)
class RestrictRule:
    """One call-site boundary: `pattern` (a regex over the file's text) may appear
    only in files under `only_in`. `paths` narrows which files are checked at all;
    empty means every scanned file. The shape behind "browser storage only through
    the wrapper", "no `fetch` outside the API client", "no SQL outside the
    repository layer"."""

    name: str = ""
    pattern: str = ""
    only_in: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class StructureConfig:
    """What `scripts/hooks/structure_check.py` scans, and the limits it holds to.

    `paths` defaults to the app directory, the frontend source tree when one is
    enabled, and `scripts/`; the test directory is always scanned for the counters
    (suppressions, skipped tests) but never measured for size. `exclude` and
    `entrypoints` are repo-relative path prefixes: the first drops a subtree from the
    scan (generated code, migrations), the second exempts one from the orphan rule
    (files loaded by name rather than by import). `disabled` names rules a project
    opts out of. `limits` overrides any default in the script's `DEFAULT_LIMITS`; an
    unknown key is a configuration error the script reports rather than ignores.
    """

    paths: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    entrypoints: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    limits: dict[str, int] = field(default_factory=dict)
    layers: tuple[LayerRule, ...] = ()
    restrict: tuple[RestrictRule, ...] = ()


@dataclass(frozen=True)
class WorktreeConfig:
    """Extra `.env` assignments an ephemeral box must make for itself.

    A box already gets its own port lease, but a setting *derived* from a port is
    still the source checkout's after seeding, and nothing notices until a browser
    does. carameli's `CORS_ORIGINS` is the case that found this: the box publishes
    its frontend on its own port, the seeded `.env` still names the primary's, and
    the app then refuses every request its own frontend makes -- as a CORS error in
    the console, which reads as an application bug rather than as a box that was
    provisioned half-configured.

    Ports are the only thing devkit knows generically, so this is a template map
    rather than a fixed key: `${NAME}` expands against the managed env devkit
    already writes -- `COMPOSE_PROJECT_NAME` and one `<SERVICE>_HOST_PORT` per
    service in the port registry. A template naming something else is left out
    rather than written half-expanded, because a `.env` line containing a literal
    `${...}` is a value compose would pass through to the app verbatim.

    `ui_services` is what makes a UI-only preview possible: the compose services
    that make up this project's UI tier (usually just `["frontend"]`). Empty --
    the default -- means the project has not declared one and `worktree.py
    preview --ui` refuses, because devkit cannot guess which of a stack's
    services is the frontend. `ui_env` is `env`'s sibling for that mode only:
    the same template map, applied on top of `env`, for values that are only
    right when the backend is the *static checkout's* stack rather than the
    box's own (a dev-proxy target, an API base URL). In a UI-only box the
    managed env the templates expand against mixes two slots -- the box's own
    ports for `ui_services`, the source checkout's for everything else -- so a
    template like `http://localhost:${APP_HOST_PORT}` lands on the backend that
    is actually running.
    """

    env: dict[str, str] = field(default_factory=dict)
    ui_services: tuple[str, ...] = ()
    ui_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    """Shape of the project the harness scripts operate on."""

    # Prefix for harness control env vars, e.g. "CARAMELI" ->
    # CARAMELI_SKIP_STOP_VERIFY / CARAMELI_STOP_TESTS_AUTOSTART / ...
    env_prefix: str = "DEVKIT"
    app_dir: str = "app/"
    tests_dir: str = "tests/"
    unit_tests: str = "tests/unit"
    db: DbConfig = field(default_factory=DbConfig)
    frontend: FrontendConfig = field(default_factory=FrontendConfig)
    python: PythonConfig = field(default_factory=PythonConfig)
    bash: BashConfig = field(default_factory=BashConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    test_contract: TestContractConfig = field(default_factory=TestContractConfig)
    structure: StructureConfig = field(default_factory=StructureConfig)

    def env(self, suffix: str) -> str:
        """The prefixed control-env name, e.g. env("SKIP_STOP_VERIFY")."""
        return f"{self.env_prefix}_{suffix}"


def _as_str_tuple(value: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    return fallback


def _db_from(raw: dict[str, Any], default: DbConfig) -> DbConfig:
    test_env = raw.get("test_env")
    return replace(
        default,
        enabled=bool(raw.get("enabled", default.enabled)),
        services=_as_str_tuple(raw.get("services"), default.services),
        db_service=str(raw.get("db_service", default.db_service)),
        db_port=int(raw.get("db_port", default.db_port)),
        redis_service=str(raw.get("redis_service", default.redis_service)),
        redis_port=int(raw.get("redis_port", default.redis_port)),
        user=str(raw.get("user", default.user)),
        password=str(raw.get("password", default.password)),
        name=str(raw.get("name", default.name)),
        url_scheme=str(raw.get("url_scheme", default.url_scheme)),
        url_env=_as_str_tuple(raw.get("url_env"), default.url_env),
        redis_env=str(raw.get("redis_env", default.redis_env)),
        test_env=(
            {str(k): str(v) for k, v in test_env.items()}
            if isinstance(test_env, dict)
            else dict(default.test_env)
        ),
    )


def _frontend_from(raw: dict[str, Any], default: FrontendConfig) -> FrontendConfig:
    return replace(
        default,
        enabled=bool(raw.get("enabled", default.enabled)),
        dir=str(raw.get("dir", default.dir)),
        src=str(raw.get("src", default.src)),
        skin=str(raw.get("skin", default.skin)),
        test_cmd=_as_str_tuple(raw.get("test_cmd"), default.test_cmd),
        typecheck_cmd=_as_str_tuple(raw.get("typecheck_cmd"), default.typecheck_cmd),
    )


def _python_from(raw: dict[str, Any], default: PythonConfig) -> PythonConfig:
    return replace(
        default,
        install_command=str(raw.get("install_command", default.install_command)),
        version=str(raw.get("version", default.version)),
    )


def _int_or(value: Any, fallback: int) -> int:
    """int(value) when it is a real number, else `fallback`. Never raises.

    `bool` is excluded deliberately: it is an `int` subclass, so `max_bytes = true`
    would otherwise silently become a 1-byte cap.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _bash_from(raw: dict[str, Any], default: BashConfig) -> BashConfig:
    return replace(
        default,
        max_bytes=_int_or(raw.get("max_bytes"), default.max_bytes),
        head_bytes=_int_or(raw.get("head_bytes"), default.head_bytes),
    )


def _docker_from(raw: dict[str, Any], default: DockerConfig) -> DockerConfig:
    # `is True` rather than `bool(...)`: this key licenses stopping a stack, so a
    # typo ("yes", 1) must read as the safe default, not as truthy.
    return replace(default, auto_stop=raw.get("auto_stop", default.auto_stop) is True)


def _test_contract_from(raw: dict[str, Any], default: TestContractConfig) -> TestContractConfig:
    return replace(
        default,
        sources=_as_str_tuple(raw.get("sources"), default.sources),
        exclude=_as_str_tuple(raw.get("exclude"), default.exclude),
    )


def _int_map(value: Any) -> dict[str, int]:
    """`{name: int}` from a TOML table, dropping entries that are not whole numbers.

    A dropped limit falls back to the script's default rather than to "no limit",
    and `bool` is excluded for the reason `_int_or` gives.
    """
    if not isinstance(value, dict):
        return {}
    return {
        str(k): int(v) for k, v in value.items() if isinstance(v, int) and not isinstance(v, bool)
    }


def _layer_rules(value: Any) -> tuple[LayerRule, ...]:
    if not isinstance(value, list):
        return ()
    rules = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        rules.append(
            LayerRule(
                name=str(raw.get("name", "")),
                # `from` in TOML, because that is how the rule reads; a Python
                # field cannot carry that name.
                sources=_as_str_tuple(raw.get("from"), ()),
                forbid=_as_str_tuple(raw.get("forbid"), ()),
            )
        )
    return tuple(rules)


def _restrict_rules(value: Any) -> tuple[RestrictRule, ...]:
    if not isinstance(value, list):
        return ()
    rules = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        rules.append(
            RestrictRule(
                name=str(raw.get("name", "")),
                pattern=str(raw.get("pattern", "")),
                only_in=_as_str_tuple(raw.get("only_in"), ()),
                paths=_as_str_tuple(raw.get("paths"), ()),
            )
        )
    return tuple(rules)


def _structure_from(raw: dict[str, Any], default: StructureConfig) -> StructureConfig:
    return replace(
        default,
        paths=_as_str_tuple(raw.get("paths"), default.paths),
        exclude=_as_str_tuple(raw.get("exclude"), default.exclude),
        entrypoints=_as_str_tuple(raw.get("entrypoints"), default.entrypoints),
        disabled=_as_str_tuple(raw.get("disabled"), default.disabled),
        limits=_int_map(raw.get("limits")) or dict(default.limits),
        layers=_layer_rules(raw.get("layers")) or default.layers,
        restrict=_restrict_rules(raw.get("restrict")) or default.restrict,
    )


def _env_map(value: Any, fallback: dict[str, str]) -> dict[str, str]:
    # Both halves coerced to str: TOML gives an int for `PORT = 5176`, and a
    # non-string value reaching the `.env` writer would be a template that never
    # matches and a key that renders as `PORT=5176` only by luck of repr.
    if not isinstance(value, dict):
        return dict(fallback)
    return {str(k): str(v) for k, v in value.items()}


def _worktree_from(raw: dict[str, Any], default: WorktreeConfig) -> WorktreeConfig:
    return replace(
        default,
        env=_env_map(raw.get("env"), default.env),
        ui_services=_as_str_tuple(raw.get("ui_services"), default.ui_services),
        ui_env=_env_map(raw.get("ui_env"), default.ui_env),
    )


def from_dict(data: dict[str, Any]) -> Config:
    """Build a Config from an already-parsed manifest dict. Pure; never raises."""
    default = Config()
    project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
    paths = data.get("paths", {}) if isinstance(data.get("paths"), dict) else {}
    db_raw = data.get("db", {}) if isinstance(data.get("db"), dict) else {}
    fe_raw = data.get("frontend", {}) if isinstance(data.get("frontend"), dict) else {}
    py_raw = data.get("python", {}) if isinstance(data.get("python"), dict) else {}
    bash_raw = data.get("bash", {}) if isinstance(data.get("bash"), dict) else {}
    docker_raw = data.get("docker", {}) if isinstance(data.get("docker"), dict) else {}
    wt_raw = data.get("worktree", {}) if isinstance(data.get("worktree"), dict) else {}
    tc_raw = data.get("test_contract", {}) if isinstance(data.get("test_contract"), dict) else {}
    st_raw = data.get("structure", {}) if isinstance(data.get("structure"), dict) else {}
    return Config(
        env_prefix=str(project.get("env_prefix", default.env_prefix)),
        app_dir=str(paths.get("app", default.app_dir)),
        tests_dir=str(paths.get("tests", default.tests_dir)),
        unit_tests=str(paths.get("unit_tests", default.unit_tests)),
        db=_db_from(db_raw, default.db),
        frontend=_frontend_from(fe_raw, default.frontend),
        python=_python_from(py_raw, default.python),
        bash=_bash_from(bash_raw, default.bash),
        docker=_docker_from(docker_raw, default.docker),
        worktree=_worktree_from(wt_raw, default.worktree),
        test_contract=_test_contract_from(tc_raw, default.test_contract),
        structure=_structure_from(st_raw, default.structure),
    )


def load(root: Path) -> Config:
    """Load `<root>/.devkit.toml`, or return defaults when absent/unreadable.

    Any failure -- no file, no `tomllib`, a parse error -- degrades to `Config()`
    so the harness stays a valid (if minimal) lint+script-test gate.
    """
    manifest = root / MANIFEST_NAME
    if not manifest.exists():
        return Config()
    try:
        import tomllib  # stdlib 3.11+; guarded for older shims
    except ModuleNotFoundError:
        return Config()
    with contextlib.suppress(OSError, ValueError), manifest.open("rb") as fh:
        return from_dict(tomllib.load(fh))
    return Config()


def harness_version(root: Path) -> str:
    """The vendored harness's provenance, for a hook to stamp on what it tells an agent.

    Returns the short `DEVKIT_VERSION` SHA in a consuming project, `"source"` in devkit
    itself (which has no such file because it *is* the upstream), and `""` when neither
    can be determined -- callers omit the stamp rather than print a placeholder.

    Why a hook message should carry this at all. A vendored gate is a **copy**, and it
    is routinely months of fixes behind the repo it came from: at the time of writing
    every consumer in this workspace is pinned at the v0.10.2 merge while nine
    subsequent PRs -- two of them fixes to this very gate's false positives -- sit
    upstream. An agent that trips over one of those has no way to tell "devkit is
    wrong" from "this copy of devkit is old", so it reports the block as a defect, a
    human relays it upstream, and it is closed as already-fixed. The version is the one
    fact that distinguishes the two cases, it costs one file read, and the agent cannot
    obtain it any other way without spending a turn.

    Never raises: this is called from hooks, on the path where they are already
    reporting something else.
    """
    with contextlib.suppress(OSError, ValueError):
        stamp = (root / "DEVKIT_VERSION").read_text(encoding="utf-8").strip()
        # The file holds a SHA by contract (see `sync-devkit.stale_pin`). Trim it to
        # the length a human pastes into `git show`, and refuse anything that is not
        # one rather than echoing arbitrary file content into an agent's context.
        first = stamp.split()[0] if stamp.split() else ""
        if first and len(first) >= 7 and all(c in "0123456789abcdef" for c in first.lower()):
            return first[:12]
    if is_devkit_source(root):
        # devkit vendors *out* of itself and so carries no stamp. Saying "source" is
        # more useful than saying nothing: it tells the agent this copy cannot be
        # behind, so a defect here is genuinely a defect and worth reporting.
        return "source"
    return ""


def is_devkit_source(root: Path) -> bool:
    """Whether `root` is devkit itself rather than a project that vendored it.

    Read from `pyproject.toml`'s project name, **not** from the directory name: devkit
    develops itself in ephemeral boxes under `.worktrees/devkit--<slug>/`, so the
    directory is `devkit` in exactly the checkout where nobody is working. The name
    is the fallback for a repo with no parseable `pyproject.toml`.
    """
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError, ImportError):
        import tomllib  # stdlib 3.11+; guarded for older shims

        with (root / "pyproject.toml").open("rb") as fh:
            return bool(tomllib.load(fh)["project"]["name"] == "devkit")
    return root.name == "devkit"


def provenance(root: Path) -> str:
    """The one-line footer a hook appends when it tells an agent something is wrong.

    Deliberately terse. It fires on every block -- 150-odd times a month in this
    workspace -- so it has to be worth its bytes on the calls where nothing is wrong
    with the harness at all. What earns them is the second clause: it names the check
    that settles "already fixed upstream?" without the agent having to know
    `sync-devkit.py` exists.
    """
    version = harness_version(root)
    if not version:
        return ""
    if version == "source":
        return "(devkit harness: this repo is the source, so this behaviour is current.)"
    return (
        f"(devkit harness {version} -- a vendored copy, which may be behind. If this "
        f"looks wrong, check for an upstream fix before reporting it: "
        f"python scripts/sync-devkit.py --check)"
    )


def lookup(cfg: Config, dotted: str) -> str:
    """One config value as a plain string, for shell callers. Never raises.

    An unknown or non-scalar path yields "" rather than an error, so a caller can
    treat "no value" and "no such field" the same way -- which is what a shell
    script wants: `[ -n "$value" ]`.
    """
    node: Any = cfg
    for part in dotted.split("."):
        node = getattr(node, part, None)
        if node is None:
            return ""
    if isinstance(node, bool):
        # Lowercase so the shell caller can write `[ "$v" = "true" ]` rather than
        # matching Python's "True" — this value only ever crosses into shell.
        return "true" if node else "false"
    return "" if isinstance(node, (dict, list, tuple)) else str(node)


# --- The harness kill switch -------------------------------------------------
#
# `DEVKIT_HOOKS_OFF` switches hooks off from the **environment**, so one line in a
# `settings.json` `env` block quietens the harness across every project at once and
# deleting that line restores it. The alternative — stripping hook entries out of each
# project's `.claude/settings.json` — is N files to edit and N files to reconstruct from
# memory later, which is not a switch anyone flips twice. It is read here rather than in
# each hook so the spelling, the "all" aliases and the off-values asymmetry have one
# owner.
#
# **The branch tier is switchable too, and that is a deliberate reversal.**
# `worktree-guard.py` — which routes an agent edit into a box on a task branch — and
# `task_slug.py`, which names it, were exempt from this switch on the reasoning that
# turning them off does not quieten a session, it lands agent work on a checkout's home
# branch with nothing under it. That argument held only while the hooks were the *only*
# thing that could cut the branch. They no longer are: devkit's workspace carries a task
# per verb — cut the box, open an agent in it, ship it, destroy it — so an operator who
# stands the tier down is choosing to drive it by hand rather than losing it. That tier is
# devkit's, not every project's, which is why this comment describes the *reversal* rather
# than naming a script a consumer does not have.
#
# The two share one name, `branch-tier`, rather than one each. They are halves of a
# single mechanism — the slug exists to name the box the guard cuts — and the vendored
# tier can only see one of them anyway: consumers run `worktree-guard-launch.py` and
# have no copy of `task_slug.py` at all.
HOOKS_OFF_ENV = "DEVKIT_HOOKS_OFF"

# Every hook the switch reaches, spelled as it is written in the env var. Listing them
# is what makes `DEVKIT_HOOKS_OFF=stop` a typo-checkable value rather than a guess, and
# what lets the harness be re-enabled one hook at a time — the order it will actually
# come back in, since the Stop gate is the expensive one and `lint-fix` is nearly free.
SWITCHABLE_HOOKS = (
    "session-start",
    "capped-bash",
    "lint-fix",
    "stop",
    "failure-retro",
    "branch-tier",
)

# Anything meaning "every hook in `SWITCHABLE_HOOKS`". A bare `=1` is what an operator
# reaches for first, so it has to mean the obvious thing.
_ALL_HOOKS = frozenset({"1", "all", "*", "true", "yes", "on"})

# Values that read as "off" to a human must not switch the harness off — the same
# asymmetry `git_policy.SKIP_ENV_VAR` documents, for the same reason: someone who writes
# `DEVKIT_HOOKS_OFF=0` means "leave the hooks running", and honouring it as "off" would
# disable the harness for the person trying to turn it back on.
_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})


def hooks_off(name: str, env: Mapping[str, str] | None = None) -> bool:
    """True when `DEVKIT_HOOKS_OFF` names `name`, or names every hook.

    Asked at the top of a hook's `main()`, before anything else runs — deliberately
    *before* `load()`, so a project whose `.devkit.toml` is broken can still be
    quietened. Never raises and never touches the filesystem for the same reason.
    """
    raw = (os.environ if env is None else env).get(HOOKS_OFF_ENV, "") or ""
    if raw.strip().lower() in _OFF_VALUES:
        return False
    tokens = {token.strip().lower() for token in raw.replace(";", ",").split(",")}
    return bool(tokens & _ALL_HOOKS) or name.strip().lower() in tokens


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    # `python3 scripts/hooks/harness_config.py python.install_command` -> stdout.
    # Exists so `.claude/hooks/session-start.sh` can read the manifest without a
    # TOML parser in shell. Always exits 0: a hook must not die over config.
    import sys

    argv = sys.argv[1:]
    if argv[:1] == ["--hook-off"]:
        # `--hook-off session-start` -> exit 0 when that hook is switched off, 1 when it
        # is live. An exit code rather than stdout because the only caller is
        # `session-start.sh`, where `if ... --hook-off session-start; then exit 0; fi` is
        # the whole of what it needs. This arm is the one that may exit non-zero; the
        # lookup arm below still must not, per the note above.
        sys.exit(0 if len(argv) > 1 and hooks_off(argv[1]) else 1)
    print(lookup(load(Path.cwd()), argv[0]) if argv else "")
