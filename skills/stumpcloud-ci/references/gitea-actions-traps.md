# Gitea Actions traps

Where this Gitea 1.27 instance plus its `act_runner` diverge from GitHub Actions. Every row carries
the run, issue, or date it was measured on, so you can re-verify one row cheaply instead of
distrusting the whole file. Rows marked **linted** are also checked by
`scripts/gitea_workflow_lint.py` in this skill directory; rows marked **forge behavior** have no
possible guard and are on the reader.

## 1. A workflow_call callee cannot see the triggering event — linted

Inside a called workflow, `github.event_name` is the literal string `workflow_call` and
`github.event.<anything>` does not exist. `github.repository`, `github.ref`, and `inputs.*` **are**
populated normally. Verified on Gitea 1.27.0, 2026-07-29, for both a local
`./.gitea/workflows/x.yaml` call and a cross-repo `https://.../x.yaml@ref` call.

The consequence is not an error. A job-level `if:` in a callee that tests the event is permanently
false, the job skips on every run, and because a caller job reports the conclusion of the workflow
it called, the caller shows `skipped` too. On the PR page that reads as a deliberate config choice.

- `stump.wtf/ci`'s `aibot.yaml` gate was `(github.event_name == 'pull_request' && ...)`. Every
  branch was false, so **AI review had never run once on any `stump.wtf` repo** — not on PR open,
  not on a push to an existing PR branch. Measured on `stump.wtf/ci#7` in a callee on a real
  non-draft PR; fixed in `stump.wtf/ci#8` by moving the gate to the caller and passing `is_draft`
  down as an input.
- `static-site.yaml`'s publish step had the same clause and silently skipped two real publishes
  (`stump.wtf/harness` run 3348, `stump.wtf/cairn` run 3531). The run 3531 log says it outright:
  *received task 10171 of job site, be triggered by event: workflow_call*. The fix was to fence on
  `github.ref` alone — which is the caller's ref, and on a PR is `refs/pull/<n>/merge`, so dropping
  the event clause widened nothing.
- **Still live on `main` as of 2026-08-30.** `go-service.yaml`'s `image` job gates its registry login
  and its tag-and-push on `github.event_name == 'push' && ...`, inside a callee. `stump.wtf/msgbrowse`
  passes `publish_image: true`, and there is no `msgbrowse` container package in the `stump.wtf`
  registry. Re-derive before acting on this: list the org's container packages and look for the repo
  name, then read the most recent main-branch run's `container image` job log.
- **A second-order version of the same thing**, also live: both `go-service.yaml` and the *fixed*
  `static-site.yaml` fall back on `github.event.repository.default_branch`. Inside a callee that is
  empty, so the `format('refs/heads/{0}', ...)` arm is dead. `static-site.yaml` still works because
  its other arm compares `github.ref` to the literal `refs/heads/main` — but any repo whose default
  branch is not `main` gets a silently false gate.

**Rule:** gate on the event in the caller; hand the callee an input with a permissive default so
existing callers keep working. Inside a callee use `github.ref` and `inputs.*` only — nothing under
`github.event`. Migrating a repo from a *scoped* workflow to a reusable one can silently disable its
gating, because a scoped workflow is triggered by the event directly and sees it fine.

## 2. Boolean workflow_call inputs are dropped — linted

This act_runner drops caller-passed values for `type: boolean` `workflow_call` inputs. The callee
always sees the input's default, at job level and at step level. Proven empirically:
`stump.wtf/ci` run 3149 shows `toJSON(inputs).publish_image == false` while the caller passed
`true`.

Declare flag inputs `type: string`. A bare YAML `true`/`false` from the caller arrives as the string
`"true"`/`"false"` (run 3150), so gates must compare `== 'true'` — **never** rely on bare
truthiness, because the non-empty string `"false"` is truthy.

