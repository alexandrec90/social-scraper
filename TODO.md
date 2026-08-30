# TODO — Social Scraper

Freshly generated. These are the things the template deliberately left for you,
because guessing them wrong is worse than leaving them blank.

## Setup

- [ ] Fill in `.env` from `.env.example` (it is gitignored; nothing works without it)
- [ ] Confirm `python scripts/sync-devkit.py --list` shows a stamped `DEVKIT_VERSION`
- [ ] Set `DEVKIT_DIR` in CI so the drift check actually gates — a
      `--check` that prints "nothing to do (skipping)" is checking nothing
      (once `DEVKIT_VERSION` is stamped it fails instead of skipping; to check
      drift on a machine with no devkit clone, `pre-commit run devkit-drift`)
- [ ] Replace the placeholder DB password in `.env` (the committed one is a
      local-dev placeholder and is fine to keep local, but never reuse it remotely)
- [ ] Create the **private** object-storage bucket and fill the `ARCHIVE_S3_*` vars
- [ ] Write `.claude/rules/data-lake.md`'s "what must never be sent" list before
      the first byte ships, not after an incident

## First real work

- [ ] Replace the placeholder in `social_scraper/` with something that does the job
- [ ] Delete `tests/test_smoke.py` once real tests exist

## Archive

<!-- Completed items move here. -->
