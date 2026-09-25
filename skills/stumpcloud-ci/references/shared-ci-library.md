# The shared CI library — stump.wtf/ci

Four reusable workflows that a product repo composes into **one** pipeline. Source of truth:
`https://gitea.stump.rocks/stump.wtf/ci` — its `README.md`, its four files under
`.gitea/workflows/`, and `scripts/apply-branch-protection.sh`.

This file carries the graph shape, the context strings, the reasoning, and the corrections. It
deliberately does **not** reproduce each stage's input table: those change, and reading
`on.workflow_call.inputs` in the file itself is one fetch.

## Why the repo exists

The `aibot` workflow was copy-pasted into every repo. Its checkout step could never handle a fork PR
— it resolved the head branch name against the *base* repo, so the job died in about 6 seconds — and
the identical bug had to be found and fixed separately in `stump.wtf/msgbrowse` and
`joestump/dotfiles`. Copies drift, and policy that lives in copies is not policy.

## The four stages

| Workflow | Stage | Job name (the half that appears in a context string) |
|---|---|---|
| `gitleaks.yaml` | secret scan | `gitleaks` under workflow name `Secret scan (gitleaks)` |
| `go-service.yaml` | lint → test → build → publish image | `lint + test`, `container image` |
| `aibot.yaml` | AI code review (LiteLLM) | `aibot` |
| `static-site.yaml` | build → publish to Gitea Pages (Garage) | `build + publish` |

Composed shape, one dependency graph rather than five workflows racing:

```
secret scan -> lint -> test -> build -> ai review
                                  |  (merge to main only)
                    desktop apps . publish packages . Gitea Pages
```

Gitea 1.27 resolves `uses:` server-side, so each called job becomes its own job with its own logs and
its own node in the graph. The composed pipeline reads as one run.

## Reusable, not scoped — and why that is a design decision

Gitea 1.27 also offers **scoped workflows**: register a repo under Organization Settings → Actions →
Scoped Workflows and its workflows run on every repo the org owns, with no file in the consuming
repo. Tempting for policy like secret scanning. Two properties rule it out here:

1. **A scoped workflow is its own run**, so nothing can `needs:` it. AI review would start the
   instant a PR opens, in parallel with the build, burning tokens reviewing code that may not
   compile. Gating it behind `needs: [build]` requires it to be a called job.
2. **A consuming repo cannot disable a scoped workflow.** A repo that composes these stages would
   *also* get an ungated org-level copy, so gitleaks and aibot would each run twice.

Recorded in case that changes: scoped workflows do not support `on: schedule` or `on: workflow_run`,
and a private source repo discloses its workflow logic through consuming repos' logs.

## The worked example

`stump.wtf/msgbrowse`'s `.gitea/workflows/pipeline.yaml` is the only repo running the full graph.
Read it before wiring a new one. Its ordering is deliberate:

- **Secrets first** — a leaked credential is the one finding that must not wait behind a compile.
- **AI review last, behind the build** — as a separate workflow it fired on PR open and spent tokens
  on code that had not been shown to compile.
- **Everything after `main-only` is fenced to a push on the default branch**, stated once in a single
  gate job that downstream stages `needs:`, so the fence cannot drift between them.

Two incidents shaped the rest of it, and both are the same shape — *a job that only ran on main*:

- Eight docs PRs (`#319`–`#326`) went green with nothing having compiled `docs-site`. The Docusaurus
  3.10 bump among them failed the build outright and no check said so. Hence a PR-only `docs` job
  with `publish: "false"` hardcoded rather than computed.
- The lipgloss v2.0.6 bump (`#343`) merged green because the nested `cmd/msgbrowse-desktop` module
  was only exercised in the main-only `desktop` job, so the required `go mod tidy` surfaced as a red
  main (run 6669) instead of a red PR.

A third, subtler one: `npm run build` strips TypeScript types without checking them, so the
TypeScript v7 bump (`#355`) built a perfectly good site while `npm run typecheck` failed and every
check was green. The `build_cmd` is `npm run typecheck && npm run build` for that reason.

## Branch protection — the contract

A reusable workflow cannot set branch protection; that is per-repo config. Context strings are
`<workflow name> / <job display name> (<event>)`. **Calling a reusable workflow emits two contexts** —
the caller's job and the callee's — and both are useful: the caller aggregates, the callee names what
broke.

The `go-service` pipeline set, as `apply-branch-protection.sh` defaults to it:

| Context | Emitted by | Required |
|---|---|---|
| `pipeline / secrets (pull_request)` | caller → `gitleaks.yaml` | yes |
| `Secret scan (gitleaks) / gitleaks (pull_request)` | the callee | yes |
| `pipeline / build (pull_request)` | caller → `go-service.yaml` | yes |
| `go-service / lint + test (pull_request)` | the callee | yes |
| `pipeline / review (pull_request)` | caller → `aibot.yaml` | no, advisory |
| `aibot / aibot (pull_request)` | the callee | no, advisory |
| `go-service / container image (pull_request)` | the callee | no, skipped on PRs |

**A check that runs but is not listed as required gates nothing.** As of 2026-07-29, five of six
`stump.wtf` Go repos (`cairn`, `msgbrowse`, `spotter`, `switchboard`, `stet`) required only the
secret scan and `aibot / aibot`, and did **not** require the job that runs the tests. A red test
suite left the PR mergeable. Only `reduit` had it right.

**AI review is deliberately not required.** It was listed as required on five repos, which was
harmless only because it never ran (trap 1 in `references/gitea-actions-traps.md`). Now that it does
run, requiring it would let a model's opinion — or a LiteLLM outage — hard-block a merge. It
comments; the tests gate.

**There is no single context list to paste everywhere.** The contexts a repo emits depend on which
pipeline it runs: only repos on `pipeline.yaml` emit the `go-service` contexts, `switchboard` runs
its own `ci / ci`, and some repos have no test job at all. Anything `skipped` is not `success`, so
never require a context that is skipped on the event you are gating — `container image` is
main-only, for instance.

## Applying it

```sh
# [mac] OPERATOR step: the script still curls with GITEA_TOKEN, which agent shells do not have
GITEA_TOKEN=... ./scripts/apply-branch-protection.sh stump.wtf/cairn
GITEA_TOKEN=... ./scripts/apply-branch-protection.sh stump.wtf/switchboard main "ci / ci (pull_request)"
```

Until the script moves to `tea api`, an agent does not source a token to run it: hand the command to
the operator. Read the result back with
`tea api --login gitea.stump.rocks repos/<owner>/<repo>/branch_protections`.

The script samples the contexts actually reported on the most recent PR head and **refuses** to
require one that has never been seen, printing what the repo does report instead. Call it; never
reimplement the API call, or you create a second, drifting definition of the required set.

## A correction you will otherwise trip over

`stump.wtf/ci`'s README states, under *Deployment is NOT in these pipelines*, that per ADR-0002
hosts deploy themselves via `ansible-pull` and `stumpcloud/ansible`'s own workflows are
validate-only. **That is no longer true.** `stumpcloud/ansible`'s `ci.yaml` header says outright that
CI now *also* SSH-pushes on merge to main, and the file carries `detect`, `converge-base`,
`converge-apps`, `reconcile`, and `deploy` jobs to do it.

`ci.yaml` is authoritative — it is the thing that runs. No test can span the repo boundary to catch
this, so it is recorded here as a dated contradiction (checked 2026-08-30) rather than guarded.