Prefer opt-out gates for anything destructive-by-omission: `static-site.yaml` uses
`inputs.publish != 'false'` precisely so a missing or mangled value can never silently suppress a
publish again.

`aibot.yaml` still declares `is_draft` as `type: boolean`, so the linter reports it. That one is
tolerable — the action re-checks draft state authoritatively, so losing the caller's value costs a
little money and nothing else. It is the only known instance where the answer is *leave it*.

## 3. The built-in token is rejected by the container registry — linted

The per-run `gitea.token` / `secrets.GITHUB_TOKEN` authenticates fine against the Gitea **API** and
is refused by the Gitea **container registry**:

```
Error response from daemon: Get "https://gitea.stump.rocks/v2/": unauthorized
```

Still true on Gitea 1.27.0 (verified 2026-08-08). In-repo comments claiming it was fixed in 1.26
are wrong. The working pattern is a real PAT with `write:package`:

```yaml
permissions:
  contents: read
  packages: write            # the job needs this too
steps:
  - uses: docker/login-action@v3
    with:
      registry: ${{ vars.LOCAL_REGISTRY }}
      username: ${{ vars.REGISTRY_USER }}       # a VARIABLE
      password: ${{ secrets.REGISTRY_TOKEN }}   # a SECRET
```

**The hour-burning trap:** `REGISTRY_USER` is provisioned as a repo *variable* and `REGISTRY_TOKEN`
as a *secret*. Reading `secrets.REGISTRY_USER` yields empty, which looks like the secret was never
set and tempts a swap to `GITHUB_TOKEN` — which then 401s for a different reason. That exact
mis-diagnosis broke `stumpcloud/gitea-aibot-action`.

**Where the credential comes from:** `playbooks/services/gitea.yaml` in `stumpcloud/ansible` loops
over `conf.actions.repos`. A repo not in that list gets no credential, so adding the repo there is
the durable fix, not setting a secret by hand.

**`conf` is not an inventory key.** The playbook sets `conf: "{{ gitea }}"` at its top, so every
`conf.*` reference in playbook prose — and in the notes that quote it — resolves against the
inventory's `gitea` dict. Searching an inventory for a literal `conf:` finds nothing and reads as
"the list moved". Re-derive the real list:

```sh
# [mac] the inventories carry !unsafe tags, so yq beats a naive yaml.safe_load
yq '.all.vars.gitea.actions.repos' ~/src/ansible/dub.yaml
```

`gitea-aibot-action` was missing from that list. Its push 401'd on every run, `gitea-aibot:v6` was
never published, and every consumer's aibot job died in about 3 seconds with
`failed to resolve reference ...:v6: not found`.

## 4. A glob in branch protection can make an advisory job load-bearing — forge behavior

`joestump/dotfiles` required the status-check context `*pull_request*`, which also matches
`aibot / aibot (pull_request_target)`. When the aibot image went missing, every dotfiles PR became
silently unmergeable from 2026-07-25 for two weeks while `bats`, `lint`, and `gitleaks` were all
green.

Read a repo's `status_check_contexts` before concluding a red check is optional, and prefer exact
context strings to globs. The one place a glob is right is absorbing Gitea's ` (event)` suffix.

## 5. A path-filtered workflow posts no status — linted (warning)

A workflow with `paths:` or `paths-ignore:` does not run at all on a PR that touches nothing
matching, so no status is ever posted and a required check inside it never arrives. The PR is
blocked forever waiting for something that will never come. `stumpcloud/ansible`'s `ci.yaml` is
deliberately unfiltered for exactly this reason (`ci.yaml:98-106`), and gates `pages` with a job
`if:` instead.

The second-order version: a filter whose source list drifts fails *silently*. `ci.yaml`'s `pages`
job stays unfiltered because its inputs (`docs/adrs`, `docs/openspec`, `docs/playbooks`,
`docs/apps`, `docs/guides`, `.claude-ops/`, `CLAUDE-OPS.md`, `docs-site/`) already drifted out of
sync with `docs-site/package.json`'s watch glob once, and the only symptom was that the site quietly
stopped updating.

