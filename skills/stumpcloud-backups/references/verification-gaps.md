# What actually asserts the backups, and what nothing asserts

Read this when a backup failed and every existing signal was green, or before claiming a
signal proves anything. It maps each property that could be false onto the mechanism that
checks it — and, in the second half, names the properties nothing checks at all.

**Re-derive before trusting it.** In `stumpcloud/ansible`:

```sh
# [mac]
sed -n '208,275p' playbooks/services/files/autorestic/.autorestic.yml.j2   # the check location
grep -n 'def test_' tests/test_autorestic_healthchecks.py tests/test_healthcheck_schedules.py
pipenv run pytest tests/test_autorestic_healthchecks.py tests/test_healthcheck_schedules.py
```

If a row below no longer matches the file, the file wins. Line numbers are from
2026-08-30 and drift on every edit; the surrounding comment text is the durable anchor.

## The assertion matrix

| Property | What asserts it | Where | What it still misses |
|---|---|---|---|
| The repository is internally consistent | `restic check` | `.autorestic.yml.j2:249`, `check` location before-hook, weekly `0 6 * * 0` | Says nothing about whether the repo holds anything. Packs can all be valid and referenced by no snapshot |
| A stale lock is not blocking verification | `restic unlock` immediately before the check | `:248` | Only removes locks restic itself judges stale |
| The repository holds at least one snapshot | `snapshots --json \| grep -o short_id \| wc -l` > 0 | `:259` | Repo-wide. Goes green while every location but one is dead. Runs against `check_repo` only |
| Each extra location has ever produced a snapshot | `snapshots --tag <loc> --latest 1 \| grep -q short_id` | `:264-268` | **Existence, not freshness.** A snapshot from six months ago passes. Loops `extra_locations` only |
| A location that fails reaches a human | `failure:` hooks ping `<slug>/fail` plus apprise | `:380-386`, `:175-177` | Renders only when the location declares `healthcheck:`. A location that never starts fires nothing |
| A location that silently stops reaches a human | healthchecks.io missing-ping past its grace | `dub.yaml` `healthchecks.checks` | ADR-0054: healthchecks only ever notices a **missing** ping. A run that completes having refused every source still pings success |
| A source is not empty | `[ -d p ] && [ -n "$(ls -A p)" ]` per `from:` entry | `:170-171`, `:201`, `:348-351` | All-or-nothing per location. One legitimately empty subdirectory kills the whole location, permanently and quietly |
| A staged dump is fresh, not last week | `find <path> -mmin -<n> \| grep -q .` | `:133` (raft), `:168` (sqlite `.staged-ok`) | Fails the backup, which is correct — but nothing separately alerts that the *producer* timer died |
| Every scheduled location declares a dead-man switch | `test_every_extra_location_has_a_healthcheck` | `tests/test_autorestic_healthchecks.py:117` | Config-time only. Iterates hosts under `all.hosts` declaring an `autorestic` dict |
| A declared schedule matches the job cron | `test_extra_location_crons_match_their_declared_schedule` | `tests/test_healthcheck_schedules.py:199` | Same host walk, same blind spot |
| The rendered config parses | `test_rendered_config_is_valid_yaml_for_every_host` | `tests/test_autorestic_healthchecks.py:321` | Valid YAML that backs up nothing is still valid YAML |
| Guards run after the dumps that populate them | `test_empty_source_guard_runs_after_database_dumps` | `:507` | Ordering only |

Everything in the `tests/` column is a **config-time** assertion, gated in CI. Nothing in
that column has ever looked at a live repository. The 2026-08-16 failure was live-content,
which is why 1,021 lines of passing guard tests did not see it.

## What nothing asserts

**1. Freshness, anywhere.** No mechanism in the repo compares a location's newest snapshot
against its own cron. The comment above `:260-263` says each location is asserted to have a
snapshot "inside its own retention window"; the code on `:266` checks only that one exists.
Read the command, not the comment.

