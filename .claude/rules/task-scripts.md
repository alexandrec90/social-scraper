---
description: What a script a VS Code task runs can rely on -- the machine's python, the standard library, no console
paths:
  - scripts/*.py
---

# Rule: A script a VS Code task runs

Vendored from devkit, like `engineering.md`: change it there. Scoped to `scripts/`, so it
costs the always-loaded tier nothing.

A task lives in the workspace file and dispatches through devkit, never in
`.vscode/tasks.json`. What that dispatcher starts is `python <script>` under the
machine's `python`, not this project's `.venv`, with no console. So a script a task runs:

- imports only the standard library until it has handed itself to the `.venv`, as
  `scripts/run-tests.py` does;
- cannot rely on Ctrl+C: a task that serves until stopped has to watch for its parent
  going.

A script written against a shell and an activated `.venv` passes every run by hand and
fails the first time a task runs it, on an import the task's interpreter does not have.
