---
name: stumpcloud-ci
description: >-
  Change, gate, or debug CI on the self-hosted Gitea at gitea.stump.rocks and its act_runner.
  Use when CI is red, when a check runs but does not gate, when a pull request is unmergeable
  while every check looks green, when a converge or deploy never happened after a merge, when a
  docs site quietly stopped updating, or when a new repo needs wiring into the shared pipeline.
  Covers reading a run and its job logs over the REST API, the syntax and lint lanes plus the one
  required status context in stumpcloud/ansible, the reusable workflows in stump.wtf/ci and how a
  product repo composes them, branch-protection context strings, and the act_runner behaviors that
  diverge from GitHub Actions - a workflow_call callee cannot see the triggering event, boolean
  inputs are dropped, and the built-in per-run token is rejected by the container registry.
---

# StumpCloud CI

Every failure in this fleet has been silent at least once. `skipped` is green, a path-filtered
workflow posts no status at all, and a check that is not named in branch protection gates nothing.
Assume green means nothing until you know which context the protection rule requires.

## Which pipeline am I touching

| Repo shape | Pipeline | Read |
|---|---|---|
| `stumpcloud/ansible` | its own monolith `.gitea/workflows/ci.yaml` — validate, converge, and deploy in one graph. Does **not** use the shared library. | `references/ansible-ci-invariants.md` |
| a `stump.wtf` / `stumpcloud` product repo | composes reusable stages from `stump.wtf/ci` into one `pipeline.yaml` | `references/shared-ci-library.md` |
| an action repo (`gitea-aibot-action`, `gitleaks-action`, `garage-pages-deploy`, `opentofu-gitops`) | a Docker action; the immutable-tag rule dominates | `references/gitea-actions-traps.md` |

**The invariant that decides everything downstream: a status check gates only if branch protection
names its exact context string.** Context strings are `<workflow name> / <job display name>
(<event>)`. Rename a job and every open PR becomes unmergeable.

## Before you edit a workflow

```sh
# [mac] offline: no credentials, no network, no forge. Paths below are relative
# to THIS skill directory -- resolve them against the location your harness gave
# you for this skill, not against the working directory.
python3 scripts/gitea_workflow_lint.py <repo>/.gitea/workflows/*.yaml
python3 scripts/gitea_workflow_lint.py --inventory ~/src/ansible/dub.yaml <workflow>
```

It encodes six structural mistakes that YAML syntax and `yamllint` cannot see, each keyed to a real
outage. `yamllint` is installed here and passes every one of them; `actionlint` is not installed and
knows none of these Gitea behaviors anyway. Read `--help` for the rule list.

Then run the repo's own guard tests. In `stumpcloud/ansible` that is `make test`, which includes
`tests/test_ci_*.py` — those tests are what actually block a merge, and a skill-side linter never
substitutes for them.

## stumpcloud/ansible at a glance

`.gitea/workflows/ci.yaml` (1,257 lines as of 2026-08-30; `wc -l` re-derives it). Its first ~105
lines are the design rationale — read them before editing rather than re-deriving them.

| Job | Shown as | Runs when | Does |
|---|---|---|---|
| `check` | `syntax+lint (<lane>)` | every event | static matrix over named lanes (`.gitea/scripts/select-check-lane.sh --lanes` prints them), syntax-check plus lint over every playbook |
| `test` | `unit tests (pytest)` | every event | pytest, plus the workflow-secret and image-pullability gates |
| `gitleaks` | `gitleaks` | every event | tree scan at HEAD, host runner not the container |
| `gate` | `all checks passed` | `if: always()` | fan-in. **The required check.** |
| `pages` | `publish docs site` | push to main | Docusaurus to Gitea Pages |
| `detect` | `detect deploy targets` | push to main | diffs the merge into base and app targets |
| `converge-base` | `converge base (<n>-of-N)` | `has-base` | base, docker, host storage, binnacle-probe per inventory |
| `converge-apps` | `converge services (<n>-of-N)` | `has-apps` | changed service playbooks |
| `reconcile` | `reconcile stopped containers` | after converge-apps | restarts containers a dead shard left stopped |
| `deploy` | `manual deploy` | `workflow_dispatch` | one playbook, one inventory |
| `smoke` | `smoke` | `schedule` | probes the deploy lane's plumbing so it cannot rot |

The workflow is deliberately **not** path-filtered (`ci.yaml:98-106`): a filtered workflow posts no
status on a non-matching PR, so a required check never arrives and the PR wedges forever.

## The traps, one line each

Full symptom, mechanism, and evidence for each: `references/gitea-actions-traps.md`.

- A `workflow_call` callee sees `event_name == 'workflow_call'` and has no `github.event` payload —
  put the event gate in the **caller**; `github.ref` and `inputs.*` are fine in a callee.