**2. Any location on a host with no `extra_locations`.** The per-location loop is
`{% for lname, loc in (autorestic.extra_locations | default({})).items() %}`. A host running
purely on `standard_locations` renders **zero** per-location assertions — its `volumes` and
`caddy` locations are covered only by the repo-wide count. Check with the derivation command
in the skill body: on 2026-08-30 ie01 was in exactly this state, with `volumes` 62 hours
stale behind a `check` that had passed that morning.

**3. Every backend except `check_repo`.** `backend:` may be a list; `check_repo` is a single
string. A dual-backend host verifies one repository and writes blindly to the other. On
2026-08-30 ie01 wrote to Garage and Backblaze and verified only Garage; its 16-snapshot B2
repository is verified by nothing at all.

**4. Any backup outside the `autorestic` inventory group.** `_autorestic_blocks()` in both
test files walks `all.hosts` for a dict-valued `autorestic` key. `cloud01` backs up through
a separate 101-line hand-written template with **no `check` location, no success or failure
hooks, and no declared healthchecks slug**, so it is invisible to all 20 guard tests in that file and has
no dead-man switch. Verified 2026-08-30: the config was templated 2026-08-23, and
`docker ps -a --filter name=switchboard-backup` returned nothing — the container has never
existed, so no backup has ever run. `playbooks/services/cloud01-edge.yaml` restarts it with
`failed_when: false`, so the converge cannot notice.

**5. The verifier itself.** If the `check` location stops running, the only thing that says
so is its own `<host>-restic-check` dead-man switch, declared with a **21,600-second grace**
against a weekly schedule. A missed weekly verification is invisible for six hours past a
run that already only happens once a week.

**6. Backups, in the hourly ops sweep.** `ls .claude-ops/checks/` lists sixteen `verify-*.md`
checks. None of them is about backups.

**7. A restore.** Nothing in the fleet has ever exercised one end to end. ADR-0055:17-18
states it directly. See `references/restore-drill.md`.

## Two mechanical traps that make green readings wrong

**restic groups by `hostname`, and `hostname` here is the container ID.** Nothing sets a
container hostname, so restic records the Docker-generated ID. `restic snapshots --help`:
`--latest n` shows "the last n snapshots **for each host and path**". So
`snapshots --tag <loc> --latest 1` returns one row per container incarnation, not the latest
snapshot — on 2026-08-30 ie01 held 65 snapshots across 9 such groups, and a `--latest 1`
query for `volumes` returned six rows spanning 08-07 to 08-28. Anything that reads the first
row, or counts rows, is reading a dead container. Always sort by `time` yourself.

The same default (`forget --group-by`, default `host,paths`) applies to retention: each dead
incarnation keeps its own `keep-daily`/`keep-weekly`/`keep-monthly` window forever. Any
change here moves real data — propose it as a repo change against
`playbooks/services/files/autorestic/.autorestic.yml.j2`, verify the flag name against
`restic backup --help` inside the container first, and never hand-edit a host.

**A guard is all-or-nothing across a location's sources.** The missing-or-empty check
renders once per entry in `from:`, and any one failing exits the before-hook, which fails the
whole location. A location listing six trees stops backing up all six the day one of them is
emptied. Verified live 2026-08-30: ie02 `media` attempted at 08:03 EDT and produced nothing
because `/voltron/Media/Scans` held zero entries; its newest snapshot was 08-27, and the
per-location existence assertion on that snapshot passed.

## And do not trust a citation either

`dub.yaml` cites "ADR-0053 / SPEC-0031 (drive power distribution)" on the `media` location.
ADR-0053 is the Gitea bot-account model and SPEC-0031 is AWS IAM identities. Open the ADR and
read its `status:` and title before repeating a reference — 27 of the 61 ADRs are
`status: proposed`, and ADR-0023, which governs this entire subsystem, is one of them and
carries a correction block retracting its own PBS topology.
