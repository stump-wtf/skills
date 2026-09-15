# Supporting files: layout, addressing, and the scripts contract

Everything in a skill that is not `SKILL.md`. Two halves: how a supporting file is *addressed* so a
model can actually read it, and what a bundled script has to look like.

## Layout

```
skills/<name>/
  SKILL.md              frontmatter + a body of at most 180 lines
  references/*.md       loaded on demand, 100-200 lines each, one subject per file
  scripts/*.py          bundled Python, with <script>_test.py beside it
```

No other shapes. In particular: no `assets/` unless something reads it, no `templates/`, no
`examples/` directory, and nothing at the repo root that a skill points at.

## How a cited path actually resolves

This is the half that gets written wrong, because it looks like it should work.

**A relative path is joined against the working directory, not the skill directory.** In Crush the
view tool does `SmartJoin(workingDir, params.FilePath)` (`internal/agent/tools/view.go:113`). So a
body that says "see `references/foo.md`" sends the model to `<cwd>/references/foo.md` — which, in a
session working anywhere but this repo, does not exist. The response is
`File not found: <cwd>/references/foo.md`, sometimes with near-name suggestions from an unrelated
directory (`view.go:157-184`), which is worse than a clean error because it looks answerable.

**The fix is to resolve against the skill's own location.** Every harness hands the model the
skill's absolute path in the prompt — Crush emits it as `<location>` alongside name and description
(`skills.go:341`). So write the path relative to the skill directory *and say that is what it is*:

> `references/harness-loading.md` — resolve against this skill's own directory.

**A repo-root `references/` is unreachable, and so is a sibling skill's.** The read that skips the
permission prompt is `isInSkillsPath` (`view.go:402-441`): resolve the file with `EvalSymlinks`,
resolve each configured skills path the same way, and accept only if the file is a prefix-descendant
of one of them. `<repo>/references/` sits outside `<repo>/skills`, so it gets no grant. A file
outside the working directory with no grant raises a permission request (`view.go:135-154`), and a
denial returns `User denied permission` and **stops the turn** (`internal/agent/tools/tools.go:83-87`).

Being inside a skills path is not only about prompts. A skill file also gets no line limit
(`view.go:194-201`, limit 1,000,000 instead of 200) and no content-size cap (`view.go:230-234`). A
reference read from outside the grant is truncated at 200 lines as well as prompted for.

## The symlink install trap

`EvalSymlinks` resolves the *file*, so **how the repo is installed decides whether the grant
applies.** Linking each skill individually into a skills directory breaks it:

```sh
# operator Mac — WRONG: the grant fails for every file in every skill
mkdir -p ~/.agents/skills
ln -s ~/src/stumpcloud-skills/skills/* ~/.agents/skills/
```

`~/.agents/skills/<name>/references/x.md` resolves through the per-skill symlink to
`~/src/stumpcloud-skills/skills/<name>/references/x.md`, which is not under `~/.agents/skills`, so
`filepath.Rel` returns a `..` path and the grant is refused. Discovery still works — `fastwalk` runs
with `Follow: true` — so the skill loads normally and only the reads misbehave, which is exactly the
kind of half-working that costs an hour.

Link or configure the **directory that contains the skills** instead:

```sh
# operator Mac — either of these keeps the grant
ln -s ~/src/stumpcloud-skills/skills ~/.agents/skills
# or add ~/src/stumpcloud-skills/skills to options.skills_paths in crush.json
```

Verified empirically against a reimplementation of `isInSkillsPath` on a fixture tree: per-skill
symlink `false`, skills directory configured `true`.

## Never nest a SKILL.md

Discovery matches the filename `SKILL.md` at **any depth** with no ceiling (`skills.go:256`), and
`skill.Path` is only its immediate parent (`skills.go:161`). So
`skills/agent-skill-authoring/templates/skill-template/SKILL.md` with `name: skill-template`
validates cleanly and becomes a live skill in every session's system prompt, forever, costing its
description in every prompt and offering itself for selection.

This is why the worked example below is a fenced block and not a real directory, and why the linter
fails a nested `SKILL.md` outright.

## References

- 100-200 lines. One subject per file. Under 100 and it belonged in the body; over 200 and it is two
  subjects.
- Same rules as the body: harness-agnostic, verified against source, evidence inline, every command
  says where it runs. `make lint` scans every `.md` under a skill, not just `SKILL.md`.
- Name the file after the question it answers, not the section it came from.
- The body must say what each reference is *for*, in one clause, so the model can decide without
  opening it. A bare list of filenames means every reference gets read every time.

## Scripts

The bar is in the repo's `CONTRIBUTING.md`, along with the list of scripts already cut for failing
it. Assume the answer is no. If it is yes:

**Shape.**

- Read-only. No writes, no deploys, no credential handling, no forge mutations.
- stdlib-only where possible; the linters in `tools/` are stdlib-only on purpose so they behave
  identically on the operator Mac and on a bare runner. **PyYAML is not stdlib** — declare it if you
  parse YAML.
- `argparse`, a `--json` mode for machine consumption, and a non-zero exit on findings.
- Block comments in the personal-log format: a stable header, a 2-3 paragraph TL;DR, then dated
  `@joestump-agent MM/DD/YYYY - ...` lines. No ASCII art, no divider rules.

**Tests.**

- `foo.py` gets `foo_test.py` beside it. The repo's `pytest.ini` sets
  `python_files = *_test.py test_*.py` precisely because pytest's own default matches only
  `test_*.py`, and a file named the house way would otherwise be collected silently as nothing.
- Sample-data builders go in `<prefix>_test_helpers.py`, **never** `conftest.py`. Two `conftest`
  modules in one pytest run collide in `sys.modules` and one suite silently imports the other's. The
  convention exists because that collision already happened.
- **Do not add a per-skill `conftest.py`.** The repo-root `conftest.py` globs every
  `skills/*/scripts` onto `sys.path`, so `import foo` works from any invocation directory.
- The tested surface is a pure function over already-fetched data: no network, no credentials, no
  checkout of any other repo.
- A test that reaches outside this repo **skips**, never fails. A bare CI container has neither
  `~/src/ansible` nor a Crush checkout.
- Every test docstring names the mistake it encodes and cites the incident, issue, or run.

**Wiring.** None needed. `SCRIPT_DIRS := $(wildcard skills/*/scripts)` in the `Makefile` picks the
directory up for both `make lint` and `make test` the moment it exists.

## A worked example

Written as fenced blocks on purpose — see the nesting trap above.

`skills/stumpcloud-example/SKILL.md`:

````markdown
---
name: stumpcloud-example
description: >-
  One paragraph in the third person naming the phrases a human actually says when
  they want this, plus the symptoms that should pull it in. Under 900 characters,
  no apostrophes, no quotes.
license: MIT
---

# What this does

Answer first. The decision, the order of operations, the traps with evidence.

## Deriving the current state

```sh
# operator Mac, from a stumpcloud/ansible checkout
ansible-inventory -i dub.yaml --list --yaml
```

## References

Resolve against this skill's own directory.

- `references/thing-mechanics.md` — the lookup table and the two failure modes.
````

`skills/stumpcloud-example/references/thing-mechanics.md` — 100-200 lines, one subject, same rules.

Then, on the operator Mac from the repo root:

```sh
make check
```
