# context7-add

An agent skill that makes documentation available through [Context7](https://context7.com)
when it isn't yet: it **indexes** a repo, docs site, `llms.txt`, OpenAPI spec or PDF/DOCX file
if Context7 doesn't have it, and **allows** it in your teamspace policy so your API key and the
Context7 MCP actually serve it.

It is meant for teamspaces that restrict access with
[policies](https://context7.com/docs/howto/policies) (a hand-picked allowlist, or quality
filters with a wild-card list). Without a policy it still handles the indexing half.

Other Context7 skills look docs up. This one is for the step before that, when the lookup fails
with `access_denied` / "not included in your allowed libraries", or the library isn't indexed.

## What it does

- **Check** each input: is it indexed (ready, processing, not found), is it blocked by the
  policy, and which allowlist entry (exact library, org or domain) already admits it.
- **Index** what's missing through the Context7 API: GitHub, GitLab, Bitbucket and other Git
  repos (public or private), websites, `llms.txt`, OpenAPI URLs, PDF/DOCX/OpenAPI uploads.
- **Allow** the narrowest entry that works (`/owner/repo` by default). A dry run shows how
  many libraries the teamspace can reach before and after.
- **Wait** for indexing and verify the library is served.
- **Log** every applied change to `registry.jsonl`, an audit trail of why each entry exists.

Nothing changes without `--apply`; the skill tells the agent to confirm anything broader than
what you asked for (org or domain entries, removals, private content).

## Install

```bash
npx skills add saalse/context7-add
```

or with the Context7 CLI:

```bash
npx ctx7 skills install /saalse/context7-add context7-add
```

or copy `skills/context7-add/` into `~/.claude/skills/` (Claude Code), `~/.agents/skills/`,
or your agent's skills folder.

## Requirements

- Python 3.8+ (standard library only).
- A Context7 API key: `CONTEXT7_API_KEY` in the environment, or the `context7` MCP server
  configured in `~/.claude.json` with a `CONTEXT7_API_KEY` or `Authorization: Bearer` header.
  The key is read, never printed.
- Changing policies or adding private sources needs an **owner or admin** key; private sources
  need a paid plan.

## Use

Ask your agent, for example:

- "Add https://github.com/saalse/context7-add to Context7 and allow it."
- "Context7 says /saalse/context7-add isn't in my allowed libraries, fix it."
- "Make the docs at https://docs.acme.dev available in context7."

Or run the helper yourself:

```bash
C7=skills/context7-add/scripts/c7.py
python3 $C7 whoami
python3 $C7 check https://github.com/saalse/context7-add
python3 $C7 allow /saalse/context7-add            # dry run
python3 $C7 allow /saalse/context7-add --apply
python3 $C7 add https://github.com/saalse/context7-add --apply   # already indexed: reports its id, creates nothing
python3 $C7 wait /saalse/context7-add
```

Results are JSON on stdout; errors go to stderr with a distinct exit code (bad input,
auth, not found, refused by policy, rate limited, unprocessable, timed out, submission
queue full, file too large). See `python3 $C7 --help`.

The skill follows the [Agent Skills specification](https://agentskills.io/specification).

## Notes

- Submitting a public source adds it to Context7's public index. Use `--private` for internal
  repositories.
- Notion and Confluence need connected accounts; add those at
  [context7.com/add-library](https://context7.com/add-library).
- If you use the official Context7 Admin MCP, its `add_library` / `update_library_policies`
  tools make the same calls; this skill adds the check → narrowest entry → add → verify
  workflow and needs only your regular API key.

## Author

Made by [Alex Samson](https://saalse.com).

## License

MIT, see [LICENSE](LICENSE).
