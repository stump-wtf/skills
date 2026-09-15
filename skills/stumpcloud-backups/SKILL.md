---
name: stumpcloud-backups
description: >-
  Verify, diagnose, and extend the StumpCloud restic and autorestic backups plus
  the healthchecks.io dead-man switches meant to notice when one stops. Load
  whenever someone asks are the backups working, is X restorable, check the
  backups, when did Y last back up, restore this service, add a backup for a new
  host or path, or after any incident that ends with do we still have a copy of
  this. Also load when a restic-check or a volumes, critical, or media
  healthcheck goes red, when a backup looks stale, and before editing an
  autorestic block in inventory. Teaches asserting live repository content
  rather than exit codes, because a green verifier, a passing restic check, and
  a full bucket are all compatible with nothing being restorable. Covers the
  restic tier only - ZFS pools and Proxmox Backup Server belong to other skills.
license: MIT
---

# StumpCloud backups

## Assert content. Never assert a return code.

| Signal that reads green | What it is compatible with |
|---|---|
| `restic check` passes | a repo holding **zero snapshots** — ie02 b2 sat at 45,658 packs / 0 snapshots through weekly green checks (`.autorestic.yml.j2:250-259`) |
| the weekly `<host>-restic-check` is green | a location that has been dark for months (the per-location hook asserts a snapshot **exists**, not that it is recent — `:266`) |
| the `autorestic` container is `Up` | every location refusing to run on a fail-closed guard, hours ago |
| a location ran, exit 0 | nothing happened — a stale `running: true` lock makes every later tick exit **zero** (ie02 `critical`, 6 days dark from 2026-08-10) |
| a snapshot exists | it covers **0 bytes** — ie02 `media` snapshot f47e01a1, six trees at 0 B, after voltron detached (2026-08-16) |
| the config is templated and committed | no agent has ever run it (cloud01, below) |

**Never conclude "backups are fine" from an exit status, a container status, a Homepage
tile, or a healthchecks.io row.** Conclude it from a snapshot with a plausible byte count,
timestamped inside its own schedule, in every repository the host writes to.

## Where each command runs

- **[mac]** = the operator Mac. BSD userland: no `timeout`/`gdate`/`gstat`; `date -d` and
  `stat -c` fail. Epoch to local time is `date -r <epoch>`. `restic` and `autorestic` are
  **not installed here** — there is no local path to a repository.
- **[host]** = over SSH, which lands as **`joestump`**, then `sudo -n docker exec`.
  `ssh root@` is refused by these hosts. The container is named `autorestic` for hosts in
  that inventory group and something else elsewhere (`switchboard-backup` on cloud01) —
  confirm with `sudo -n docker ps --format '{{.Names}}' | grep -iE 'restic|backup'` first.
- Repository passwords and object-store keys live only in the container environment. Never
  print them, and never `docker inspect` the container's `Config.Env`.

## Derive the surface — never quote a host list

```sh
# [mac] which hosts declare autorestic, their repos, and what is verified
cd ~/src/ansible && python3 - <<'PY'
import yaml
class L(yaml.SafeLoader): pass
L.add_multi_constructor('!', lambda l, s, n: l.construct_scalar(n))
for inv in ('dub.yaml', 'dtw.yaml', 'pdx.yaml', 'gva.yaml'):
    for h, c in ((yaml.load(open(inv), Loader=L) or {}).get('all', {}).get('hosts') or {}).items():
        a = (c or {}).get('autorestic')
        if not isinstance(a, dict): continue
        be = a['backend'] if isinstance(a['backend'], list) else [a['backend']]
        print(inv, h, 'locations=' + (','.join(sorted(a.get('extra_locations') or {})) or 'standard'))
        for n in be:
            print('   repo', a['backends'][n]['type'] + ':' + a['backends'][n]['path'],
                  '<-- CHECKED' if a['backends'][n]['type'] + ':' + a['backends'][n]['path'] == a.get('check_repo') else '<-- verified by nothing')
PY
```

PyYAML is not stdlib but is installed; the `!unsafe` multi-constructor is required or
`dub.yaml` raises `ConstructorError`. Declared cadence and grace live in one place —
`dub.yaml` `all.vars.healthchecks.checks[*].{slug,schedule,grace}` — for the whole fleet.

**Then look outside the group.** A host can back up without being in it. `cloud01` runs a
hand-written config from `playbooks/services/templates/cloud01-edge/autorestic.yml.j2`,
is absent from the `autorestic` inventory group, and `gva.yaml` contains zero `restic`
matches — so all 20 guard tests in `tests/test_autorestic_healthchecks.py`, which iterate
hosts declaring an `autorestic` dict under `all.hosts`, skip it entirely.
Find the rest with `grep -rln autorestic playbooks/services/`.

## The two-axis verdict

Every location has two clocks. Read both, always.

```sh
# [host] clock 1 - autorestic's own last ATTEMPT per location (epoch)
ssh <host> 'sudo -n cat /volumes/autorestic/state/.autorestic.lock.yml'
# [mac] decode one:  date -r <epoch>     (BSD; date -d @<epoch> is GNU and fails here)
```

