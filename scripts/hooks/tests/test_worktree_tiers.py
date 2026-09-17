"""Tests for worktree_tiers.py -- which agent CLI cut a worktree, and whose it is.

Every path is a literal or a `tmp_path`, and the one function that touches the disk
(`git_checkout`) is driven against a `.git` file written here, so the suite passes
identically in devkit and in every consumer this is vendored into. The Codex tier's home
is monkeypatched through `CODEX_HOME` rather than assumed, because a machine that has
moved it must not be matched against a directory it does not use.
"""

from pathlib import Path

from conftest import load_module

wt = load_module("scripts/hooks/worktree_tiers.py")

CLAUDE_ENV: dict[str, str] = {"CODEX_HOME": "C:/home/.codex"}


def _claude(checkout="C:/ws/devkit", name="topic"):
    return Path(f"{checkout}/.claude/worktrees/{name}")


def _codex(name="carameli", digest="2e51", home="C:/home/.codex"):
    return Path(f"{home}/worktrees/{digest}/{name}")


class TestTier:
    def test_a_tier_is_detached_exactly_when_it_names_a_home(self):
        """The one flag every function here branches on. Claude's tier hangs off the
        checkout, so its owner is arithmetic; Codex's hangs off a home, so its owner is a
        file read -- and getting this backwards for a new tier would silently resolve
        every one of its worktrees to the runtime's own directory."""
        claude, codex = wt.TIERS
        assert isinstance(claude, wt.Tier)
        assert not claude.detached
        assert codex.detached

    def test_a_tier_added_by_hand_needs_nothing_but_its_row(self):
        """The point of the list: a third runtime is one `Tier(...)`, and everything
        below reads its shape off the fields rather than off its name."""
        made = wt.Tier(agent="acme", segments=("trees",), depth=1, home_env="ACME_HOME")
        assert made.detached
        assert wt.home_of(made, {"ACME_HOME": "C:/acme"}) == Path("C:/acme")


class TestMatch:
    def test_a_nested_claude_worktree_matches_and_names_its_checkout(self):
        found = wt.match(_claude(), CLAUDE_ENV)
        assert found is not None
        tier, anchor, name = found
        assert (tier.agent, anchor, name) == ("claude", Path("C:/ws/devkit"), "topic")

    def test_a_detached_codex_worktree_matches_and_names_the_runtime_home(self):
        """The anchor for a detached tier is the runtime's home, NOT a checkout.

        Returned raw rather than resolved because `owning_checkout` is the function that
        owes a checkout, and a `match` that quietly returned one for a tier that cannot
        compute it would be the whole bug this module exists to close.
        """
        found = wt.match(_codex(), CLAUDE_ENV)
        assert found is not None
        tier, anchor, name = found
        assert (tier.agent, anchor, name) == ("codex", Path("C:/home/.codex"), "carameli")

    def test_a_plain_checkout_matches_nothing(self):
        assert wt.match(Path("C:/ws/devkit"), CLAUDE_ENV) is None

    def test_the_tier_directory_itself_is_not_a_worktree(self):
        """`.claude/worktrees/` holds worktrees; it is not one, and neither is the digest
        directory Codex groups a repo's worktrees under."""
        assert wt.match(Path("C:/ws/devkit/.claude/worktrees"), CLAUDE_ENV) is None
        assert wt.match(Path("C:/home/.codex/worktrees/2e51"), CLAUDE_ENV) is None

    def test_a_worktree_cut_inside_a_worktree_belongs_to_the_inner_checkout(self):
        """Nested arithmetic reads the nearest tier upward, so a worktree cut from inside
        another one names that one -- which is what git would say too."""
        inner = _claude("C:/ws/devkit/.claude/worktrees/outer", "deeper")
        found = wt.match(inner, CLAUDE_ENV)
        assert found is not None
        assert found[1] == Path("C:/ws/devkit/.claude/worktrees/outer")

    def test_a_codex_shaped_path_under_another_home_is_not_the_codex_tier(self):
        """The shape alone is not enough: a project that keeps `worktrees/<x>/<y>` would
        otherwise be read as somebody's agent worktree and resolved against git."""
        assert wt.match(_codex(home="C:/some/project"), CLAUDE_ENV) is None

    def test_the_codex_home_is_read_from_the_environment(self):
        moved = {"CODEX_HOME": "D:/state/codex"}
        assert wt.tier_of(_codex(home="D:/state/codex"), moved).agent == "codex"
        assert wt.match(_codex(home="C:/home/.codex"), moved) is None

    def test_an_unset_codex_home_falls_back_to_the_documented_default(self):
        home = Path("~/.codex").expanduser()
        assert wt.tier_of(_codex(home=home.as_posix()), {}).agent == "codex"

    def test_matching_is_case_insensitive_on_the_home(self):
        """Windows: `C:/Users` and `c:/users` are one directory, and git prints one of
        them while the environment holds the other."""
        assert wt.tier_of(_codex(home="c:/HOME/.codex"), CLAUDE_ENV).agent == "codex"

    def test_a_shallow_path_is_answered_rather_than_indexed_past_the_end(self):
        assert wt.match(Path("/"), CLAUDE_ENV) is None
        assert wt.match(Path("worktrees"), CLAUDE_ENV) is None