## 6. Docker action images are cached with forcePull=false — linted

`act_runner` pulls a Docker action's image with `forcePull=false`, so a runner holding a cached
`:latest` never sees a rebuild. That is how `stumpcloud/garage-pages-deploy`'s slugify fix (issue #1)
shipped green and still ran the old entrypoint. Its `action.yml` now pins `:v2` and says so in place.

Pin an immutable tag, and bump the action's tag and the build's tag in the same change. A tag the
runner has not seen cannot be stale. Related: bumping only `action.yml` triggers no rebuild unless
`action.yml` is inside the build workflow's own `paths:`, which is how a reference to an unbuilt
image ships undetected.

## 7. Runner-image facts — forge behavior

Learned from what already works on this instance, not assumed:

- `actions/setup-go` works. **`actions/setup-python` does not** — the runner image has no
  `/opt/hostedtoolcache`. `stumpcloud/ansible` works around it with a system-python venv.
- `actions/upload-artifact@v4` fails here: it detects GHES and exits 1. `msgbrowse` skips artifact
  upload on PRs for this reason.
- GitHub Pages actions do not work. There is no Gitea equivalent of `actions/deploy-pages`;
  publishing goes through `stumpcloud/garage-pages-deploy`.
- Only **statically known** matrices expand into parallel jobs. A dynamic matrix built from
  `fromJSON` of a `needs` output still runs every leg, but serially inside one job.
- A matrix job is named `<job name> (<matrix values, comma-joined>)`. There is no separate display
  name, so a readable check name comes from putting prose in the matrix *values*.
- A container job's workspace is not pre-populated, and Node actions such as `actions/checkout@v4`
  cannot execute in a Python image — hence the plain `git init` + `fetch` checkout steps in
  `stumpcloud/ansible`'s container jobs.
- Sequential jobs beat fan-out here: this act_runner is more contended than a hosted runner, and
  timing-sensitive tests flake when a race suite shares a runner with a compile.

## Reading a run

Against `https://gitea.stump.rocks/api/v1`:

| Step | Endpoint |
|---|---|
| find the run | `GET repos/{owner}/{repo}/actions/runs?limit=20` |
| find the job | `GET repos/{owner}/{repo}/actions/runs/{run_id}/jobs?limit=100` |
| read the log | `GET repos/{owner}/{repo}/actions/jobs/{job_id}/logs` |

**The log endpoint works.** Verified 2026-08-30 against `stumpcloud/ansible` job 51301: 17,427
bytes. Notes elsewhere in these repos still say it 404s and recommend splitting a workflow into one
job per hypothesis to infer failures from which jobs skipped — that workaround is retired.

Pass `limit=100` on the jobs listing. Callee jobs appear *after* their caller, so the default page
truncates exactly the ones you need.

Every one of these reads needs a token: anonymous returns **401**, measured 2026-08-30. The agent
shell carries no `GITEA_TOKEN` in the environment; `tea` is authenticated from its own config file,
and `gh` from the keyring (GitHub only). Fingerprint or length-check a token if you must confirm one
is set — never print it.

**A 500 on `/actions/runs` may be one poison row, not a broken endpoint.** A run whose ref is
shorter than 10 characters panics Gitea's converter, and the listing converts rows in a loop with no
per-item isolation, so one bad row 500s the listing for the whole repo. It looks intermittent
because a small `limit` skips the bad row. Bisect `limit` (1, 2, 4, 8) to prove it is one row, walk
`?limit=1&page=N` to name it, read it via `/actions/runs/<id>/jobs` (a different converter path that
survives), then `DELETE /actions/runs/<id>`. Observed on run 3067 in `joestump/dotfiles`,
2026-07-25. Deleting the run does **not** retract its commit status — a wedged check stays red.
