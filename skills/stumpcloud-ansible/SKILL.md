---
name: stumpcloud-ansible
description: >-
  Working inside stumpcloud/ansible, the Ansible monorepo that converges the
  StumpCloud fleet, without shipping a silent no-op. Load whenever a task
  touches that checkout - editing a playbook, role, or inventory entry, running
  ansible-playbook against dub, dtw, pdx, or gva, asking which host runs a
  service or which hosts are in a group, working out why a merged change never
  reached a host, adding a converge-skip entry, or deciding which of the many
  disagreeing documents to believe. Load it before quoting any host, service, or
  group count from prose, and before running ansible from the operator Mac,
  where the ansible on PATH is neither the one the repo pins nor the one CI
  runs. Covers repo geography, merge-is-deploy and the converge lane, previewing
  deploy targets, deriving fleet facts from the inventories instead of frozen
  tables, and the fail-open traps that make a no-op look green.
license: MIT
---

# Working in stumpcloud/ansible

## Everything here fails open

The characteristic failure is not an error. It is a plausible green.

| You run | The failure |
|---|---|
| `ansible-inventory -i broken.yaml --list` | exit **0**, empty host set, warning only on stderr |
| `ansible-playbook -i broken.yaml play.yaml` | `hosts (0)`, exit 0, "green" |
| a playbook whose `hosts:` matches nothing | `Could not match supplied host pattern`, exit 0 |
| a merge whose diff maps to no deploy target | every converge job **skips**, and a skipped job is green |

Verified 2026-08-30 by running each. `tests/test_inventory_parses.py` exists because a
secret-scrub rewrote six pdx.yaml values to `***REDACTED***` on 2026-08-08; an unquoted
leading `*` opens a YAML alias, the file stopped parsing, PDX resolved to zero hosts, and
the check used to sign off `stumpcloud/stumpcloud#282` reported OK.

**So: never conclude "it deployed" from a green run.** Assert on content — a non-empty host
list, a named target, a converged task count — never on an exit code.

## Find the checkout

