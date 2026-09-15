# Harness loading mechanics

Where a skill comes from, what each harness does to it, and how to tell which stage swallowed one
that never appeared. Every claim cites its source; re-verify before quoting, because Crush is built
from source on every machine and these constants move.

Crush source lives at `~/src/crush` on the operator Mac, and the running binary is built from
`~/.local/share/crush-src`. Check the copy that matches the binary you are debugging.

## Where skills are discovered

**Crush** builds one flat list of directories to walk, in this order (`internal/config/load.go`):

| Order | Path | Source |
|---|---|---|
| 1 | whatever `options.skills_paths` lists in `crush.json` | user config |
| 2 | `~/.config/crush/skills` | `GlobalSkillsDirs()`, `load.go:1348-1374` |
| 3 | `~/.config/agents/skills` | same |
| 4 | `~/.agents/skills` | same, per the Agent Skills spec |
| 5 | `~/.claude/skills` | same |
| 6 | `<cwd>/.agents/skills`, `.crush/skills`, `.claude/skills`, `.cursor/skills` | `ProjectSkillsDir()`, `load.go:1388-1408` |
| 7 | the same four subdirectories at the **git worktree root**, when the cwd is below it | same |

Two things are easy to get backwards:

- **`skills_paths` augments, it does not replace.** `load.go:612-617` appends each global directory
  that is not already listed. Configuring a path adds to the defaults.
- **`CRUSH_SKILLS_DIR` does replace** — set it and `GlobalSkillsDirs()` returns that one entry and
  nothing else (`load.go:1349-1351`). Rows 1, 6, and 7 still apply; rows 2-5 vanish.

**Claude Code** reads `~/.claude/skills`, a repo's `.claude/skills`, and installed plugins. A plugin
is a repo carrying `.claude-plugin/plugin.json` plus a `.claude-plugin/marketplace.json` that names
it; skills inside it appear namespaced as `<plugin>:<skill>`.

That namespacing is the one behavioral difference that matters between the two harnesses, and it
shows up under collisions — see below.

## What happens to each SKILL.md found

`Discover` → `DiscoverWithStates` (`skills.go:224-299`):

1. **Walk.** `fastwalk` with `Follow: true`, so symlinked directories are traversed at any depth.
   The match is `d.Name() == "SKILL.md"` (`skills.go:256`) — **at any depth**, with no ceiling. A
   `SKILL.md` nested four levels down inside another skill is a separate, live skill, because
   `skill.Path` is only its immediate parent directory (`skills.go:161`).
2. **Parse.** `splitFrontmatter` (`skills.go:184-211`) strips a UTF-8 BOM, normalizes CRLF and bare
   CR to LF, then skips leading blank lines to find the first `---`. It is tolerant of all of that
   and intolerant of exactly one thing: an unclosed block, which returns `unclosed frontmatter`.
   Note what it does **not** do — it takes the *first* closing `---` it finds, so a second
   frontmatter block later in the file is body text, and a horizontal rule written as `---` ends
   your frontmatter early.
3. **Unmarshal.** `yaml.Unmarshal` into the `Skill` struct (`skills.go:37-49`), non-strict: unknown
   keys are silently ignored. `metadata` is `map[string]string`, so any non-string value fails the
   whole unmarshal and the skill is dropped.
4. **Validate** (`skills.go:119-147`). Name present, ≤64, matching `namePattern`, and equal to the
   directory name case-insensitively; description present and ≤1024; compatibility ≤500.
5. **Drop on failure.** A parse error logs `Failed to parse skill file`; a validation error logs
   `Skill validation failed` (`skills.go:268-276`). Both `return nil` and continue the walk. The
   user sees nothing unless they are reading debug logs or open the skills dialog.
6. **Sort**, by lowercase path then lowercase name (`skills.go:291-296`).
7. **Deduplicate** by name, keeping the **last** occurrence (`skills.go:392-405`).
8. **Filter** out anything in `options.disabled_skills` (`skills.go:418-441`).
9. **Inject** the survivors as `<available_skills>` XML (`skills.go:326-348`).

## Collisions

Step 6 plus step 7 mean a name collision is resolved by an alphabetical accident of install
location, not by config order, and the losing copy leaves no trace.

Live example, verifiable today on the operator Mac:

```sh
# operator Mac
diff -q ~/.claude/skills/outline-edits/SKILL.md \
        ~/.config/claude-marketplaces/claude-personal/skills/outline-edits/SKILL.md
```

