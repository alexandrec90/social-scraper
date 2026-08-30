"""Unit tests for the shared task-branch helpers (scripts/task_branch.py)."""

import datetime as dt
import subprocess

from conftest import load_module

tb = load_module("scripts/task_branch.py")


def _cp(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    """A CompletedProcess stand-in for injecting fake `git` results."""
    return subprocess.CompletedProcess([], returncode, stdout, "")


class TestDetectDefaultBranch:
    def test_reads_branch_from_origin_head(self):
        def git(*args):
            return _cp(0, "refs/remotes/origin/main\n") if args[0] == "symbolic-ref" else _cp(1)

        assert tb.detect_default_branch(git) == "main"

    def test_master_from_origin_head(self):
        def git(*args):
            return _cp(0, "refs/remotes/origin/master\n") if args[0] == "symbolic-ref" else _cp(1)

        assert tb.detect_default_branch(git) == "master"

    def test_probes_master_when_head_unset_and_no_main(self):
        def git(*args):
            if args[0] == "symbolic-ref":
                return _cp(1)  # origin/HEAD not set
            # rev-parse: only origin/master resolves.
            return _cp(0) if args[-1] == "refs/remotes/origin/master" else _cp(1)

        assert tb.detect_default_branch(git) == "master"

    def test_falls_back_when_nothing_resolves(self):
        assert tb.detect_default_branch(lambda *a: _cp(1)) == "main"
        assert tb.detect_default_branch(lambda *a: _cp(1), fallback="trunk") == "trunk"


class TestParsePrompt:
    def test_extracts_prompt_field(self):
        assert tb.parse_prompt('{"prompt": "add SMS retry"}') == "add SMS retry"

    def test_missing_field_is_empty(self):
        assert tb.parse_prompt('{"other": 1}') == ""

    def test_malformed_json_is_empty(self):
        assert tb.parse_prompt("not json") == ""
        assert tb.parse_prompt("") == ""

    def test_non_string_prompt_is_empty(self):
        assert tb.parse_prompt('{"prompt": 42}') == ""


class TestSlugify:
    def test_basic(self):
        assert tb.slugify("Add SMS retry logic") == "add-sms-retry-logic"

    def test_collapses_punctuation_and_trims(self):
        assert tb.slugify("  Fix: the /webhooks/ bug!! ") == "fix-the-webhooks-bug"

    def test_empty_falls_back(self):
        assert tb.slugify("") == "task"
        assert tb.slugify("!!!") == "task"

    def test_truncates_at_word_boundary(self):
        slug = tb.slugify("word " * 30, max_len=20)
        assert len(slug) <= 20
        assert not slug.endswith("-")


class TestTopic:
    def test_drops_filler_and_keeps_content_words(self):
        assert tb.topic("Can you please add a retry to the SMS sender?") == "add-retry-sms-sender"

    def test_keeps_the_action_verb(self):
        assert tb.topic("I think we should rename the ports module") == "rename-ports-module"

    def test_stops_at_the_first_sentence(self):
        prompt = "Fix the webhook timeout. It also fails on empty payloads sometimes."
        assert tb.topic(prompt) == "fix-webhook-timeout"

    def test_reaches_past_a_one_word_lead_in(self):
        assert (
            tb.topic("Question. Why does the lint hook skip templates?")
            == "lint-hook-skip-templates"
        )

    def test_caps_the_word_count(self):
        assert tb.topic("alpha bravo charlie delta echo foxtrot golf hotel") == (
            "alpha-bravo-charlie-delta-echo-foxtrot"
        )
        assert tb.topic("alpha bravo charlie", max_words=2) == "alpha-bravo"

    def test_all_filler_yields_nothing(self):
        assert tb.topic("can you do this for me please") == ""
        assert tb.topic("") == ""


class TestSlugFromPrompt:
    def test_names_the_branch_after_the_topic_not_the_preamble(self):
        prompt = (
            "The coding agent prompt hook that creates the branch - I think it uses "
            "a generic branch name. Is it possible to do better?"
        )
        assert tb.slug_from_prompt(prompt) == "coding-agent-prompt-hook-creates-branch"

    def test_falls_back_to_raw_text_when_topic_is_empty(self):
        # All-filler prompt: better to slugify the words than to name it "task".
        assert tb.slug_from_prompt("can you do this") == "can-you-do-this"

    def test_falls_back_to_task_when_there_is_nothing_at_all(self):
        assert tb.slug_from_prompt("") == "task"

    def test_respects_the_length_cap(self):
        assert len(tb.slug_from_prompt("supercalifragilistic " * 10)) <= tb.SLUG_MAX_LEN


class TestBranchName:
    def test_includes_prefix_slug_and_date(self):
        assert tb.branch_name("add-sms", set(), today=dt.date(2026, 7, 22)) == "agent/add-sms-0722"

    def test_disambiguates_collision(self):
        existing = {"agent/add-sms-0722"}
        assert (
            tb.branch_name("add-sms", existing, today=dt.date(2026, 7, 22))
            == "agent/add-sms-0722-2"
        )

    def test_disambiguates_multiple_collisions(self):
        existing = {"agent/x-0722", "agent/x-0722-2", "agent/x-0722-3"}
        assert tb.branch_name("x", existing, today=dt.date(2026, 7, 22)) == "agent/x-0722-4"

    def test_an_unattended_job_can_name_its_own_namespace(self):
        """The seam the automation namespace is reached through: one caller passes a
        prefix, and every session keeps the default."""
        cut = tb.branch_name(
            "devkit-upgrade-v0-11-2",
            set(),
            today=dt.date(2026, 8, 23),
            prefix=tb.AUTOMATION_PREFIX,
        )
        assert cut == "agent/auto/devkit-upgrade-v0-11-2-0823"

    def test_the_disambiguator_counts_within_the_namespace_it_was_given(self):
        """A same-day rerun must collide with the automation branch, not with a session's
        branch of the same topic -- the two are different refs and both may exist."""
        existing = {"agent/auto/x-0722", "agent/x-0722"}
        assert (
            tb.branch_name("x", existing, today=dt.date(2026, 7, 22), prefix=tb.AUTOMATION_PREFIX)
            == "agent/auto/x-0722-2"
        )


class TestManagedTaskBranch:
    def test_recognizes_neutral_and_compatible_agent_namespaces(self):
        for branch in ("agent/x", "claude/x", "codex/x"):
            assert tb.is_managed_task_branch(branch)

    def test_does_not_claim_arbitrary_feature_or_home_branches(self):
        for branch in ("feature/x", "main", "carameli-b", ""):
            assert not tb.is_managed_task_branch(branch)

    def test_reports_the_prefix_for_safe_topic_extraction(self):
        assert tb.managed_branch_prefix("codex/fix-shipping-0813") == "codex/"

    def test_an_automation_branch_stays_a_managed_task_branch(self):
        """The whole reason the namespace nests inside `agent/`: `/ship`, the sweep,
        `reap` and the guard all gate on this one predicate, and an unattended job's
        branch has to ship exactly like any other."""
        assert tb.is_managed_task_branch("agent/auto/devkit-upgrade-v0-11-2-0823")

    def test_the_longest_prefix_wins(self):
        """`agent/` also prefixes an automation branch, and every caller uses this answer
        to *strip* the prefix off a topic. A first-match answer leaves `auto/` in the
        topic, and `worktree.box_name` turns that into a directory separator inside a
        box name and a `COMPOSE_PROJECT_NAME` compose rejects."""
        branch = "agent/auto/devkit-upgrade-v0-11-2-0823"
        assert tb.managed_branch_prefix(branch) == tb.AUTOMATION_PREFIX
        assert "/" not in branch[len(tb.managed_branch_prefix(branch)) :]

    def test_automation_is_a_namespace_not_a_word_in_the_slug(self):
        """`auto-merge-label` is a task somebody gave an agent. No substring test can
        tell it from a job's own branch, which is why the marker is a path segment."""
        assert tb.is_automation_branch("agent/auto/devkit-upgrade-v0-11-2-0823")
        assert not tb.is_automation_branch("agent/auto-merge-label-0823")
        assert not tb.is_automation_branch("agent/automate-the-prune-0823")


class TestWorktreeFile:
    def test_returns_the_path_git_reports(self):
        def git(*args):
            assert args == ("rev-parse", "--git-path", "agent-shipped")
            return _cp(0, ".git/worktrees/wt/agent-shipped\n")

        path = tb.worktree_file(git, "agent-shipped")
        assert path is not None
        assert path.as_posix() == ".git/worktrees/wt/agent-shipped"

    def test_none_when_git_fails(self):
        assert tb.worktree_file(lambda *a: _cp(1, ""), "agent-shipped") is None

    def test_none_on_empty_output(self):
        assert tb.worktree_file(lambda *a: _cp(0, "  \n"), "agent-shipped") is None

    def test_none_when_git_cannot_be_spawned(self):
        def boom(*args):
            raise OSError("no git")

        assert tb.worktree_file(boom, "agent-shipped") is None

    def test_the_surviving_marker_is_the_stop_counter(self):
        """`agent-shipped` and `agent-task-intent` are gone with the branch hooks; a
        marker nothing reads is a file every session writes for no one."""
        assert tb.STOP_ROUNDS_MARKER_NAME == "agent-stop-rounds"
        assert not hasattr(tb, "SHIPPED_MARKER_NAME")
        assert not hasattr(tb, "TASK_INTENT_MARKER_NAME")