Canonical remote is `https://gitea.stump.rocks/stumpcloud/ansible.git`; the usual path is
`~/src/ansible`. Confirm with `git remote -v` before you touch anything — you may be in a
worktree under `.claude/worktrees/`, which has no `.venv` of its own (the `Makefile` borrows
the main checkout's; see `MAIN_ROOT`).

The repo's own `CLAUDE.md` is short, current, and worth reading before your first edit — it
carries the playbook conventions and the Gluetun VPN exception, both of which have their own
CI guards.

## Repo geography

- **Four site inventories at the root**: `dub.yaml`, `dtw.yaml`, `pdx.yaml`, `gva.yaml`.
  `nerdheim.yaml` is also at the root and is **zero bytes** — globbing `*.yaml` as
  inventories picks it up. `services.yaml` and `world.yaml` are aggregate playbooks, not
  inventories.
- **No `group_vars/`, no `host_vars/`.** Verified absent. Every variable is inline under
  `all.vars`, `all.hosts.<host>`, or `all.children.<group>.vars`.
- **Two service-definition surfaces**, and reading only one loses whole sites: a service is
  a dict carrying `image`, and it may sit under a host block **or** under `all.vars`.
  Snapshot 2026-08-30, host-block / `all.vars`: dub 91/11, dtw 8/8, pdx 5/4, **gva 0/7**.
  A host-block-only walk reports `cloud01` as running nothing.
- **One playbook per service** at `playbooks/services/<name>.yaml` (140 files; 76 use
  `roles/service`, 28 drive `docker_compose_v2` directly, 39 are not services at all).
  Base-layer plays are at `playbooks/` depth 1: `base.yaml`, `docker.yaml`, `users.yaml`,
  `mounts.yaml`, `host.yaml`, `binnacle-probe.yaml`.
- `roles/` holds 7 roles; `roles/service` is the generic one. `tests/` is ~99 pytest guard
  modules over the repo itself, separate from the collection tests under `collections/`.

Counts are a dated snapshot. `references/deriving-fleet-facts.md` has the command for each.

## Which document to believe

Ranked. Never quote a host, service, or group count from anything below rank 2.

1. **The inventories and `tests/`** — executable and CI-gated. This is the answer.
2. **`playbooks/` and `roles/`** — what actually runs.
3. **`docs/adrs/` (61) and `docs/openspec/specs/` (37)** — check `status:` first. Today 28
   are `accepted`, 27 `proposed`, 5 `superseded`: nearly half the corpus is aspirational,
   not built. That sums to 60, not 61 — `ADR-0027` declares its status in a `## Status`
   prose section and has no `status:` key, so a `grep '^status:'` sweep silently omits it.
   `ADR-0056` and `ADR-0058` each exist twice under different filenames.
4. **`CLAUDE.md`** (127 lines) — current and worth reading in full.
5. **`AGENTS.md`** and **`CLAUDE-OPS.md`** — *not* copies of each other, so never "update one
   and its twin"; `wc -l` them rather than quoting a remembered length. Both get corrected
   in place (PR #578, 2026-08-30, rewrote 148 lines across the two), so **cite a line you
   have just read, never a claim you remember**. A correction also rarely lands everywhere:
   `AGENTS.md:159` says there are four site inventories and `AGENTS.md:1415`, in the same
   file, still lists three.
6. **`.claude-ops/**`** — stalest. `checks/verify-caddy.md:7` says Caddy runs on "all three
   hosts"; the `caddy` group in `dub.yaml` has four.

Staleness is not confined to prose files. `roles/service/defaults/main.yaml` reads 43
`service_config` keys and its own header comment, six lines above, lists 36.

## Merge is deploy

Push to `main` runs `gate` -> `detect` -> `converge-base` -> `converge-apps` -> `reconcile`
(`.gitea/workflows/ci.yaml`, job comment block at the top). Your change ships **only** if
`.gitea/scripts/detect-deploy-targets.py` maps a changed file to a target.

Preview it before you open the PR, from the repo root on the Mac:

```sh
# [mac] repo root; needs PyYAML and no credential. Without PyYAML it degrades
# to a fleet-wide fan-out, which is why CI hard-fails on its absence.
python3 .gitea/scripts/detect-deploy-targets.py "$(git merge-base origin/main HEAD)" HEAD \
  | python3 -m json.tool
```

- Read **stderr**. Every `note:` line is a change that will not converge.
- `"base": []` **and** `"apps": []` means the merge deploys nothing. That is legitimate for
  docs or tests and indistinguishable from a broken diff base otherwise — the run posts
  `::notice::No deploy targets` and a "Deploy targets" step summary. Read that summary, not
  the overall tick.
- Never reimplement this mapping. Call the script; it owns the name/kebab/Jinja-scan
  fallback (`speedtest` resolves to `speedtest-tracker.yaml`) and `.gitea/converge-skip.txt`.

`.gitea/converge-skip.txt` lists operator-only playbooks, one stem per line, plus scoped
`stem:inventory.yaml` pairs. Its entries are announced on stderr, never silently dropped.

## Running things locally

Full recipe, the three-ansible problem, and the BSD-vs-GNU list: `references/operator-shell.md`.
The two that bite first:

- `ansible.cfg` sets `inventory = ./dub.yaml`, so any command without `-i` silently answers
  about **DUB**. Always pass `-i <site>.yaml`.
- `ansible-playbook` on `PATH` is Homebrew's **2.10.15**. The repo pins 2.21.x in the
  `.venv`; CI runs 2.17.14 in a digest-pinned image. Use `pipenv run` or the venv, and
  expect the CI gap: a `{% raw %}` template rendered locally and died in CI on 2026-08-22.

## Collections over raw URI

**When a community collection exists for a service's API, the role speaks through
the collection — never through hand-rolled `ansible.builtin.uri` calls.** A MUST,
not a preference: check Galaxy (and the vendored `collections/ansible_collections/`)
before writing a raw API call, and say so in the PR if none exists. Why: the raw-API
`roles/technitium` carried 29 `uri` tasks, three of which silently mis-encoded
requests (JSON bodies the form-urlencoded API ignored, so every "changed" was a lie)
before the 2026-09-04 refactor onto `effectivelywild.technitium_dns`. A collection
module gets idempotency, check mode, validation, and upstream fixes for free; a uri
task gets none and drifts quietly.

Mechanics: pin the exact version (no ranges) in **both** `requirements.yml` (docs)
and the `ansible-runner` image's `requirements.yml` (the half CI runs) — image PR
merges and builds first, then the `ci.yaml` digest bumps in the same change as the
refactor. `tests/test_technitium_uses_collection.py` enforces this for technitium;
copy that guard shape for the next role.

`make test` / `make lint` / `make check` wrap `.gitea/scripts/run-tests.sh` and
`run-lint.sh`, which CI invokes directly, so local and CI cannot drift. Run `make check`
before pushing.

## Deriving fleet facts

Do not write a host, service, or group table anywhere. Derive it. Every command, with the
non-empty assertions that stop them failing open, is in `references/deriving-fleet-facts.md`.
The four you need most:

```sh
# [mac] repo root, repo venv on PATH, OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
ansible-inventory -i dub.yaml --list          # hosts + groups for a site
ansible-inventory -i dub.yaml --graph caddy   # who is in a group
ansible-inventory -i gva.yaml --host cloud01  # merged vars, BOTH service surfaces
ansible-playbook -i dub.yaml playbooks/services/jellyfin.yaml --list-hosts
```

The last one is the playbook-to-host join nothing else does. `hosts (0)` with exit 0 means
the play targets nobody — that is the answer, not a tooling failure.

## Traps that have cost outages

- **An inventory host-var edit converges nothing without the base phase.** Host vars
  (`host_zfs_*`, `host_swap`, ...) are applied only by `playbooks/host.yaml`.
  `stumpcloud/stumpcloud#483` raised `tank/media` to 22T; ie01 sat at 21T with 0 bytes free
  for days because the merge emitted 65 app targets and `base: []`.
- **A rebase merge makes `HEAD^1` the branch's own previous commit**, so the diff sees only
  the last commit. That is how the RomM `DB_HOST` fix (`#180`) merged green and never
  reached ie02. `resolve_diff_base` handles it; do not hand-roll a diff base.
- **`enabled: false` is a tombstone, not a bug to fix.** `roles/service/tasks/main.yaml:69`
  makes converge a no-op for it and preserves data and secrets; the old absent-mode teardown
  fataled on a database it had just dropped (`#248`). Live tombstones today: `bazarr` and
  `speaches` on ie01.
- **`Permission Denied` in converge is usually OpenBao token expiry, not a policy gap.** The
  AppRole mints a 60-minute token; run 6334 lost four of six shards at exactly T+3600s
  (`#245`, `#246`). Do not widen the ACL.
- **`gitea-runners` decapitates its own job** when it recreates the runner executing it, and
  has twice taken unrelated services down. It converges only by deliberate dispatch.
- **`all.vars` sets the legacy alias `ansible_ssh_user: joestump`, which shadows a host-level
  `ansible_user`** (`dub.yaml:214`). Hosts that must be reached as root set both keys.

## Before you push

`make check`, then confirm your change appears in the detect preview above. These are two
different questions: a passing `make check` says the repo is valid, and says nothing at all
about whether the merge will converge anything.