The two differ (165 vs 188 lines). `.claude` sorts before `.config`, so Crush keeps the marketplace
copy and silently discards the other. Claude Code shows both, one bare and one namespaced
`personal:outline-edits` — so the same pair of files is one skill in one harness and two in the
other. Choose a name nothing else in the fleet uses; there is no warning when you do not.

`Deduplicate`'s last-wins rule is deliberate elsewhere: it is how a user skill overrides a builtin
of the same name (`skills.go:389-391`).

## Escaping into the prompt

`promptReplacer` (`skills.go:31`) rewrites `&` `<` `>` `"` `'` as entities. It is applied to:

- `name`, `description`, and `location` in `ToPromptXML` — every skill, every session.
- **the entire body** in `FormatInvocation` (`skills.go:352-365`), which runs only when a skill is
  invoked as a slash command, which happens only when `user-invocable: true`
  (`internal/commands/commands.go:69`).

Consequences worth internalizing:

- An apostrophe in a description occupies 6 characters against the 1024 cap, not 1. A double quote
  also becomes 6, an ampersand 5, and `<` and `>` 4 each. That asymmetry is why the house cap is 900:
  the 124-character margin is there to absorb escaping you cannot see when you measure the file.
- `user-invocable: true` on a command-heavy skill mangles every redirect, comparison, and quoted
  string in its body. Leave it unset.
- `disable-model-invocation: true` excludes the skill from `ToPromptXML` entirely
  (`skills.go:334-336`). Combined with no `user-invocable`, nothing can ever load it.

## Reads that skip the permission prompt

Discovery and reading are separate systems. A skill can be discovered and still not be readable
without a prompt — see `references/supporting-files.md`, which covers `isInSkillsPath` and the
symlink install trap in full. The short version: the grant is a symlink-resolved prefix test
against the configured skills paths, so how the repo is installed decides whether reading a
reference costs a permission prompt.

## Install propagation — a pushed skill is not an installed skill

Two documented failures, both of which made a correct commit invisible for months, both from
`~/src/dotfiles/.chezmoiscripts/run_after_31-install-claude-plugins.sh.tmpl`:

- **`marketplace add` exits 0 for an already-registered marketplace**, so chaining
  `add || update` meant the update never ran and the cached `marketplace.json` froze at the commit
  of the first clone. When that first clone carried a broken entry, the failure was permanent and
  self-perpetuating: install fails, plugin never appears in `plugin list`, the branch retries,
  `add` succeeds, update still never fires. `harness@claude-plugin-harness` stayed uninstallable for
  three months against a marketplace whose `main` had been fixed 28 minutes after the bad clone
  (dotfiles#124). Both commands must run unconditionally.
- **The version-keyed cache survives uninstall.** A plugin is cached at
  `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, and `plugin uninstall` does not
  remove it. Reinstalling at an unchanged version reuses the stale copy, reports success, and the
  newly pushed skill never appears. Bumping the plugin version also fixes it, but authors do not
  reliably bump versions, so the cache directory is purged before reinstall instead.

The operational rule: after pushing a skill, confirm it is present in the harness before believing
it landed. A green CI run says the file is correct, not that anything is running it.

## Symptom to cause

| Symptom | First thing to check |
|---|---|
| Skill never appears, no error anywhere | Validation dropped it. `name` vs directory, name pattern, description length, a non-string `metadata` value. Run `make lint`. |
| Skill appears but the description is truncated or garbled | An unquoted `: ` or ` #` in a plain scalar, or a `---` horizontal rule ending the frontmatter early. Use a folded `>-` block. |
| Skill loads an old version of itself | Install propagation, not the file. Check the plugin cache directory above, and that the marketplace was updated, not just added. |
| Two variants of the same skill, or the wrong one wins | Name collision across two discovery paths. Compare lowercase paths; the later one wins under Crush. |
| Reading a reference asks for permission, or returns `User denied permission` | The file resolved outside every configured skills path. See `references/supporting-files.md`. |
| Reading a reference returns `File not found` with odd suggestions | The relative path was joined against the working directory. Resolve against the skill location instead. |
| Shell snippets in the body come back with `&gt;` and `&apos;` | `user-invocable: true`. Remove it. |
| The model never chooses the skill even though it loads | A description problem, not a loading problem: it does not contain the phrases the user actually said. |
| A directory that is not meant to be a skill shows up as one | A nested `SKILL.md`. Discovery matches the filename at any depth. |
