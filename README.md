# stump.wtf/skills

Harness-agnostic [Agent Skills](https://agentskills.io) for **stump.wtf** products and the
**StumpCloud** fleet — Joe Stump's apps and the self-hosted infrastructure they run on. One
repository, three families:

| Family | For | Example subjects |
|---|---|---|
| `coding` | agents building the apps | Go service layout, the composed CI pipeline, test house style, HTMX UIs, releases |
| `operating` | scheduled sweep agents and humans running the fleet | triage, converge freshness, container wedges, secrets, storage, backups |
| `review` | agents reviewing PRs, closing issues, auditing repos | verify the property not a proxy, land-or-replay a wedged PR, close with evidence |

Every skill loads in every harness (Claude Code, Crush, any agentskills.io runner) and is sized
for a small model: the scheduled sweeps run on a local 27B with a 196k window.

## The one design rule

> **A skill teaches an agent how to derive a fact, never what the fact is.**

No frozen host lists, no service inventories, no endpoint tables, no counts presented as current.
Give the derivation command. A snapshot that helps a reader orient is dated and has the command
that re-derives it beside it. The reasoning, and the rest of the contract, is in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Layout

```
skills/<name>/SKILL.md          the skill — frontmatter plus a body of at most 180 lines
skills/<name>/references/*.md   the detail, loaded on demand, 100–200 lines each
skills/<name>/scripts/*.py      bundled Python, plus its <script>_test.py suite
tools/                          the repo's own linters, run by CI, read by nobody
.claude-plugin/                 plugin + marketplace manifests for Claude Code
```

Each skill owns every file it references. There is no repo-root `references/`, and no skill points
at a sibling with `../` — a harness resolves a relative path against the *working directory*, not
the skill directory. `skills/README.md` lists the families and how to place a new skill.

## Install

**Any spec-compliant harness** — clone and link the *directory that contains the skills*
(never each skill, which loses the permission-free read grant):

```sh
git clone https://github.com/stump-wtf/skills.git ~/src/stump-wtf-skills
mkdir -p ~/.agents
ln -s ~/src/stump-wtf-skills/skills ~/.agents/skills
```

**Claude Code** — install it as a plugin, which keeps it updatable:

```sh
claude plugin marketplace add stump-wtf/skills
claude plugin install stump-wtf@stump-wtf-skills
```

**Crush** — the `~/.agents/skills` link above is discovered automatically, as are
`~/.config/crush/skills` and `~/.claude/skills`. `CRUSH_SKILLS_DIR` overrides all three.

**One repo only** — link `skills/` as `.agents/skills` inside that repo instead, and the skills
load only for sessions working there.

## The frontmatter contract

```yaml
---
name: example-skill                # == the directory name, ^[a-zA-Z0-9]+(-[a-zA-Z0-9]+)*$, <= 64
description: >-                    # <= 900 chars, third person, trigger-phrase dense
  Third-person description naming the phrases a human actually says when they
  want this skill, so the model can match on them.
license: MIT                       # optional
compatibility: ...                 # optional, <= 500 chars
metadata:                          # optional, string values ONLY
  family: "operating"
---
```

Exactly one frontmatter block. `description` is capped at 900 rather than the harness limit of
1024 because Crush HTML-escapes it into every session system prompt, where one apostrophe costs six
characters. `metadata` is typed `map[string]string`; an unquoted `2` is an int and takes the skill
down with it. `make lint` enforces all of it; the rules and the incident behind each are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Adding a skill

1. Read [CONTRIBUTING.md](CONTRIBUTING.md), pick the family, and `mkdir skills/<name>`.
2. Verify every factual claim against the real file — not against an ADR, not against another
   skill, not against prose. Cite `file:line`, a PR, a commit, or an incident date.
3. `make check`. It must be clean before the PR.

## Development

```sh
make check     # lint + test, exactly what CI runs
make lint      # frontmatter, bodies, manifests, byte-compile
make test      # pytest over tools/ and skills/*/scripts/
make help      # every target
```

`python3` and `pytest` are the only dependencies. The linters are stdlib-only on purpose so they
behave identically on a laptop and on a bare CI runner.

## CI

Gitea Actions gates merge: `lint`, `test`, and `gitleaks` fan out, `all checks passed` fans in,
and all four are required status checks. The secret scan is composed from the shared `ci` repo
rather than copied, and it is required from the first commit — a live AWS key pair once sat in a
skill doc for seven weeks unscanned.

## Where this lives

The canonical repository, its issues, and its CI are on a private Gitea instance; this GitHub copy
is a read-only mirror kept for discoverability. Issues and pull requests opened on the mirror are
not read.

## License

MIT. See [LICENSE](LICENSE).
