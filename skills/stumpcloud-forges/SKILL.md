---
name: stumpcloud-forges
description: >-
  Which copy of a repo is real, and how to create and configure one, across the self-hosted Gitea
  at gitea.stump.rocks and GitHub. Use before pushing, branching, or opening a pull request or
  issue in any repo that exists on both forges, when someone asks which copy is canonical, when a
  push or PR seems to vanish, when creating a new repo, when setting topics, push mirrors, branch
  protection, or collaborators, when moving a branch between repos, and before writing anything
  public that references a repo: a README, docs page, release note, install command, or GitHub
  comment. Covers canonical versus mirror versus reverse-gitea topology, the canonical-gitea,
  canonical-github, and downstream-mirror topics, the stump.wtf versus stump-wtf spelling trap,
  checking the remote, repo-factory ownership, and why a private Gitea link or go install path in
  a public artifact passes every test on the LAN.
license: MIT
metadata:
  family: "coding"
---

# Forges: which copy is real, and how a repo is born

Most repos exist **twice**, and only one copy is real. Getting this backwards is the most
expensive mistake available, because the work is not rejected — it is accepted, then silently
overwritten by the next mirror sync.

## The three topologies

| Topology | Canonical (branch, push, PR, issues, gating CI) | Copy (history replaced on every sync, issues and PRs unread) |
|---|---|---|
| **mirror** (the common case) | Gitea `stump.wtf/<repo>`, gated by Gitea Actions | GitHub `stump-wtf/<repo>` via a Gitea **push** mirror that force-overwrites it; GitHub Actions publishes public artifacts only |
| **GitHub-native** | GitHub; there may be no Gitea copy at all | — |
| **reverse-gitea** | GitHub, where releases, `ghcr.io` images, and the gating Actions run | Gitea, via a Gitea-side **pull** mirror |

**Branch, push, PR, and file issues on the canonical copy. Never on the copy.** A push to a mirror
is reverted by the next sync; a PR there targets a branch that gets force-replaced. New shared work
converges on the `stump.wtf` org. **Never guess from the hostname, and never rely on a list of repo
names** — lists rot.

**reverse-gitea** exists because the repo was born on GitHub and consumers depend on its release
machinery there. It syncs Gitea-pulls-from-GitHub on purpose: a push mirror would store credentials
to a private host in a public forge's Actions secrets, while a pull mirror needs no outbound trust.
It is the trap case — its Gitea copy looks like an ordinary clone and `git ls-remote` works. Two
tells, both worth checking before you branch:

- Its tag list is **behind** GitHub's. If the deployed version has no Gitea tag, you are on the copy.
- The deployed image comes from `ghcr.io`, which only GitHub builds.

## Ask the repo: topics are the source of truth

| Topic | Meaning |
|---|---|
| `canonical-gitea` | The Gitea copy is the source of truth. Work there. |
| `canonical-github` | The GitHub copy is the source of truth. Work there. |
| `downstream-mirror` | **This** copy is a mirror. Do not push, PR, or file here; find the twin its `canonical-*` topic names. |

Both copies carry the same `canonical-*` topic, so you get the same answer wherever you land; the
copy additionally carries `downstream-mirror`. A reverse-gitea repo is `canonical-github` on both,
with `downstream-mirror` on Gitea. Topics are per-host metadata and are **not** replicated by a
mirror.

```sh
# operator Mac
tea api --login gitea.stump.rocks repos/<owner>/<repo>/topics
gh api repos/<owner>/<repo>/topics
```

Forge calls go through `tea` (Gitea) and `gh` (GitHub) only — never `curl` with a token, which the
agent shell does not carry, and never a forge MCP. `tea api` exits 0 even on a 404, so read the body.

**Maintain missing topics, once.** When you touch a repo whose `canonical-*` topic is absent, add it
in that session rather than leaving the next agent to re-derive it. Do not restate correct topics,
and do not churn the list run after run. Gitea
`tea api --login gitea.stump.rocks -X PUT repos/<owner>/<repo>/topics/<topic>` adds one without
disturbing the rest; on GitHub use `gh repo edit <owner>/<repo> --add-topic <topic>`, which
merges — the raw `PUT /topics` API replaces the whole list.

- **A control plane may own the field.** Repos vended by the repo factory are declared in
  `<owner>/<owner>-repositories` (`repos.yaml`, applied by OpenTofu). Setting anything there by API
  is drift the factory exists to prevent. Agents hold read-only Gitea repo tokens on purpose, so a
  `401` on a write is the system working — open a PR against the workspace or hand it to the
  operator, never route around it.
- **A wrong topic is not a missing one.** A `canonical-*` topic that contradicts where the releases
  and images actually are is stale metadata, not an instruction. Seen on `spotter`: the Gitea copy
  said `canonical-gitea` (next to topics from an unrelated project) while GitHub held the deployed
  `v0.3.9`, the `ghcr.io` images, and two weeks of newer commits. Trust the artifacts, fix the
  topic, and **name the correction in your summary** — it is a deliberate change, not a silent fix.
