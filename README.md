# Social Scraper

Browser-driven social ingestion (Reddit, X) into data-lake

Generated from [devkit](https://github.com/alexandrec90/devkit)'s project
template. The agent harness in `scripts/hooks/` is vendored from there — see
`CLAUDE.md`, "Vendored agent harness".

## Quick start

```bash
cp .env.example .env          # then fill in the placeholders
uv sync --all-extras          # creates .venv from the committed uv.lock
# no uv? pip install -e ".[dev]" works, but resolves fresh instead of from the lock
docker compose up -d
pytest
```

In VS Code, `Ctrl+Shift+B` runs the default build task and the task quick-pick
(`Ctrl+Shift+P` → "Run Task") lists everything else, each with a one-line `detail`
explaining what it costs and what it touches.

## Host ports

This checkout is **slot 2** in devkit's `ports.toml`. Every published port
is its conventional base plus the slot:

| Service | Host port |
| --- | --- |
| postgres | 5434 |

Regenerate the `.env` block for any checkout with
`python <devkit>/scripts/devkit_ports.py <checkout-name>`.


## Layout

```text
social_scraper/                  application code
tests/                tests
scripts/                 project scripts (Python, each with tests)
scripts/hooks/           vendored agent harness — edit upstream in devkit
.devkit.toml      the per-project harness seam (NOT vendored)
docker-compose.yml       the local stack
social_scraper/archive/          data-lake writer + DuckDB read lens
```

## CI

`.github/workflows/pr-gate.yml` runs lint, tests, and the harness drift check on
every PR. The drift check is only meaningful when it can see devkit — if it prints
"nothing to do (skipping)", the gate is inert and the wiring needs fixing. Locally,
where `DEVKIT_DIR` is usually unset, `pre-commit run devkit-drift --all-files` is the
same check against the devkit rev pinned in `.pre-commit-config.yaml`.

`.github/dependabot.yml` opens weekly dependency PRs, and
`.github/workflows/dependabot-automerge.yml` merges them once the gate passes —
patch/minor bumps of anything, plus majors confined to dev tooling. A major that
touches a runtime dependency is labelled `needs-manual-merge` and waits for you.

That auto-merge workflow is **vendored** from devkit, so `sync-devkit.py --check`
compares it byte-for-byte and editing it here shows up as drift. Keep the gate's title
`PR Gate`: the auto-merge workflow waits on it by name, and a rename makes the merge job
inert without turning anything red.

`.github/actions/setup-python-env/` is **yours**, by contrast. devkit shipped it through
that same channel for one release and pulled it back out when two projects turned out to
install in ways `uv sync` cannot express, so it now arrives from `templates/` as a
one-shot copy: nothing compares it, and rewriting its steps to match how this project
installs is the supported move rather than drift. What owning it costs is the other
direction — later devkit fixes to that file never reach here.
