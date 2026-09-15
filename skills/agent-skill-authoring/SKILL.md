---
name: agent-skill-authoring
description: >-
  How to write an Agent Skill that loads in every harness, and how to debug one that does not.
  Use when writing a new skill in stumpcloud/skills, editing an existing SKILL.md, splitting an
  over-long body into references, deciding whether a skill should bundle a script, or when make
  check or make lint reports a frontmatter or body failure. Also use when someone says my skill
  never triggers, the skill loaded the wrong version, reading a reference file asked for
  permission, or the description looks truncated. Covers the frontmatter fields and which harness
  reads each one, the 900-character description cap and why it is not 1024, the 180-line body cap,
  the discovery paths, name collisions, HTML escaping in the system prompt, the portable command
  vocabulary, and why a repo-root references directory is unreachable.
license: MIT
---

# Authoring a skill that loads everywhere

Governs every skill in this repo. The mechanical rules are enforced by `make lint`; this is the
model behind them and the diagnosis path when a skill misbehaves. Repo policy (the design rule,
the verification rule, the cut list for scripts) lives in the repo's `CONTRIBUTING.md` — read that
first if you are adding a skill; read this when you need to know *why* a rule exists or why a
skill is not loading.

## The model, in one paragraph

A skill is two things with two different price tags. The **description** is injected into every
session's system prompt for every skill on disk, whether or not it is ever used — it is the entire
selection surface and it is never free. The **body** is loaded only when the model decides to read
it. Everything below follows from that split: the description is optimized for *matching* and
priced per character; the body is optimized for *acting* and priced only on use; anything needed
in a subset of invocations belongs in `references/`, priced only when that subset happens.

Under Crush, discovery, validation, and prompt injection are `internal/skills/skills.go`. A skill
that fails validation is not an error the user sees — `Discover` logs a warning and drops it
(`skills.go:272-276`). Silence is the failure mode.

## Frontmatter

Exactly one block, at the top of the file, opened and closed by `---`.

| Key | Read by | Rule | What breaks |
|---|---|---|---|
| `name` | both | == directory name, `^[a-zA-Z0-9]+(-[a-zA-Z0-9]+)*$`, ≤64 | any violation drops the skill; `foo-` and `foo--bar` both fail |
| `description` | both | ≤900 house / 1024 hard | over the hard cap the skill vanishes with no diagnostic |
| `license` | both | free text | nothing |
| `compatibility` | Crush | ≤500 | over it, the skill drops |
| `metadata` | Crush | one level, **string values only** | typed `map[string]string`; an unquoted `2` is an int and takes the skill down |
| `user-invocable` | Crush | leave unset | see below |
| `disable-model-invocation` | Crush | leave unset | excluded from the prompt; the model can never load it |
| `model`, `allowed-tools` | Claude Code | do not rely on them | Crush unmarshals non-strictly and ignores both |

Limits are `skills.go:22-27`; `Validate` is `skills.go:119-147`; the struct that decides which keys
exist at all is `skills.go:37-49`.

**Never set `user-invocable: true` on a skill carrying shell commands.** It is what turns the skill
into a slash command (`internal/commands/commands.go:69`), and invoking it calls `FormatInvocation`
(`skills.go:352-365`), which runs the **entire body** through the same HTML escaper — every `>`,
`<`, quote, and apostrophe in every snippet comes back mangled.

## The description

Third person, naming the phrases a human actually says, plus the symptoms that should pull the
skill in. It is the only text the model sees before deciding.

**Why the cap is 900 and not 1024.** Crush escapes `& < > " '` into the prompt XML
(`skills.go:31`, `promptReplacer`, applied by `ToPromptXML` at `skills.go:326-348`). An apostrophe
costs six characters *there* and one *here*, so a description measuring 1000 can land at 1030 and
be dropped. Today `cgg` measures 982 with six escapable characters — twelve characters of headroom
against a silent cap. 900 is the margin; `make lint` warns on apostrophes and quotes for the same reason. Rewrite
the sentence rather than spend the budget.

**The budget is shared.** Twenty-one descriptions in the personal marketplace total 10,714
characters — roughly 2,700 tokens in *every* session, before a single skill is used. A description
is not free because it is short; it is a permanent tax. Re-measure the repo with:

```sh
# operator Mac, from the repo root
python3 - <<'PY'
import pathlib, re
t = 0
for f in sorted(pathlib.Path("skills").glob("*/SKILL.md")):
    fm = re.search(r"^---\n(.*?)\n---\n", f.read_text(), re.S).group(1)
    d = re.search(r"^description:.*?(?=\n[a-zA-Z_-]+:|\Z)", fm, re.S | re.M).group(0)
    n = len(" ".join(d.split())) - len("description: >- ")
    t += n
    print(f"{n:5d}  {f.parent.name}")
print(f"{t:5d}  TOTAL (~{t // 4} tokens in every prompt)")
PY
```

**Collisions are resolved by an alphabetical accident.** Two skills with the same `name` in two
installed paths are deduplicated by keeping the **last** occurrence (`skills.go:392-405`) of a list
sorted by lowercase path (`skills.go:291-296`). Live today: `outline-edits` exists in both
`~/.claude/skills` and the personal marketplace, the two files differ (165 vs 188 lines), and Crush
serves whichever path sorts later. Claude Code instead namespaces the plugin copy, so both are
visible and neither is silently lost. Pick names nothing else in the fleet uses.

