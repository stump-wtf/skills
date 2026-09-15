# Contributing

The authoring contract for `stump.wtf/skills`, in one place, so no skill has to restate it.
`make check` enforces the mechanical half. The judgment half is below it.

General git, forge, and secrets policy lives in the agent rules, not here.

---

## 0. Families

Every skill belongs to exactly one family, and the family is the first word of the answer to "who
loads this":

| Family | Loaded by | Teaches |
|---|---|---|
| **coding** | an agent building or changing a stump.wtf product | how the apps are built: layout, pipeline, tests, UI, persistence, release |
| **operating** | a scheduled sweep agent or a human operating the StumpCloud fleet | how to derive fleet state, triage, remediate within caps, hand off |
| **review** | an agent reviewing a PR, closing an issue, or auditing a repo | how to verify, what to refuse, what to fix in place |

State the family in the skill's `metadata` (`family: "operating"`). The same caps apply to all
three; the operating family carries one extra obligation:

**A sweep-facing skill is not usable until the sweep profile lists it.** The scheduled sweeps run
a lean profile that disables every skill except the ones named in `SWEEP_KEEP_SKILLS` in the
sweep's `crushrc` (dotfiles repo, `sweeps/stumpcloud-sweep/crushrc`). A PR adding or renaming an
operating skill the sweeps should load must say so, and a follow-up issue in the dotfiles repo adds
it to that list and re-measures the sweep's starting context. Merging the skill alone changes
nothing a sweep can see.

## 1. The one design rule

**A skill teaches an agent how to derive a fact, never what the fact is.**

The fleet is documented in four places that all disagree with the inventory, and each was true when
written. So: no frozen host tables, no service inventories, no endpoint lists, no counts presented
as current.

Where a snapshot genuinely helps a reader orient, write it as one — dated, and with the derivation
command beside it:

```
As of 2026-08-30, playbooks/services/ holds 142 playbooks.
Re-derive:  ls stumpcloud/ansible/playbooks/services/*.yaml | wc -l
```

A number without a date and a command is a bug waiting to be quoted back at someone.

## 2. Verify against source, never against prose

Every factual claim must be checked against the file it describes.

- **ADRs are not evidence of what exists.** 27 of the 60 in `stumpcloud/ansible/docs/adrs/` are
  `status: proposed` — aspirational. Check the status line before citing one.
- **Published docs lag the code.** `roles/service/defaults/main.yaml` reads 43 `service_config`
  keys; its own header comment claims 37, and both published references are staler still. The
  defaults file is the authority.
- **Near-identical documents are not identical.** `AGENTS.md` and `CLAUDE-OPS.md` are 1,437 and 598
  lines with 2,023 lines of diff between them. Never tell an agent to update one "and its twin".
- **Another skill is not a source.** If two skills need the same fact, each verifies it.

Cite the evidence inline: a `file:line`, a PR number, a commit SHA, or an incident date. A trap
without its evidence is folklore, and the next reader deletes it.

## 3. Frontmatter

Exactly ONE block, at the top of the file.

| Key | Required | Rule | Why |
|---|---|---|---|
| `name` | yes | == the directory name; `^[a-zA-Z0-9]+(-[a-zA-Z0-9]+)*$`; ≤ 64 chars | Crush's `Validate()` rejects any of these, silently |
| `description` | yes | ≤ **900** chars, third person, trigger-phrase dense | see below |
| `license` | no | | |
| `compatibility` | no | ≤ 500 chars | `MaxCompatibilityLength` |
| `metadata` | no | one level, **string values only** | typed `map[string]string`; an unquoted `2` is an int and fails to unmarshal |

**The 900-char cap.** Crush's hard limit is 1024, and over it the skill vanishes with no diagnostic.
It also HTML-escapes the description into every session's system prompt
(`crush/internal/skills/skills.go:31`, `promptReplacer`), so one apostrophe costs six characters
*there*, not here. 900 is the margin. `make lint` warns on apostrophes and double quotes for the
same reason — rewrite the sentence rather than spending the budget.

**Do not set `user-invocable: true`** on a skill carrying shell commands. `FormatInvocation()`
escapes the *entire body*, mangling `>`, `<`, quotes, and apostrophes in every snippet.

**Write the description in the third person, naming the phrases a human actually says.** It is the
only text the model sees before deciding whether to load the skill.

## 4. Body

- **≤ 180 lines.** The body loads whole, every session. Detail goes to `references/`.
- Answer-first. Lead with the conclusion; no preamble, no restating the request.
- Bullets and tables over prose.
- American spelling, Oxford commas, em-dashes sparingly.
- **Harness-agnostic.** No MCP tool names (`mcp__…`), no Task/Skill/Agent tool, no subagents, no
  hooks, no slash-command framing. Name the *capability* — "read the file", "call the forge API" —
  and let each harness supply it.
