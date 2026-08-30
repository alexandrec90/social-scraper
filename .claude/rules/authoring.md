---
description: Conventions for authoring .claude rules and skills files
paths:
  - CLAUDE.md
  - "**/CLAUDE.md"
  - .claude/rules/**/*.md
  - .claude/skills/**/SKILL.md
---

# Rule: Rules & Skills Authoring

## Source of truth

`CLAUDE.md` and `.claude/rules/` are the project-instruction source of truth. Codex reads
the same `CLAUDE.md` files through its configured project-document fallback, so do not
create a second instruction tree.

Repository skills are the one compatibility exception: author them under
`.claude/skills/`, then run `scripts/sync-codex-context.py` to refresh the generated
`.agents/skills/` copy that Codex discovers. Never hand-edit that generated skill copy.

## Instruction files ship with a test, like code

An instruction file is not documentation — it is input that changes what the agent
does, so it carries the same obligation as the code in the same change. **Treat a new
or substantially changed instruction file the way you treat new code: it ships with a
test in the same change.** This is the instruction-file half of the coverage mandate
in `.claude/rules/engineering.md`; the two are one policy, not two.

The harness that measures it is **project-provided** — devkit does not vendor one, and
its shape varies (carameli runs a promptfoo suite under `evals/` with a
with-instructions vs leave-one-out-ablation comparison). Whatever the harness, the
same three properties decide whether a test is worth having:

- **It must discriminate.** The task has to measurably fail, or behave worse, when the
  file is ablated. A test that passes either way proves nothing and costs money on
  every run.
- **The baseline must be fair.** Compare against the skill's plain-English equivalent,
  not against an unresolved `/command` — otherwise you are measuring command
  resolution, not guidance. Weight correctness above efficiency.
- **Scope the run explicitly.** Eval runners typically fan a test across every
  configured model arm unless the test narrows them. Put read-only and single-edit
  tasks on the cheapest arm; reserve the capable arm for genuinely multi-step
  reasoning.

**Genuinely untestable headless?** Some skills cannot be evaluated — they read live
editor diagnostics, or they are aggregate dispatchers with no behavior of their own
(`/fix-all`). Document the exclusion and its reason alongside the harness rather than
shipping a flaky test.

## CLAUDE.md files

- **Only record non-obvious configuration** — things that can't be derived by reading
  source files (e.g. proxy routes, port mappings, env var semantics, architectural
  constraints). If Claude can find it in a config file in one read, leave it out.

## Rules (`.claude/rules/`)

Every rule file must include YAML frontmatter so Claude can scope when it applies:

```yaml
---
description: One-line summary of what the rule covers
paths:
  - <source-dir>/models/**/*.py
  - migrations/**/*.py
---
```

- `description` — brief, specific summary (used to decide relevance).
- `paths` — glob patterns for files the rule applies to. Omit only if the rule is
  truly global (rare).
- Keep rules focused on a single domain — don't mix unrelated conventions in one file.

The vendored repository contract validates this frontmatter. Keep `description` a
non-empty scalar; when present, `paths` must be a non-empty YAML list of non-empty
glob strings.

### One rule file per variant

When a domain has interchangeable variants (themes, providers, adapters), give each
its own `.claude/rules/<domain>-<variant>.md` and **scope its paths to that variant's
directory only** — never to the domain's global tree, or every variant's rule loads on
every file in it.

Prefer **spec tables over code blocks** for values: a table of property/value/notes
survives a refactor that would leave a pasted code sample quietly wrong. Keep code
blocks for structure that only structure can convey (a hierarchy tree, a two-line
signature).

### Security / scoping rules

Cross-cutting security rules (e.g. multi-tenant auth) belong in a scoped rule file, not
in `CLAUDE.md`. Scope them tightly:

```yaml
paths:
  - <source-dir>/api/**/*.py
```

Then add a one-line pointer in `CLAUDE.md`'s guardrails cross-reference list, so the
constraint is discoverable from the root file without being restated there.

## Skills (`.claude/skills/`)

- Every `SKILL.md` must have YAML frontmatter with non-empty scalar `name` and
  `description` fields. Keep optional invocation metadata specific to the skill rather
  than imposing one invocation mode on every workflow.
- If the skill generates scripts, those scripts follow the same conventions as
  hand-written ones — see `.claude/rules/engineering.md`, plus whatever tooling rule
  the project adds (notably `-T` on `docker compose exec`, without which the
  subprocess handle can outlive the command and hang the caller).

### Environment dependencies

A skill that depends on the local environment (Docker stack, running services, git
hooks, a browser runner) must say so in a one-line blockquote at the top of the
SKILL.md, e.g.:

```markdown
> Depends on the local Docker stack and its diagnostics being available.
```

Hooks are a Windows-local performance shortcut; they must never be the only path for
any step a skill needs to complete — skills use the Glob/Grep/Read/Write/Edit tools
directly and write state files (e.g. `state.json`) themselves rather than waiting on
a Stop hook.

### Hook output byte caps (token control)

When a hook or command placeholder emits command output that will be injected into
model context, cap output bytes by default to reduce token usage.

- Prefer a shared helper script in `scripts/hooks/` for capping and truncation markers
  instead of ad-hoc per-skill snippets.
- Do not keep only the first chunk when diagnostics matter. Prefer head+tail windows (or
  at minimum tail-on-error) so terminal errors near the end are preserved.
- Preserve exit-code semantics. Truncation wrappers must not mask command failures.
- Keep cap sizes explicit and small by default (for example, 4-8 KB), and raise only when
  diagnostics require a larger window.

### Instruction size and skill references

Keep every `CLAUDE.md`, rule file, and `SKILL.md` under **500 lines**. If skill content
exceeds this, apply progressive disclosure:

1. Extract reference material into a Markdown file beside `SKILL.md` (for example,
   `writing-conventions.md`).
2. Link references with normal Markdown links so the contract can resolve them.
3. Keep references **one level deep**: `SKILL.md` may link to a sibling reference, but
   a reference must not link to another local Markdown file.
4. Add a `## Table of contents` section containing anchor links to every reference file
   longer than 100 lines.
5. Use forward slashes in frontmatter, Markdown link destinations, and inline-code file
   paths; never use backslashes as path separators.
