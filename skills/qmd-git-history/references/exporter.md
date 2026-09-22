# The exporter: git-history-to-md.sh

The whole exporter, its arguments, and how to keep it running where nothing already calls
`qmd update` on a schedule. It is plain bash with git as its only dependency, and it stays
bash 3.2-compatible because that is `/bin/bash` on macOS and what `env bash` resolves to under
launchd when Homebrew bash is absent.

## Install

Save the script below as `git-history-to-md.sh` in a directory on `PATH`, then make it executable.

```sh
# operator machine, macOS or Linux
mkdir -p ~/.local/bin
$EDITOR ~/.local/bin/git-history-to-md.sh        # paste the script below
chmod +x ~/.local/bin/git-history-to-md.sh
command -v git-history-to-md.sh                  # must print the path
```

## Arguments

| Position | Default | Meaning |
|---|---|---|
| 1 `repo` | `.` | any path inside the repository |
| 2 `outdir` | `<repo>/.qmd-history` | where the markdown goes; git-ignored locally when it is inside the work tree |
| 3 `max-diff-lines` | `200` | diff lines kept per commit; `0` keeps message and `--stat` only |

The last line of output is `exported <n> new commits from <ref>`, where `<ref>` is the ref it
actually walked. `from HEAD` on a clone that has a remote means `origin/HEAD` was not resolvable,
which is worth a look: the export then includes local, unpushed commits.

## The script

```bash
#!/usr/bin/env bash
# Git History To Markdown
#
# Exports one markdown file per commit (subject as heading, body, --stat, and
# the first N diff lines) into a hidden directory so a qmd collection rooted
# there can index a repo's history. Incremental and atomic; exits 0 when the
# repo is missing or the fetch fails, because qmd aborts the whole `qmd update`
# run when any collection's update command exits non-zero.
#
# Usage: git-history-to-md.sh [repo] [outdir] [max-diff-lines]
set -euo pipefail

repo=${1:-.}
if ! top=$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null); then
  echo "git-history-to-md: not a git repository: $repo (skipping)" >&2
  exit 0
fi
out=${2:-$top/.qmd-history}
max=${3:-200}
mkdir -p "$out"
out=$(cd "$out" && pwd -P)
top=$(cd "$top" && pwd -P)

# Keep the export out of `git status` when it lives inside the work tree.
case "$out" in
  "$top"/?*)
    rel=${out#"$top"/}
    if ! git -C "$top" check-ignore -q "$rel"; then
      excl=$(cd "$top" && git rev-parse --git-path info/exclude)
      case "$excl" in /*) ;; *) excl="$top/$excl" ;; esac
      mkdir -p "$(dirname "$excl")"
      printf '/%s/\n' "$rel" >>"$excl"
    fi
    ;;
esac

# Best effort, and never interactive: a scheduled run has nobody to answer.
GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes" \
  git -C "$top" fetch --quiet 2>/dev/null || true

ref=HEAD
for candidate in origin/HEAD origin/main origin/master; do
  if git -C "$top" rev-parse -q --verify "$candidate^{commit}" >/dev/null; then
    ref=$candidate
    break
  fi
done

n=0
while read -r sha day; do
  f="$out/$day-${sha:0:12}.md"
  [ -e "$f" ] && continue
  {
    git -C "$top" show -s --format='---%ncommit: %H%nauthor: %an%ndate: %aI%n---%n%n# %s%n%n%b%n' "$sha"
    printf '## Files\n\n```\n'
    git -C "$top" show --stat --format= "$sha"
    printf '```\n'
    if [ "$max" -gt 0 ]; then
      printf '\n## Diff\n\n```diff\n'
      # sed reads to the end; head would SIGPIPE git and pipefail would kill the loop
      git -C "$top" show --format= --no-color "$sha" | sed -n "1,${max}p"
      printf '```\n'
    fi
  } >"$f.tmp"
  mv "$f.tmp" "$f"
  n=$((n + 1))
done < <(git -C "$top" log --no-merges --format='%H %as' "$ref")

echo "exported $n new commits from $ref"
```

`%as` (short author date) needs git 2.21 or newer.

## Line by line: the choices that are not obvious

- **`rev-parse --show-toplevel` before anything else.** It resolves any path inside the repo to its
  root, and it is also the not-a-repo check, which must exit 0.
- **`pwd -P` on both paths.** On macOS `/var` is a symlink to `/private/var`; without resolving
  both, the inside-the-work-tree test misses and the export shows up in `git status`.
- **`"$top"/?*`, not `"$top"/*`.** The `?` stops an `outdir` equal to the repo root from matching
  and writing an exclude rule for the whole tree.
- **`check-ignore` before appending.** A repo that already ignores the directory, or a second run,
  adds nothing, so the exclude file never accumulates duplicates.
- **`--git-path info/exclude`.** Correct in a linked worktree too, where the exclude file lives in
  the common git directory rather than under `.git/`.
- **Ref chosen before the pipe.** The first prototype wrote `git log A || git log B | while`, which
  parses as `A || (B | while)`: when `A` succeeded, its SHAs went to the terminal and nothing was
  exported.
- **Process substitution feeds the loop.** `while ... done < <(git log ...)` keeps the loop in the
  current shell, so `n` survives to the summary line; a `git log | while` pipe would run it in a
  subshell.
- **The temp-then-rename write.** Any failure inside the brace group (a full disk, a killed
  process, a failing pipe under `pipefail`) leaves only `<file>.tmp`, which the next run overwrites.
  A direct write would leave a partial `.md` that the file-exists check then skips forever.

## Scheduling without an existing qmd job

Registering the exporter as the collection's update command (see the skill body) means anything
that runs `qmd update` refreshes the history. If nothing does yet, schedule `qmd update` itself.

```sh
# Linux, or macOS with cron enabled: daily at 04:00, with a PATH that finds qmd and git
( crontab -l 2>/dev/null; echo '0 4 * * * PATH=$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin qmd update >/dev/null 2>&1' ) | crontab -
```

A scheduled run has a minimal environment. Give it a `PATH` that includes the directories holding
`qmd`, `git`, and the exporter, or the update command fails and, per the abort trap, takes every
later collection down with it. Embeddings are not refreshed by `qmd update`; add `qmd embed` to the
same job only if the machine can afford the compute.

## Removing a history

```sh
# any machine with qmd
qmd collection remove myrepo-history
rm -rf "$REPO/.qmd-history"
```

The `/.qmd-history/` line in `.git/info/exclude` is harmless to leave. Removing the collection
also deletes its vectors: qmd reports `Cleaned up <n> orphaned content hashes` (seen 2026-09-22 on
a 435-commit history), so adding it back later means paying for `qmd embed` again. To rebuild the
files without losing vectors, keep the collection and delete only the directory's contents; the
re-export is byte-identical for unchanged commits.
