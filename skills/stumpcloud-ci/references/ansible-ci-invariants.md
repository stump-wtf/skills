# stumpcloud/ansible — one pipeline, eleven jobs

An editing checklist for `.gitea/workflows/ci.yaml`, not a description of it. The file's own first
~105 lines are the design doc; read those for *why*. This file is *what must stay true, what breaks
when it does not, and which test catches it*.

Rows are keyed by **job name**, never by line number. Job names are load-bearing — branch protection
matches `CI / all checks passed (pull_request)` literally — so they move far more slowly than the
file. Re-derive the current job list:

```sh
# [mac] in a stumpcloud/ansible checkout
grep -n '^  [a-z][a-z0-9-]*:$' .gitea/workflows/ci.yaml
grep -n '^    name:' .gitea/workflows/ci.yaml
```

## Per-job invariants

| Job | Invariant | What breaks | Guard |
|---|---|---|---|
| `check` | matrix lanes == `select-check-lane.sh`'s `LANES`, total and disjoint | a playbook in no lane is never syntax-checked; the sweep stays green as coverage shrinks | `tests/test_ci_check_lanes.py` |
| `check` | an empty lane fails the job | a renamed directory reads as "nothing to do" | the step's own `[ ! -s ]` check |
| `test` | every `secrets.X` any workflow references exists on the repo | a job silently receives an empty string | `.gitea/scripts/check-workflow-secrets.py`, wired into this job |
| `test` | every first-party image in the inventories is anonymously pullable | the converge dies at the compose pull *after* merge, and presents as DNS NXDOMAIN because the CNAME task sits behind it | `tests/test_image_pullability_gate.py` |
| `gitleaks` | runs on the host runner, not the ansible-runner container | a Docker action cannot run in a container job with no docker-in-docker | none — it just fails |
| `gate` | every job in `needs:` is also asserted in the step's shell | with `if: always()` an unasserted job can fail while `gate` goes green | **none.** The file warns about it; `scripts/gitea_workflow_lint.py` in this skill checks it |
| `pages` | not path-filtered | a stale source filter fails silently: the site quietly stops updating | none |
| `pages` | holds a cross-run `concurrency` lock on the S3 prefix it writes | the job rclone-syncs to one fixed `stumpcloud.pages/ansible` prefix and that sync PRUNES the destination, so two merges minutes apart rewrite and delete each other's objects mid-transfer. It fails naming files neither diff touched -- `corrupted on transfer: md5 hashes differ` and `Failed to copy: object not found` on unrelated ADR and spec pages -- and the next merge publishes cleanly, so it reads as S3 flakiness rather than a race the workflow creates itself. Runs 1914 and 1915, 2026-08-30, 44 seconds apart. Fixed in ansible#581 | none |
| `detect` | its site list matches the `inventory` dispatch choices | a site outside the list converges nothing, in both directions | `tests/test_detect_deploy_targets.py` |
| `converge-base` | `playbooks/host.yaml --tags storage` runs alongside `base` and `docker` | neither `base.yaml` nor `docker.yaml` reaches `roles/host`, so a merged host-var change has no play that applies it | none |
| `converge-apps` | `detect` is a **direct** entry in `needs:` | the `needs` context holds only jobs named in `needs:`, so `needs.detect.outputs.*` read through the transitive `converge-base` edge is empty and the `if:` is never true | none |
| `converge-base`, `converge-apps` | `concurrency.group` is keyed by job **and** worker | a single static group serializes one run's own matrix legs, and only one job may be pending per group, so queued legs are silently cancelled — a partial deploy that still looks green | none |
| `converge-apps`, `deploy` | the Route53 trio is in `env:` | `amazon.aws.route53` takes no credential parameters, so it falls back to whatever stale key the runner inherits and dies at the CNAME task with `InvalidClientTokenId` — after the app has already deployed | `tests/test_ci_workflow_credentials.py` |
| `converge-apps` | OpenBao token is re-minted **before each playbook** | the converge AppRole mints a 60-minute token; a shard's serial worklist outlives it | none |
| `deploy` | dispatch inputs are validated against the checkout before anything credentialed runs | `choice` menus are **not** enforced on API dispatch, so every input is an arbitrary string | none |
| `smoke` | stays a plumbing probe, not a `--check` converge | check-mode noise in service roles trains everyone to ignore a red smoke | none |

Where the Guard column says none, the rule is on you or on the linter — say so in the PR.

## The converge lane, end to end

```
gate -> detect -> converge-base (3 workers) -> converge-apps (6 workers) -> reconcile
```

`detect` diffs the pushed range and emits `base`, `apps`, `has-base`, `has-apps`. Everything
downstream is `if:`-gated on those, and **a skipped job is green**, so a wrong diff base produces a
fully green run in which nothing shipped.

### The diff base, and why it is not HEAD^1

`resolve_diff_base()` in `.gitea/scripts/detect-deploy-targets.py` prefers `github.event.before` and
falls back to `HEAD^1`. Both are needed, because neither is correct for every merge style:

- **Rebase merge** replays the branch's commits onto main one at a time, so the pushed HEAD's first
  parent is the branch's own previous commit, not prior main. Diffing from it sees only the PR's last
  commit — routinely a lint fixup that maps to no service. That is exactly how the RomM `DB_HOST` fix
  (`stumpcloud/stumpcloud#180`) merged green and never reached ie02.