```sh
# [host]+[mac] clock 2 - what actually landed, per location, per repo
ssh <host> "sudo -n docker exec autorestic restic -r '<repo>' snapshots --json" | python3 -c '
import sys, json, datetime
snaps = json.load(sys.stdin); now = datetime.datetime.now(datetime.timezone.utc); best = {}
for s in snaps:
    loc = next((t.split(":")[-1] for t in s.get("tags", []) if t.startswith("ar:location:")), "?")
    t = datetime.datetime.fromisoformat(s["time"][:26] + s["time"][-6:])
    if loc not in best or t > best[loc][0]:
        best[loc] = (t, (s.get("summary") or {}).get("total_bytes_processed"), s["short_id"])
print("snapshots=%d" % len(snaps))
for loc, (t, b, sid) in sorted(best.items()):
    print("  %-10s %s age=%.1fh bytes=%s %s" % (loc, t.isoformat(), (now - t).total_seconds() / 3600, b, sid))'
```

Repo strings are `type + ":" + path` from the block above. **Type them from that output,
never from memory.** Sort by `time` yourself: `restic snapshots --latest 1` returns one row
*per host-and-path group*, and the `hostname` restic records here is the **container ID**,
so every recreate opens a new group — ie01 showed 9 groups across 65 snapshots on
2026-08-30. The same grouping applies to `forget --group-by` (default `host,paths`), so
retention is applied per container incarnation.

| attempted recently | snapshot recent | verdict |
|---|---|---|
| yes | yes | **OK** — confirm the byte count is plausible, not 0 and not the 255 B no-op |
| **yes** | **no** | **GUARD TRIP** — it ran and refused. A before-hook failed. This is the common case and it is invisible |
| no | no | **NOT RUNNING** — stale lock, dead container, or no agent at all |
| no | yes | clock skew or a manual run; re-derive before concluding anything |

Verified live 2026-08-30, both GUARD TRIP, both green to every existing signal:
ie01 `volumes` attempted 02:03 EDT, newest snapshot 08-28 — `/backup/staging/openbao/raft.snap`
was from 08-28 05:30, older than the 360-minute guard, so 545 GB refused for two days.
ie02 `media` attempted 08:03 EDT, newest snapshot 08-27 — one of its six sources,
`/voltron/Media/Scans`, is empty, and the missing-or-empty guard is all-or-nothing.

## Verdict to action

| Verdict | Do this |
|---|---|
| GUARD TRIP | Read the location `before:` hooks in `.autorestic.yml.j2`, then test each guard on the host. Fix the **staging producer** (`openbao-raft-snapshot.timer`, `sqlite-backup.timer`) or the empty source. Never delete the guard |
| NOT RUNNING | `grep running /volumes/autorestic/state/.autorestic.lock.yml`. The container reaps a stale lock each 15-min tick, so a stuck `running: true` means the reaper is broken too |
| ZERO SNAPSHOTS | Treat as data loss until disproved. Not a check failure — an incident |
| HOLLOW (0 bytes) | Stop the location before its `forget: prune` ages out the real snapshots behind it |
| NO AGENT | Config on disk, nothing running. Incident |
| Repo nothing verifies | Compare each backend against `check_repo`; only one repo per host is checked |

Why an existing green signal did not catch it: `references/verification-gaps.md`.

## Changing or adding a backup

The `autorestic.*` key surface is defined by its only consumer,
`playbooks/services/files/autorestic/.autorestic.yml.j2` (388 lines, each rule commented
with the incident that caused it). Read it; the published `docs/playbooks/autorestic.md`
is stale in at least five places, including a `crond` command and a `homeassistant`
location that no longer exist.

Non-negotiable, each pinned by a test in `tests/test_autorestic_healthchecks.py`:

- Every scheduled location declares a `healthcheck:` — the extra_locations that lacked one
  is how `critical` went 6 days dark.
- Declare the slug **and its real cron** in `dub.yaml` `healthchecks.checks`. An auto-created
  check inherits a 24h period, so a weekly job is red six days in seven by construction.
- A slug is immutable; healthchecks.io never regenerates it on rename, and `?create=1`
  silently provisions a **duplicate** rather than 404ing.
- Anything with a database engine in front of it dumps first, into `/backup/staging`.
- Empty-source guards run **after** the dump hooks, never before.
- A guard that can fail on one empty subdirectory kills the whole location. Prefer excluding
  the volatile path over adding a source that may legitimately empty.

```sh
# [mac] the repo owns 1,021 lines of guard tests for this contract - run them, do not reimplement
cd ~/src/ansible && pipenv run pytest tests/test_autorestic_healthchecks.py tests/test_healthcheck_schedules.py
# [mac] deploy - needs the fork-safety and VAULT_ADDR setup the stumpcloud-ansible skill covers
cd ~/src/ansible && pipenv run ansible-playbook -i dub.yaml playbooks/services/autorestic.yaml
```

Merging the change is what deploys it on the converge lane; a manual run is for iterating
before the merge. **A config change alone proves nothing** — come back afterwards and read
both clocks above, because a new location can render, converge green, and then refuse on
its very first guard.

## Proving it restores

A backup nobody has restored is a hypothesis, and ADR-0055 says plainly it has never been
rehearsed end to end for most services. Rehearse it non-destructively:
`references/restore-drill.md`. The put-back is a human decision; the rehearsal is not.

## Not this skill

- ZFS pool health under the backup — the `stumpcloud-storage` skill.
- Proxmox Backup Server, `buoy`, `vzdump` — the `stumpcloud-proxmox` skill. ADR-0023 is
  `status: proposed` and its body describes a PBS topology that was never built; only its
  2026-08-08 correction block reflects reality.

## Escalate

Four conditions are incidents, not findings: any repo holding zero snapshots; a host whose
config exists with no agent running; any location dark past two of its own cycles; a
`forget: prune` that ran after a hollow snapshot. Propose the postmortem before filing it.
