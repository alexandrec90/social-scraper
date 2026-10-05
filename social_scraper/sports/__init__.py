"""The sports domain: social chatter about what Mise-o-jeu+ lets the operator bet on.

The scope is a hand-maintained league list (`sports-leagues.toml`, read by `leagues`);
Mise-o-jeu+ itself is never contacted (sports_betting's `docs/data-sources.md`, "Mise-o-jeu+
is not a collection source"). From the list:

- `wikidata` fetches each league's teams and athletes, with aliases in English and
  French, their subreddits and X handles; `sync` does it weekly per league, and
  `registry` keeps them in `sports_entities`;
- `matcher` finds those entities in a post;
- `ingest` stores posts that are about a league, into the `sports_*` tables;
- `targets` picks which subreddits and X searches a cycle spends its budget on;
- `x` is the X crawl, with a reputation filter on the accounts it follows;
- `discover` turns names that keep appearing into entities, once Wikidata confirms them;
- `cycle` is the domain's part of a scrape cycle, called from `runner`.

No LLM is involved anywhere: every decision is a rule or a Wikidata fact.
"""
