---
description: Tests only this workstation can run -- the nightly local end-to-end tier and its scripts/e2e.py contract
paths:
  - "**/test_*.py"
  - "**/*_test.py"
  - "**/*.test.*"
  - "**/*.spec.*"
  - scripts/e2e.py
---

# Rule: The local end-to-end tier

Vendored from devkit, like `engineering.md`: change it there.

A test that needs this workstation is skipped in CI, and a skip is not coverage. That
means a test that needs the live workspace file, a GUI, the Docker stack, a browser or a
credential. Before this tier such tests ran only when a person happened to run the suite
on the default branch.

**Reach them from `scripts/e2e.py`.** devkit's nightly `devkit-local-e2e` job runs that
script in this checkout, fast-forwarded to origin's default-branch tip, once the PC has
been idle. A red run reaches the fix pass as this project's red default branch, and its
fixer is told to run the script again.

| The contract | |
| --- | --- |
| exit code | 0 green, anything else red |
| each failure | printed as pytest's `FAILED <id>` line: the signature the fix pass groups and dedupes by |
| cwd | the checkout root |
| environment | `DEVKIT_E2E=1` under the scheduler; the checkout's own `.venv` interpreter |
| a run that executed nothing | exits non-zero, since a green from zero tests is a skip reported as a pass |

The usual shape is a pytest marker that `addopts` deselects and `scripts/e2e.py`
selects; devkit's is `local`. Under `DEVKIT_E2E` a skip in that tier is a failure: the
checkout is the one the tests were written for, so nothing has a reason left to skip.
A project without the script has no tier, which is a skip by design.

By hand: `python scripts/e2e.py` here. From devkit's checkout,
`python scripts/e2e-pass.py run --no-wait <project>` shows what the night will see.
