---
name: stumpcloud-guard-test
description: >-
  Encode a rule as a pytest guard in the stumpcloud/ansible repo so it fails the build instead of
  rotting in prose. Use whenever someone says add a test for this, guard this, write a regression
  test, codify this rule, or make sure this cannot happen again; whenever an OMG action item asks
  to prevent recurrence; whenever a fix just landed and should stay landed; and before writing we
  should always X into an ADR, AGENTS.md, or a runbook. Covers choosing the guard shape, pinning
  the assertion to the artifact that is the real source of truth rather than to a restatement of
  it, asserting content instead of an exit code, writing the incident into the docstring, proving
  the guard goes red before trusting it, and finding guards that silently collect zero cases and
  report green.
license: MIT
---

# Writing a pytest guard for stumpcloud/ansible

A rule left in prose rots silently. A rule written as an assertion fails the build. `tests/` is
where this repo puts rules it intends to keep.

Two docstrings state the whole thesis:

- `tests/test_agents_md_logging_is_current.py:1` — AGENTS.md is read first, so *a stale claim there
  is worse than no claim: it gets acted on.* It advertised the retired `loki` log driver as the live
  design for five and a half months after `daemon.json.j2` moved to `json-file`.
- `tests/test_fleet_timezone.py:14` — *the policy is asserted here rather than left as a sentence in
  an ADR.* Half the ADR corpus is aspirational: 28 accepted, 27 `status: proposed`, 5 superseded
  (2026-08-30; re-derive with `grep -h '^status:' docs/adrs/*.md | sort | uniq -c`).

## Reach for a guard when

- You just fixed something and want it to stay fixed.
- You are about to write "we should always X" into an ADR, `AGENTS.md`, or a runbook.
- A converge broke on a condition CI could have seen for free.
- An OMG action item says "prevent recurrence" — that is a test, not a doc edit.
- Two files must agree and nothing makes them.

## Step 0 — does a guard already exist?

The suite is fast enough to just run. `[mac, in the ansible checkout]`

```sh
pipenv run pytest -q tests              # whole suite: 2001 tests, ~61s (2026-08-30)
pipenv run pytest -q tests/test_foo.py  # one module: sub-second
```

Run it under `pipenv`, not bare `python3`. The system interpreter lacks `jinja2`, so a bare
`python3 -m pytest tests` errors out on 5 modules and quietly collects 1,577 instead of 2,001.

**Do not grep for the file path to find its guards.** Paths are built from fragments, so a literal
grep misses most of them: `grep -rl 'roles/service/tasks/main.yaml' tests/` finds 3 modules, while
the same file is actually guarded by 8 — the rest spell it `REPO_ROOT / "roles" / "service" / ...`
or `os.path.join(REPO_ROOT, 'roles', 'service', ...)`. Grep the intent instead, which lives in the
first line of every module docstring:

```sh
grep -m1 -A1 '^"""' tests/*.py
```

## The authoring loop

1. **Name the incident.** No incident, no guard. A guard whose docstring cannot name what went
   wrong is a guess, and the next reader deletes it as noise.
2. **Find the artifact that is the source of truth, and pin the assertion to it.** Not to a
   restatement. `test_agents_md_logging_is_current.py` reads the live `log-driver` out of
   `daemon.json.j2` and asserts AGENTS.md documents *that* — so the doc follows the template
   forever, rather than both being pinned to a literal that ages.
3. **Pick the shape** before writing code — see `references/guard-taxonomy.md`.
4. **Assert content, never a return code** (below).
5. **Prove it goes red.** Break the artifact in your worktree, run the one module, see the failure,
   revert. An un-reddened guard is a hypothesis.
6. **Assert the corpus is non-empty** before asserting a property over it (below).

## Assert content, never a return code

`ansible-inventory -i broken.yaml --list` **exits 0**. It warns on stderr, emits an empty host set,
and returns success. So does `ansible-playbook`: a run against an unparseable inventory targets zero
hosts and reports a clean green no-op.

That is how a broken `pdx.yaml` reached main on 2026-08-08 — a secret scrub rewrote values to a bare
`***REDACTED***`, whose leading `*` opens a YAML alias, and PDX silently resolved to zero hosts. The
exit-code check used to sign off reported OK, because the exit code really was 0.

So: parse the artifact yourself, and assert the parse yields something. `tests/test_inventory_parses.py`
is the pattern — one test that it parses at all, one that it yields a non-empty host set.

## Guards go vacuous — check yours has not

The characteristic failure of a guard is not being wrong. It is collecting zero cases and passing.

Live on main right now:

```
$ pipenv run pytest -q tests -rs
SKIPPED [1] tests/test_steam_kiosk_mangohud_conf.py:93:  got empty parameter set for (key, value)
SKIPPED [1] tests/test_steam_kiosk_mangohud_conf.py:115: got empty parameter set for (key, value)
```