- `type: boolean` `workflow_call` inputs are dropped by this act_runner — declare `type: string` and
  compare `== 'true'`; the non-empty string `"false"` is truthy.
- The built-in per-run token authenticates to the Gitea API but is 401-rejected by the Gitea
  registry — use `vars.REGISTRY_USER` plus `secrets.REGISTRY_TOKEN`; `REGISTRY_USER` is a variable.
- Branch-protection contexts are globbed against the full `workflow / job (event)` string — a bare
  `check` matches nothing, and a `*pull_request*` glob makes an advisory job load-bearing.
- `skipped` is not `success`, so never require a context that is skipped on the event you gate.
- A Docker action's image is pulled with `forcePull=false`, so a cached `:latest` never updates —
  pin an immutable tag and bump it with the build.

## Changing a check lane

`.gitea/scripts/select-check-lane.sh` owns the lane-to-playbook mapping; the `check` matrix in
`ci.yaml` must list the same lane names in the same order. Edit **both**, then run
`tests/test_ci_check_lanes.py`, which fails on drift, on a playbook in two lanes, on a playbook in
none, on an empty lane, and on a greater-than-4x imbalance. The last services lane is a catch-all by
construction, so a new playbook cannot silently fall out of the sweep.

## Adding a job, or a secret

- **A new job that must gate goes in `gate`'s `needs:` AND in its assertion script.** With
  `if: always()` the step runs whatever the upstream results were, so a job in `needs:` alone can
  fail while `gate` goes green. The file says so itself at `ci.yaml:396-401`.
- **A new `secrets.X` must actually exist on the repo.**
  `.gitea/scripts/check-workflow-secrets.py` asserts that inside the `test` job;
  `playbooks/services/ci-secrets.yaml` is the single writer that reconciles it from OpenBao.
- **Names starting `GITEA_` or `GITHUB_` are refused by Gitea** and become silent no-ops. Pinned by
  `tests/test_ci_secret_names_are_storable.py` after the repo-factory hole that sat open ten days
  with three green jobs.
- **Any job that runs Ansible for real needs the Route53 trio** (`AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`) or DNS publishing dies *after* a successful deploy.
  Pinned by `tests/test_ci_workflow_credentials.py`.

## Branch protection

`stumpcloud/ansible` must require exactly `CI / all checks passed (pull_request)` — never the
`(push)` variant, which wedges PRs behind a permanently grey Skipped/Required row.

**Do not hand-roll the API call.** `stump.wtf/ci`'s `scripts/apply-branch-protection.sh
<owner>/<repo> [branch] [context...]` applies it idempotently, prints before and after, and refuses
to require a context the repo has never reported (it samples the most recent PR head's statuses).
Requiring a context a repo never emits wedges every PR in it. Per-pipeline context tables:
`references/shared-ci-library.md`.

## Debugging a red run

Read path, in order, against `https://gitea.stump.rocks/api/v1`:

1. `GET repos/{owner}/{repo}/actions/runs?limit=20`
2. `GET repos/{owner}/{repo}/actions/runs/{run_id}/jobs?limit=100` — paginate; callee jobs sort
   **after** their caller, so the default page is exactly what drops them.
3. `GET repos/{owner}/{repo}/actions/jobs/{job_id}/logs`

**That log endpoint works.** Verified 2026-08-30 on `stumpcloud/ansible` job 51301: 17,427 bytes of
real log. Several files in these repos still claim it 404s — they are stale, and so is the
one-job-per-hypothesis workaround they recommend. It needs a token; anonymous is 401 (measured the
same day). The credentialless-shell token sources are in `references/gitea-actions-traps.md`.

Two reading habits that beat reading the diff:

- A check that dies in about 3 seconds is infrastructure — an image pull or an auth failure — not
  the change under review.
- When *every* PR in a repo fails the *same* check, stop reviewing PRs and go find the shared cause.

## Not covered here

PR mechanics, review, merge-on-green, worktrees, and force-push policy belong to the PR-review
workflow and the standing agent rules. So does generic red-CI triage: reproduce locally, make the
minimal fix, one concern per PR. This skill covers only what is specific to this forge, this
act_runner, and these pipelines.

## Files in this skill

| File | Open it when |
|---|---|
| `references/gitea-actions-traps.md` | authoring or debugging a workflow, or reading a red run |
| `references/shared-ci-library.md` | wiring a new product repo, or changing a composed pipeline |
| `references/ansible-ci-invariants.md` | editing `stumpcloud/ansible`'s `ci.yaml` or its converge lane |
| `scripts/gitea_workflow_lint.py` | before every workflow edit; `--help` lists its six rules |

Paths in that table are relative to this skill's own directory.