class TestIsWorktree:
    def test_both_tiers_are_worktrees_and_a_checkout_is_not(self):
        assert wt.is_worktree(_claude(), CLAUDE_ENV)
        assert wt.is_worktree(_codex(), CLAUDE_ENV)
        assert not wt.is_worktree(Path("C:/ws/devkit"), CLAUDE_ENV)


class TestSameDir:
    def test_two_spellings_of_one_directory_compare_equal(self):
        """Windows makes `C:/Users` and `c:/users` one directory and git prints forward
        slashes there, so every path comparison in the harness goes through this rather
        than through `==` on two `Path`s -- and through this rather than `resolve()`,
        which would touch the disk and answer nothing for a path that is gone."""
        assert wt.same_dir("C:/ws/DevKit", Path("c:/ws/devkit"))
        assert wt.same_dir("C:/ws/devkit/", "C:/ws/devkit")
        assert not wt.same_dir("C:/ws/devkit", "C:/ws/devkit-old")


class TestNestedCheckout:
    def test_a_nested_worktree_resolves_without_touching_the_disk(self):
        """The path does not exist, and that is the case this half is for: a script
        resolving its own workspace file answers for where it *was*."""
        assert wt.nested_checkout(_claude(), CLAUDE_ENV) == Path("C:/ws/devkit")

    def test_a_detached_worktree_has_no_pure_answer_and_says_so(self):
        assert wt.nested_checkout(_codex(), CLAUDE_ENV) is None

    def test_a_plain_checkout_is_not_one(self):
        assert wt.nested_checkout(Path("C:/ws/devkit"), CLAUDE_ENV) is None