- **Merge commit**: `^1` is main and `^2` is the branch, so `HEAD^1` is right — and Gitea did not
  always populate `event.before` on those pushes, which is why the lane moved to `HEAD^1` originally.

The candidate is only used when it exists, is a real commit, and is an ancestor of HEAD.
`tests/test_deploy_diff_base.py` builds a real git repo per merge style in `tmp_path` and asserts the
resolved base is prior-main in each. **Do not reimplement this** — a second copy will disagree.

Debug it locally:

```sh
# [mac] in a stumpcloud/ansible checkout; PyYAML is required, not optional
python3 .gitea/scripts/detect-deploy-targets.py "$(git merge-base origin/main HEAD)" HEAD
```

Read stderr. An empty `base` **and** an empty `apps` means nothing will ship — legitimate for a docs
or CI change, indistinguishable from a broken base otherwise, which is why the job prints the range
it used into the step summary.

Without PyYAML the script cannot parse an inventory and degrades to every service playbook against
every inventory — roughly 150 targets, a fleet-wide converge. The job fails loudly instead.

### converge-skip.txt — two syntaxes

`.gitea/converge-skip.txt` (note: at `.gitea/`, not `.gitea/scripts/`) is the operator-only list.

- `stem` — skip `playbooks/services/<stem>.yaml` on every inventory.
- `stem:inventory.yaml` — skip one (playbook, inventory) pair; it still converges elsewhere.

Everything on it needs credentials CI does not have, or would be a loop in which CI grants itself
privilege — `openbao`, `ci-secrets`, `ci-policy-reconciler`, `openbao-deploy-identities`,
`gitea-ci-vault-secrets`, `repo-factory-identity`. Re-derive rather than trusting that list:

```sh
# [mac] entries only, comments stripped
sed -e 's/#.*//' -e '/^[[:space:]]*$/d' .gitea/converge-skip.txt
```

Two things it teaches. First, `openbao-policies` is deliberately **not** listed: it self-authenticates
via the policy-reconciler AppRole and must converge on merge — a skip there is how `default_role` sat
un-flipped on the live cluster for a month. Second, an entry can be an interim mitigation rather than
a fix (`speedtest-tracker:pdx.yaml` exists because nuc01 has an unmanaged MariaDB whose root password
predates the purge); each carries the issue that retires it.

### The OpenBao re-mint

`converge-apps` calls `.gitea/scripts/vault-preflight.sh` **before every playbook**, not once per
job. The converge AppRole mints a 60-minute token, and run 6334 lost four of six shards at exactly
T+3600s, every remaining playbook dying with `Permission Denied`
(`stumpcloud/stumpcloud#245`).

The failure mode that matters more than the fix: **an expired token and a missing ACL grant produce
the identical error**, and the natural wrong conclusion from a 403 is to widen a policy that was
never broken (`stumpcloud/stumpcloud#246`). The job now compares wall clock against the token's
expiry and, when it is past, prints `this is token expiry, NOT a policy gap — do not widen the
converge ACL`. If you see that line, the ACL is fine.

`VAULT_*` are scoped to the **step**, never the job: the preflight script exports its validated token
through `GITHUB_ENV`, and a job-level `VAULT_TOKEN` would shadow that write on runners where job env
wins — silently putting every playbook back on a stale repo secret.

### reconcile, and a live no-op worth knowing about

`reconcile` restarts containers left stopped by a dead converge shard: `docker_compose_v2` with
`recreate: auto` stops the running container before creating its replacement, and a shard that dies
in that window leaves the service down (`stumpcloud/stumpcloud#254`).

It extracts hosts with an inline `yaml.safe_load` inside a heredoc, wrapped in
`2>/dev/null || true`. The site inventories carry `!unsafe` tags, so a plain `safe_load` **throws**,
the error is swallowed, and the host list comes back empty. Measured 2026-08-30:

```sh
# [mac] dub and dtw throw; pdx and gva parse
for f in dub.yaml dtw.yaml pdx.yaml gva.yaml; do
  printf '%s: ' "$f"
  python3 -c "import yaml;yaml.safe_load(open('$f'));print('ok')" 2>&1 | tail -1
done
```

So the safety net is a no-op on the two largest sites. Its walk is also host-block-only
(`all.children[*].hosts`), which misses anything defined elsewhere in the tree. Fixing it means a
tolerant loader plus both surfaces — the same walk every inventory reader in this fleet needs.

Note also that `reconcile` SSHes as `root@`. That is CI's own connection identity, carried by
`secrets.SSH_PRIVATE_KEY`; SSH from the operator Mac lands as `joestump`. Do not copy `root@` out of
this file into a command you run by hand.

## Two lists that are not the skip list

- `.gitea/validate-skip.txt` — files that are not standalone plays by design (templated `hosts:`,
  include targets). A merely broken playbook does **not** belong here; the sweep exists to surface it.
- `.gitea/validate-extra.txt` — extra `<playbook> <inventory>` pairs for playbooks that must validate
  against more than one site. Everything is already checked against `dub.yaml`, so list only the
  additional inventories.
