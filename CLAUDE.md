# Social Scraper

Browser-driven social ingestion (Reddit, X) into data-lake

## Tech Stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12 |
| Database | PostgreSQL |
| Data lake (bronze) | Hive-partitioned Parquet on S3-compatible storage, read via DuckDB |
| Container | Docker + Docker Compose |
| Tests | pytest |
| Lint | ruff |

## Environment Variables

See `.env.example` for every variable. `.env` is gitignored and holds this
checkout's ports and credentials.

## Tooling

> Everything in this section needs the local Docker Desktop daemon. If it isn't
> running, make the code change and defer container/stack verification until it is
> (or to CI). Run `docker ps` first — an `npipe`/daemon error means Desktop is stopped.

### Scripts and the vendored harness

Both are covered by **`.claude/rules/engineering.md`** — script conventions (pure
importable functions, stdlib-only hooks, tests in the same change), the failure-artifact
rule, and how the `.devkit.toml` seam works. That file is vendored from
[devkit](https://github.com/alexandrec90/devkit) and drift-gated, so it is the
authority; this file does not repeat it.

### VS Code tasks

- Use `"type": "process"` so VS Code monitors the process directly — that is what
  makes the spinner stop and the exit-code icon appear reliably.
- Set `"close": false` in `presentation` so the terminal stays open for review.
- **Wrap with `notify-wrap.py`** for the completion toast; never call `notify.py`
  from inside a script. Notifications are a task-layer concern only.
- Label convention: `"Domain: Title Case Action"`, and **every task carries a
  `detail`** — that is the second line in the quick-pick, and the only place a
  one-click action can state its cost or blast radius.

### Failure artifacts (fix from a file, not from the terminal)

Any task or script whose failures an agent is expected to act on must persist the
failure to a **parseable artifact file** under `logs/`. Never rely on streamed
terminal output — it scrolls away and buries the signal. Keep the terminal to a
status line plus the artifact path; put everything needed to diagnose in the file.
Write the artifact on failure too, not just success, and overwrite per run.

### Docker subprocess calls

- **`docker compose exec` must use `-T`** — without it a pseudo-TTY is allocated and
  the subprocess handle can outlive the command, leaving the caller hung.


## Testing

**`.claude/rules/engineering.md`** is the authority: tests ship in the same commit,
every testable unit of logic is covered, regression test first, targeted runs locally
and full runs in CI.

Add this project's specifics *below* — fixtures, isolation rules, markers, what to mock
and where — but do not restate the policy above. It is vendored and drift-gated; a copy
here is a fork that will disagree with it the first time either is edited.

- **The `db` fixture is the backend matrix.** SQLite in memory always; Postgres too
  when `DATABASE_URL` is a `postgresql` URL (CI's service container), each test in a
  throwaway schema. Postgres is where dialect bugs surface — psycopg reports
  `rowcount = -1` on `ON CONFLICT DO NOTHING`, so "was it inserted" is `RETURNING`.
  A test with no DB-specific behaviour takes the plain SQLite `engine` fixture.
- **No real browser in tests.** Scrapers take a `fetch.Browser`; `conftest.FakeBrowser`
  serves canned `PageResult`s by URL (exact, then longest prefix). `PlaywrightBrowser`
  is tested against a fake page object in `test_browser.py`.
- **Payload builders in `conftest.py` mirror shapes captured live** (Reddit listings;
  X classic `legacy` and 2026 `details`/`counts` tweets) with invented content. Never
  commit captured payloads: they carry real usernames.
- **Settings in tests are `conftest.IsolatedSettings`**, which never reads `.env`.
- **Live checks** (a real Chrome against Reddit and X) are manual, not tests; the facts
  they established are cited in docstrings with their date. Re-verify when a platform
  changes layout: `scrape` writes what it saw to `logs/scrape-run.json`.

## Guardrails

Baseline guardrails — including the instruction-file feedback loop (**never silently
work around a bad instruction**) — are in `.claude/rules/engineering.md`. Rules for
writing skills and rules themselves are in `.claude/rules/authoring.md`. Cross-reference
this project's own scoped rules here, one line each.

### Data lake

See `.claude/rules/data-lake.md`. The two rules that must never be relaxed: the
bucket is **private**, and the lake is **bronze, not a source of truth** — the hot
path never reads it directly.