- **No topic and you cannot set one?** Fall back to the org: Gitea `stump.wtf`, `stumpcloud`, and
  the operator's personal namespace are canonical; GitHub `stump-wtf` is a mirror. Say you inferred it.

## Check which clone you are standing in

The working directory tells you nothing; a clone made from the mirror looks completely normal.
Before your first push in any repo, read the remote — redacted, because remote URLs can embed a
credential:

```sh
# operator Mac, inside the checkout
git remote -v | sed -E 's#://([^:/@]+):[^@]*@#://\1:***@#'
git remote set-url origin https://gitea.stump.rocks/<owner>/<repo>.git   # only if origin is the copy
```

The same check applies before `gh pr create` or any forge API call taking an owner and repo. Pass
`owner/repo` explicitly rather than trusting `origin`, so a mirror clone cannot steer a PR to the
wrong host.

## The `stump.wtf` / `stump-wtf` trap

GitHub org names cannot contain dots, so one org is spelled two ways: Gitea **`stump.wtf`** (dot,
canonical) and GitHub **`stump-wtf`** (hyphen, mirror). A hyphen where a dot belongs silently
addresses the mirror. Read the separator before acting on an owner string, and never "correct" one
spelling into the other when copying a URL between hosts.

## A public artifact references only public forges

Canonical says where the **work** goes, not what you **reference**, and the two pull in opposite
directions: `gitea.stump.rocks` is a private instance behind auth. A link to it in a public artifact
is a dead end plus a small disclosure of internal infrastructure. Anything read outside the private
Gitea MUST cite the GitHub copy (`github.com/stump-wtf/<repo>`, or the personal GitHub account for a
personal repo) or the project's public docs site. That covers:

- **Attribution footers** and anything posted on **GitHub**: issue and PR bodies, reviews, release notes.
- **Public docs sites, mirror READMEs, blog posts**, anything shared outside the household.
- **Commands a reader will run** — `git clone`, `go install`, install one-liners. The worst case: a
  private host in a command **exits 0 for everyone inside the network**, so it tests clean for us
  and fails only for its audience, with an error that looks like their broken toolchain. A GitHub
  mirror is a byte copy whose `go.mod` still declares the private module path, so
  `go install github.com/<org>/<repo>/cmd/x@main` fails with a module-path conflict. Tell readers to
  clone the mirror and build from the checkout, or ship a public tap or release.
- **Citations inside a public artifact** — a bare `#212`, a SHA link, or a `docs/…` path into a
  private repo is unresolvable for an outsider *and* for any agent they run. Describe the behavior
  or cite the public docs page; keep a SHA as plain provenance text.

Gitea references stay correct, and preferred, on internal surfaces: a Gitea issue or PR, an internal
wiki doc, a commit message in a Gitea-canonical repo. The test is **"can the reader reach this?"**
A repo with no GitHub mirror is named in plain text with a note that the source is private.

**Verify a replacement by content, not status code.** The Docusaurus docs sites are SPAs that
return **200 for every path**, nonsense included; assert the page body contains what you expect.
`*.pages.stump.rocks` resolves to a LAN address on public DNS, so a local 200 proves nothing — check
with `dig @1.1.1.1` before calling one public. And run the command rather than resolving it:
`go list -m` succeeding off a proxy cache is not `go install` working for a stranger.

## Migrating work between repos

Moving a branch fork → canonical or personal → org is where history rots. Treat it as its own task:

- **Never bundle a migration with feature work.** Move first; land features after, as separate PRs.
- **Rewrite or strip issue and PR references** that pointed at the old tracker. A bare `(#191)`
  resolves against the *current* repo and silently links an unrelated issue.
- **Say what was verified**: which refs were compared, what was confirmed present, what was dropped.
- **Pin the old lineage** (an `archive/<name>` ref or tag) before archiving the source.

## Creating a repo

A repo is not finished until all six hold, done in **the same task that creates it** — the gaps
otherwise surface later as a broken deploy or an unreviewable PR:

1. **Right place** — `stump.wtf` by default, `stumpcloud` for infra, the personal namespace only for
   the genuinely personal; GitHub push mirror to `stump-wtf`; an ecosystem-idiomatic name.
2. **The operator's agent account is a write collaborator**, on every repo made for us.
3. **Metadata** — description, search topics, the mandatory canonical-host topics on both hosts,
   website URL, and `feature` / `bug` / `toil` issue labels.
4. **Boilerplate** — README, LICENSE, stack `.gitignore`, a `Makefile` with `test` / `lint` / `check`.
5. **`main` protected**, PR required, every CI job a **required** check, linear history.
6. **CI wired before the first real PR** — the contract is in the `stumpcloud-ci` skill.

The API calls, the order they must run in, and what the repo factory already does for you:
`references/creating-repos.md`, resolved against this skill's own directory.
