---
name: qmd-git-history
description: >-
  Makes a git repository commit history searchable with qmd, the local markdown search engine, by
  exporting one markdown file per commit into a hidden .qmd-history directory and indexing it as a
  repo-history collection that qmd update keeps current. Use when someone asks to index, search, or
  grep git history, commit messages, or old diffs with qmd, asks why or when something changed or
  which commit or PR introduced or removed a behavior, wants agents to find the reasoning behind a
  rule, or notices qmd answers nothing about history because it only indexes files on disk. Covers
  checking for an existing history collection, the exporter, registering it with an update command,
  lexical versus semantic queries over commits, and the traps: hidden-path skipping, one failing
  update command aborting every collection, SIGPIPE under pipefail, and partial files.
license: MIT
metadata:
  family: "coding"
---

# qmd over git history

qmd indexes files that match a glob, never git objects, so commit history is invisible to it until
it exists on disk as markdown. The pattern is three moves:

1. **Export** one markdown file per commit into a hidden `<repo>/.qmd-history/`.
2. **Index** that directory as its own collection, `<repo>-history`.
3. **Refresh** by making the exporter the collection's qmd update command, so every `qmd update`
   re-exports first. It is incremental, so a re-run with nothing new costs under a second.

## First, check whether it already exists

```sh
# any machine with qmd
qmd collection list | grep -- '-history'
qmd collection show <repo>-history     # Path:, and the Update: line that refreshes it
```

If the collection exists, skip to **Searching**. If it has no `Update:` line, its history is
frozen at the last manual export; set one (step 3 below).

## Setting it up for one repo

1. **Get the exporter.** `command -v git-history-to-md.sh` finds it where a machine already
   installs it. Otherwise write the script in `references/exporter.md` (resolve that path against
   this skill's own directory) to a directory on `PATH` and make it executable.
2. **Export, then register the collection:**

   ```sh
   # any machine with git and qmd, macOS or Linux
   REPO=$HOME/src/myrepo NAME=myrepo
   git-history-to-md.sh "$REPO"                        # writes $REPO/.qmd-history
   qmd collection add "$REPO/.qmd-history" --name "$NAME-history"
   ```

3. **Make it self-refreshing:**

   ```sh
   # any machine with qmd
   qmd collection update-cmd "$NAME-history" "$(command -v git-history-to-md.sh) $REPO"
   qmd update        # prints "Running update command" and "exported 0 new commits"
   ```

   qmd runs the command under `bash -c`, from the collection directory, before re-indexing that
   collection. Anything that already runs `qmd update` on a schedule now refreshes history too.
4. **Keyword search works immediately.** Semantic search needs vectors: `qmd embed`, which
   downloads the embedding models the first time.

## Searching

| Question | Command |
|---|---|
| an exact identifier, flag, filename, or error string | `qmd search -c myrepo-history "force-with-lease"` |
| why or when did we ..., in your own words | `qmd query -c myrepo-history "why did we stop rewriting published branches"` |
| read the whole commit | `qmd get qmd://myrepo-history/<file>.md`, then `git show <sha>` for the untruncated diff |

Each document holds frontmatter (`commit`, `author`, `date`), the subject as its `#` heading, the
body, `## Files` (the `--stat`), and `## Diff` (the first 200 lines). Files are named
`YYYY-MM-DD-<sha12>.md`, so hits read chronologically.

- **Lead with `qmd search` for identifiers.** BM25 on an exact token beats the embedder.
- **Diffs dilute why-questions.** A large squash commit touching many topics can outrank the
  focused one. Observed 2026-09-22: asked in paraphrase why agents may no longer rewrite published
  branches, `qmd query` ranked the commit that banned force-pushing third, behind a large
  multi-topic squash, while `qmd search "force push"` ranked it first. If a repo's messages carry the reasoning,
  export with a diff cap of `0` (message and stat only).
- **Omit `-c`** to search history and docs together; `qmd collection exclude <name>-history` takes a
  history out of default queries without removing it.

## What the exporter does, and why

| Behavior | Why |
|---|---|
| exports `origin/HEAD` (falling back to `origin/main`, `origin/master`, `HEAD`), `--no-merges` | on a squash-merge repo that is one document per PR, and unpushed local work stays out |
| writes into hidden `<repo>/.qmd-history` | the repo's own `**/*.md` collection skips it (see the traps), so nothing is indexed twice |
| appends `/.qmd-history/` to `.git/info/exclude` when nothing already ignores it | `git status` stays clean with no commit to the repo |
| skips any commit whose file already exists | incremental re-runs |
| writes `<file>.tmp`, then renames it | an interrupted run never leaves a partial file behind |
| truncates the diff with `sed -n`, never `head` | `head` breaks the whole run; see the traps |
| exits 0 when the repo is missing or the fetch fails | one failure would stop every other collection refreshing |
| fetches with `GIT_TERMINAL_PROMPT=0` and SSH `BatchMode` | a scheduled run has nobody to answer a credential prompt |

## Traps, with evidence

- **One failing update command aborts every collection after it.** qmd 2.1.0 calls
  `process.exit(exitCode)` when a collection's update command exits non-zero (`dist/cli/qmd.js`,
  the block guarded by `yamlCol?.update`, lines 433-456). The rest of that `qmd update` never runs,
  and nothing says which collections were skipped. An update command must never fail for an
  expected reason, such as a deleted clone or a laptop off the network.