- No `../` path to a sibling skill or a repo-root directory. A harness joins a relative path against
  the **working directory** (`crush/internal/agent/tools/view.go:113`, `SmartJoin`), so it resolves
  wherever the session happens to be. Each skill owns its files; where a shared fact is unavoidable,
  restate the few lines, or tell the reader to resolve against the skill's own `<location>`, which
  every harness supplies.

## 5. References

`skills/<name>/references/*.md`, 100–200 lines each. One subject per file. Same verification and
harness-agnosticism rules as the body — `make lint` checks them too.

No `references/` at the repo root: only paths under a configured skills path are read without a
permission prompt.

## 6. Scripts — the bar is high

**Ship a script only when the obvious alternative fails *silently*** — returns a plausible wrong
answer instead of an error — **and you can name the incident.** Tedium is never a reason.

Scripts that were cut for failing this bar, so nobody re-proposes them:

| Cut | Why |
|---|---|
| a 660-line inventory indexer | `ansible-inventory` already does it, `!unsafe` tags included |
| a 410-line `vault` identity wrapper | wraps two obvious commands; writing it is what leaked a live token |
| a 550-line backup freshness checker | becomes credentialed SSH fan-out run twice a year |
| a 310-line storage topology tool | derived-from-derived; restates two facts the prose already carries |

If it does ship:

- Live beside the skill in `scripts/`, with `<script>_test.py` next to it and sample-data builders
  in `<prefix>_test_helpers.py` (globally unique module name).
- **Read-only.** No writes, no deploys, no credential handling.
- The tested surface is a pure function over already-fetched data, so the suite runs with no
  network, no credentials, and no `stumpcloud/ansible` checkout.
- Every test docstring names the mistake it encodes and cites the incident, run number, or issue.
- A test that reaches outside this repo **skips**, never fails — a bare CI container has no
  checkout of anything else.
- **PyYAML is not stdlib.** Declare it if you parse YAML.
- Block comments follow the personal-log format: a stable header, a 2–3 paragraph TL;DR, then dated
  `@joestump-agent MM/DD/YYYY - …` lines. No ASCII art, no divider rules.

## 7. Commands in a skill must run where they say they run

- **Every command says WHERE it runs** — on the operator Mac, or over SSH on a named host class.
- **The Mac is BSD, not GNU.** `timeout`, `gtimeout`, `gdate`, and `gstat` are absent; `date -d`,
  `stat -c`, and `head -n -1` all fail. The fleet's Linux hosts have all of them, which is exactly
  why the marker matters: the same line is correct in one place and broken in the other.
- **The shell is non-interactive with no credentials in the environment.** `VAULT_ADDR`,
  `VAULT_TOKEN`, `GITEA_TOKEN`, `GH_TOKEN`, and the AWS variables are all empty. `gh` authenticates
  from the keyring, `tea` from its config file, and `vault` from `~/.vault-token` — but only after
  you `export VAULT_ADDR=https://vault.stump.rocks` yourself.
- **SSH lands as `joestump` on service hosts and as `root` on hypervisors.** Verified 2026-08-30:
  `ssh ie01.stump.rocks id -un` returns `joestump` and `root@ie01` is refused, while `ssh
  lir.stump.rocks` is refused and `root@lir` returns `root`. Prose has stated each half as if it
  were universal, so derive the class from the inventory before writing a command with a prefix.
- **Never print a secret.** `vault token lookup -format=json` returns the live token in `data.id` —
  never run it unfiltered; select `policies`, `display_name`, `ttl` and nothing else.

## 8. What `make lint` enforces

Failures:

- frontmatter parses as a flat mapping, with exactly one block and no duplicate keys
- no unquoted `: ` or ` #` in a plain scalar (YAML mis-parses or truncates it)
- `name` present, == the directory, matching Crush's pattern, ≤ 64 chars
- directory name lowercase-kebab
- `description` present and ≤ 900 chars
- `compatibility` ≤ 500 chars; `metadata` values all strings
- SKILL.md body ≤ 180 lines
- no `mcp__`, "Task tool", "Skill tool", or "subagent" in any `.md` under the skill
- no `../<sibling-skill>` or `../references` path in any `.md` under the skill
- no nested `SKILL.md` below the skill's own directory
- `.claude-plugin/*.json` parse with no duplicate keys, and every marketplace `source` resolves

Warnings (printed, do not fail): an apostrophe or double quote in `description`;
`user-invocable: true`.

**The escape hatch.** A line carrying `lint-skills:allow` is exempt from the content checks. It
exists for the one skill whose subject is these rules and which therefore has to spell out the
constructs it bans. Use it nowhere else.

## 9. Before the PR

```sh
make check
```

Both targets clean. CI runs the same ones, plus a full-history secret scan.