Those two were written for the MangoHud restart-loop incident. The fixed config no longer carries a
key they scan for, so the parametrize list is empty, the guard checks nothing, and a skip is green.

Four rules, in order of how much they buy:

- **Run `-rs` after writing a parametrized guard.** Contrast the two shapes in that output: the nine
  skips from `test_autorestic_healthchecks.py` each say *dtw.yaml declares no healthchecks block* —
  a deliberate `pytest.skip` naming its reason. "got empty parameter set" names nothing, because
  nobody wrote it.
- **Add a sentinel:** `assert entries, "no entries parsed from <path> — the guard is vacuous"`.
- **Discover the corpus by glob, not by a hardcoded list.** 22 modules hardcode `INVENTORIES = [...]`;
  6 glob. `test_homepage_widget_secret_indirection.py:54` explains why globbing won: the service-role
  guard now *fails a converge*, so an inventory this suite does not scan is one whose converge breaks
  in production rather than in CI.
- **`empty_parameter_set_mark = fail_at_collect` in `pytest.ini` kills the class repo-wide.** Note
  the value: `fail` is illegal and pytest refuses to start. Verified on pytest 9.1.1 — with
  `fail_at_collect` set, the mangohud module errors at collection and *interrupts the whole run*, so
  landing that line means fixing the two vacuous guards in the same change.

`pytest.importorskip("yaml")` at module scope (9 modules use it) is the same hazard wearing a
different hat: it is a fail-open switch that skips the entire module. It is safe here only because
`run-tests.sh` runs inside the pipenv where PyYAML is pinned.

## The docstring is the guard

Line 1 is the rule, as an imperative. The body is the incident, with dates, run numbers, and issue
links — `run 7381 fatal'd on 19 hosts`, `stumpcloud/stumpcloud#215`, `2026-08-21`. Close with why
the assertion lives in `tests/` rather than in the playbook or role, because that is the question
the next editor will have.

Each per-test docstring names the specific mistake that test alone catches. `test_manyfold_ie01_retire.py`
is the model: a dead successor plus a completed teardown is total data loss, so the successor-guard
task must precede all three teardown tasks *by index*.

The dated-log-line comment convention applies here as anywhere else in the repo — append a line
when you amend a guard rather than rewriting its history. `tests/test_host_storage_pool_guard.py:11`
is the shape.

## Placement, naming, running

- One module per invariant: `tests/test_<subject>_<invariant>.py`. 99 modules, 19,187 lines, all
  added since 2026-07-25 (`git log --diff-filter=A --format=%ad --date=short -- tests/ | tail -1`).
- **This repo names test files `test_*.py`.** The `stumpcloud/skills` repo names them `*_test.py`.
  Both are right in their own repo — `pytest.ini`'s `python_files` decides. Do not "fix" one to
  match the other.
- No `__init__.py` in `tests/`; `pytest.ini` sets `--import-mode=importlib`.
- `.gitea/scripts/run-tests.sh` is the single entry point. `make test` wraps it and CI invokes it
  directly, because the ansible-runner image ships no `make` — so local and CI cannot drift.
- **A new guard needs no CI wiring.** The whole suite is one job. That is unlike a new *playbook*,
  which must be claimed by a lane in `.gitea/scripts/select-check-lane.sh` or it is silently never
  syntax-checked (`tests/test_ci_check_lanes.py` guards exactly that).

## Reading inventories and Jinja from a test

`yaml.safe_load` on any inventory raises `ConstructorError: could not determine a constructor for
the tag '!unsafe'` — `dub.yaml` carries 9 of them. 52 of the 99 modules hand-roll the same
tag-tolerant loader, under at least two different names.

Copy-paste code for that, plus the host walk that must not be shadowed by membership-only entries,
service definitions that are legal under both `all.vars` and `all.hosts.<host>`, the shimmed Jinja
environment that renders a real Ansible expression instead of regexing its source, and the YAML
scalar-type traps: `references/inventory-and-jinja-recipes.md`.

## Collection module tests are a different thing

Mocked-client unit tests for the `joestump.*` collections live under
`collections/ansible_collections/joestump/<name>/tests/unit/`, not in `tests/`. ADR-0022
(`status: accepted`) already specifies the directory layout, the `BASE_PARAMS` / `_make_module` /
`@patch.object` pattern, and the required scenarios. Follow it; do not invent a second pattern.

## What not to guard

- **Cosmetic task-name strings.** Pinning a name that carries no meaning turns every rename red and
  trains people to add exemptions.
- **Host or service lists.** They rot. Derive them by walking the inventory, and let the guard fail
  only on the property you actually care about.
- **Anything `ansible-lint` already enforces.** A second, weaker implementation is worse than none.
- **A rule with no failure message naming the fix.** If the assertion message does not tell the
  reader what to change, the guard costs more than it saves.