class TestGitCheckout:
    def test_reads_the_checkout_off_a_worktree_git_pointer(self, tmp_path):
        checkout = tmp_path / "carameli"
        tree = tmp_path / "tree"
        tree.mkdir()
        gitdir = checkout / ".git" / "worktrees" / "carameli"
        (tree / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n", encoding="utf-8")
        assert wt.git_checkout(tree) == checkout

    def test_a_real_checkout_has_a_git_directory_not_a_pointer(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert wt.git_checkout(tmp_path) is None

    def test_a_missing_or_unreadable_pointer_is_none_rather_than_a_raise(self, tmp_path):
        assert wt.git_checkout(tmp_path / "gone") is None
        (tmp_path / ".git").write_text("not a gitdir line", encoding="utf-8")
        assert wt.git_checkout(tmp_path) is None


class TestOwningCheckout:
    def test_a_nested_worktree_uses_arithmetic_and_ignores_the_disk(self):
        assert wt.owning_checkout(_claude(), CLAUDE_ENV) == Path("C:/ws/devkit")

    def test_a_detached_worktree_is_resolved_through_its_git_pointer(self, tmp_path):
        """The whole point of the module: `<home>/worktrees/<digest>/<name>` names no
        checkout, so the answer comes from the file git wrote."""
        home = tmp_path / ".codex"
        tree = home / "worktrees" / "2e51" / "carameli"
        tree.mkdir(parents=True)
        checkout = tmp_path / "vs-code" / "carameli"
        gitdir = checkout / ".git" / "worktrees" / "carameli"
        (tree / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n", encoding="utf-8")
        env = {"CODEX_HOME": str(home)}
        assert wt.owning_checkout(tree, env) == checkout

    def test_a_detached_worktree_already_deleted_answers_none(self, tmp_path):
        """No pointer left to read, so there is no answer -- and the caller's response to
        None is to treat the path as its own checkout, which is what it now is."""
        env = {"CODEX_HOME": str(tmp_path / ".codex")}
        assert wt.owning_checkout(_codex(home=(tmp_path / ".codex").as_posix()), env) is None

    def test_a_plain_checkout_is_none(self):
        assert wt.owning_checkout(Path("C:/ws/devkit"), CLAUDE_ENV) is None


class TestLabel:
    def test_the_default_tier_is_unqualified(self):
        assert wt.label(_claude(name="glowing-sparking-swing"), CLAUDE_ENV) == (
            "glowing-sparking-swing"
        )

    def test_every_other_tier_carries_its_agent(self):
        assert wt.label(_codex(name="fix-320"), CLAUDE_ENV) == "codex/fix-320"

    def test_two_tiers_holding_the_same_directory_name_get_different_labels(self):
        """The delete dropdown resolves a ticked row back to a worktree by this string,
        so a collision would remove whichever one the scan happened to list first."""
        assert wt.label(_claude(name="fix-320"), CLAUDE_ENV) != wt.label(
            _codex(name="fix-320"), CLAUDE_ENV
        )

    def test_a_path_in_no_tier_gets_its_own_name(self):
        assert wt.label(Path("C:/ws/devkit"), CLAUDE_ENV) == "devkit"


class TestDefaultRoot:
    def test_names_the_default_tiers_directory_under_a_checkout(self):
        assert wt.default_root("C:/ws/devkit") == Path("C:/ws/devkit/.claude/worktrees")

    def test_the_default_tier_is_nested_so_the_root_is_computable(self):
        """`default_root` would have to invent a runtime's digest for a detached tier.
        The first row of `TIERS` being nested is what makes it total."""
        assert not wt.DEFAULT_TIER.detached


class TestBoxTier:
    """The third tier: `<workspace>/.worktrees/<project>--<topic>`, cut by devkit's own
    `worktree.py`. Its name lives here because six modules had spelled it privately, none
    of which could import the workspace script that owned the tier -- a Stop hook in a
    consumer checkout, the Codex hook generator, two ratchets, a tree walk and a disk
    reclaimer."""

    def _box(self, workspace="C:/ws", name="devkit--topic-0916"):
        return Path(f"{workspace}/{wt.BOXES_DIR_NAME}/{name}")

    def test_the_box_tier_is_not_in_the_list_match_answers_for(self):
        """The whole reason it is a separate constant. A box in `TIERS` would be offered
        `git worktree remove` by the delete menu -- which leaks the port lease and leaves
        the container stack up -- and would resolve its `owning_checkout` to the
        workspace, which is not a checkout at all."""
        assert wt.BOX_TIER not in wt.TIERS
        assert wt.match(self._box(), CLAUDE_ENV) is None
        assert wt.tier_of(self._box(), CLAUDE_ENV) is None
        assert not wt.is_worktree(self._box(), CLAUDE_ENV)
        assert wt.owning_checkout(self._box(), CLAUDE_ENV) is None

    def test_all_tiers_is_the_two_plus_the_box_in_that_order(self):
        """`tests/test_worktree_port.py` in devkit holds the TypeScript mirror equal to
        this tuple, so a tier added to `TIERS` reaches the Vite port derivation without
        anyone editing a second list."""
        assert wt.ALL_TIERS == (*wt.TIERS, wt.BOX_TIER)

    def test_the_box_tier_is_one_directory_immediately_above_the_box(self):
        assert wt.BOX_TIER.segments == (wt.BOXES_DIR_NAME,)
        assert wt.BOX_TIER.depth == 1
        assert not wt.BOX_TIER.detached

    def test_is_box_is_true_for_a_box_and_false_for_everything_else(self):
        assert wt.is_box(self._box())
        assert not wt.is_box(Path("C:/ws/devkit"))
        assert not wt.is_box(_claude())
        assert not wt.is_box(_codex())

    def test_is_box_does_not_call_the_boxes_directory_itself_a_box(self):
        """`.worktrees/` holds boxes; it is not one. The same distinction `match` draws
        for `.claude/worktrees/`."""
        assert not wt.is_box(Path(f"C:/ws/{wt.BOXES_DIR_NAME}"))

    def test_is_box_answers_for_a_path_that_is_already_gone(self):
        """Pure, like `match`: the callers that ask are deciding whether to *install*
        something against a directory, and a reaped box is the case they exist for."""
        assert wt.is_box(Path("C:/nowhere/.worktrees/devkit--reaped-0101"))


class TestMarkerNames:
    def test_marker_names_is_every_tiers_innermost_directory(self):
        """What a tree walk skips. Both spellings, because the box tier sits beside a
        checkout and the nested tier sits inside one -- a walk from a checkout root can
        only ever meet the second, and spelling only the first (which is what
        `instruction-budget.py` did) skips nothing it can reach."""
        assert wt.MARKER_NAMES == frozenset({"worktrees", wt.BOXES_DIR_NAME})

    def test_marker_names_leaves_dot_claude_out(self):
        """It is the Claude tier's OUTER segment and it also holds the rules, skills and
        settings a walk is usually there to find. Skipping it would be skipping the
        point -- `harness_state.SKIP_DIRS` splices this set in and would stop finding
        every `.claude/rules/*.md` in the repo."""
        assert ".claude" not in wt.MARKER_NAMES