- **`head` under `pipefail` truncates the run, not just the diff.** First prototype, 2026-09-22:
  `git show | head -n 200` took SIGPIPE when `head` exited early, `pipefail` plus `errexit` killed
  the loop, and the export stopped after 4 of 435 commits. Only a diff bigger than a pipe buffer
  (64 KB) triggers it, so a small test repo passes. `sed -n '1,200p'` reads to the end.
- **A partial file is forever.** Same incident: the killed iteration left a half-written file, and
  the file-exists check skipped it on every later run. Write to a temp name and rename.
- **`a || b | while` binds as `a || (b | while)`.** The same prototype's ref fallback printed every
  SHA to the terminal and exported nothing. Choose the ref first, then pipe.
- **qmd skips hidden paths, so a hidden export must be the collection root.** `reindexCollection`
  (`dist/store.js`, lines 863-882) globs with `dot: false` and then drops any path with a
  dot-prefixed part, relative to the collection root. That is why the repo's own collection never
  double-indexes `.qmd-history`, and also why pointing a collection at the repo never indexes it.
- **A markdown probe must skip hidden paths too.** Any script that decides whether a directory has
  docs by finding `*.md` will count `.qmd-history` unless it prunes dot directories, and then
  registers a collection that qmd indexes as empty.
- **Do not manage a global gitignore that an app also writes.** Claude Code appends its own entry
  to `~/.config/git/ignore`; owning that file from a dotfiles manager fights it on every apply. The
  per-repo `info/exclude` needs no global file.
- **Leave upstream forks out.** A fork carries its upstream's entire history, often tens of
  thousands of commits, which swamps your own in every result.

## Cost

As of 2026-09-22 on an Apple-silicon Mac with qmd 2.1.0: one 435-commit repo exported in 16 s
(3.6 MB) and embedded as 1,771 chunks in 2 min 16 s; six repos, 2,668 commits in total, exported and
keyword-indexed in 2 min 11 s. Re-derive with `time git-history-to-md.sh "$REPO"` and `qmd status`.

## Verify it worked

Every count below must match; a mismatch means the export or the index stopped early.

```sh
# any machine with git and qmd
git -C "$REPO" rev-list --no-merges --count origin/HEAD    # commits
ls "$REPO/.qmd-history" | wc -l                            # files written
qmd ls "$NAME-history" | grep -c 'qmd://'                  # documents indexed
git -C "$REPO" status --porcelain | grep -c qmd-history    # must be 0
```

## References

Resolve against this skill's own directory.

- `references/exporter.md` — the exporter script, its arguments, and scheduling without an existing
  `qmd update` job.