## The 180-line body

Stays in the body: the decision, the order of operations, the traps with their evidence, the
commands. Leaves for `references/`: lookup tables, schemas, worked examples, per-harness mechanics,
anything a majority of invocations will not read. The cap is arbitrary in the same way a speed
limit is — it forces the split that keeps the loaded cost bounded. For scale, five skills in the
personal marketplace exceed it today, the largest at 367 body lines.

## The portability floor

Four things silently break a skill outside the harness it was written in.

- **Harness-specific tool names.** An `mcp__` prefix, the Task tool, the Skill tool, a subagent, a <!-- lint-skills:allow -->
  hook, a slash command. Name the *capability* instead — read the file, call the forge API, run the
  command — and let each harness supply it. Twelve of twenty-one personal skills fail this today.
- **GNU coreutils on a BSD Mac.** `timeout`, `gtimeout`, `gdate`, and `gstat` are absent here;
  `date -d`, `stat -c`, and `head -n -1` fail. The fleet's Linux hosts have all of them, which is
  exactly why **every command must say where it runs**. Mark the block, not the line.
- **Credentials that are not in the environment.** The shell is non-interactive: `VAULT_ADDR`,
  `VAULT_TOKEN`, `GITEA_TOKEN`, `GH_TOKEN`, and the AWS variables are all empty. `gh` reads the
  keyring, `tea` its config file, and `vault` reads `~/.vault-token` only after you
  `export VAULT_ADDR=https://vault.stump.rocks` yourself. SSH lands as `joestump`, not root.
- **Non-stdlib imports.** PyYAML is not stdlib (`python3 -S -c 'import yaml'` fails). Declare it, or
  do not parse YAML.

## Where skills come from

Crush walks, in order: every path in `options.skills_paths`, then `~/.config/crush/skills`,
`~/.config/agents/skills`, `~/.agents/skills`, `~/.claude/skills`, and then the `.agents`,
`.crush`, `.claude`, and `.cursor` `skills` subdirectories under both the working directory and the
git worktree root (`internal/config/load.go:612-620, 1348-1408`). Configured paths **augment** the
defaults;
`CRUSH_SKILLS_DIR` **replaces** the global four. Claude Code reads `~/.claude/skills`, a repo's
`.claude/skills`, and installed plugins, which it namespaces `<plugin>:<skill>`.

**Install the skills *directory*, never each skill.** Symlinking `skills/*` into a skills path
leaves every file resolving outside that path, so reads lose the prompt-free grant and get truncated
at 200 lines while the skill itself still loads normally. Link or configure `skills/` itself.
Mechanism and the empirical check are in `references/supporting-files.md`.

## Where a skill's files live

`skills/<name>/{SKILL.md,references/,scripts/}`. Each skill owns everything it cites.

**Cite supporting files by their path relative to the skill directory, and say so.** A harness joins
a relative path against the **working directory**, not the skill directory
(`internal/agent/tools/view.go:113`, `SmartJoin`). So a bare `references/foo.md` resolves to
`<cwd>/references/foo.md`, which usually does not exist and comes back as `File not found` — often
with unrelated near-name suggestions. Every harness supplies the skill's own absolute path in the
prompt (`<location>`, `skills.go:341`); resolve against that.

**A repo-root `references/` is unreachable**, and so is a sibling skill's. The read that skips the
permission prompt is a symlink-resolved prefix test against the *configured skills paths only*
(`view.go:402-441`, `isInSkillsPath`). `<repo>/references/` sits outside `<repo>/skills` and gets no
grant, so it prompts, and a denial stops the turn.

**Never create a second `SKILL.md` below your skill directory** — not as a template, not as an
example. Discovery walks for the filename at any depth and `skill.Path` is only the immediate
parent (`skills.go:249-262`), so `skills/x/templates/demo/SKILL.md` validates and becomes a live
skill in every session's prompt. Worked examples go in fenced blocks, never as real files.

## When a script earns its place

Ship one only when the obvious alternative fails **silently** — returns a plausible wrong answer
instead of an error — and you can name the incident. Tedium is never a reason, and the repo's
`CONTRIBUTING.md` lists what was already cut so nobody re-proposes it. If it ships: read-only, a
pure function over already-fetched data at its tested surface, `<script>_test.py` beside it, sample
builders in a globally unique `<prefix>_test_helpers.py`, no network and no credentials in the
suite, and a test that reaches outside this repo **skips** rather than fails.

## Ship it, then verify it actually shipped

```sh
# operator Mac, from the repo root
make check
```

Read the output as a list of decisions, not nits: every failure names a harness behavior, and the
two warnings (an apostrophe in a description, `user-invocable: true`) are judgment calls you may
lose. A pushed skill is not an installed skill — install caching has twice made a correct commit
invisible for months, so confirm the skill appears in the harness before believing it landed.
Both propagation failures and the fix are in `references/harness-loading.md`.

## References

Resolve both against this skill's own directory.

- `references/harness-loading.md` — discovery paths per harness, validation, dedup, escaping, the
  read grant, install propagation, and a symptom-to-cause table.
- `references/supporting-files.md` — layout, path resolution mechanics, the symlink install trap,
  the scripts and pytest conventions, and one worked example.
