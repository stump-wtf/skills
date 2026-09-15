# Rehearsing a restore without breaking anything

A backup nobody has restored is a hypothesis. ADR-0055:17-18 says the recovery path "has
never been rehearsed end to end for most services", so the rehearsal is the missing
evidence, not a nicety.

Every command below runs **inside the autorestic container, over SSH as `joestump`**:
`ssh <host> "sudo -n docker exec autorestic sh -c \"restic -r '<repo>' ...\""`. There is no
restic on the operator Mac and no local path to any repository. Get `<repo>` from the
derivation command in the skill body — `type + ":" + path` — never from memory.

Restic version at the time of writing was 0.17.0 (`restic version`); confirm the flags
below with `restic <cmd> --help` in the container before running anything with a `--target`.

## Step 0 — do not fight the schedule

autorestic holds one lock per host, so a location cannot start while another runs. Restic
holds its own repository lock; the repo's own comment at `.autorestic.yml.j2:235-241`
records that `restic check` takes an **exclusive** one, which is why a leaked lock blocked
verification for days in August 2026. A restore is not exclusive but still contends.

- Check what is due: `sudo -n cat /volumes/autorestic/state/.autorestic.lock.yml` gives the
  last-attempt epoch per location. Decode on the Mac with `date -r <epoch>` — BSD form;
  `date -d @<epoch>` is GNU and fails there.
- Exit status **11** from any restic command means the repository is already locked. Wait,
  or clear a genuinely abandoned lock with `restic unlock`. The container reaper clears
  autorestic's `.autorestic.lock.yml` on each 15-minute tick; it does **not** clear restic's
  repository locks.

## Step 1 — pick a snapshot, do not let restic pick it

```sh
# [host] every snapshot for one location, newest last
restic -r '<repo>' snapshots --json --tag ar:location:<name> | jq -r \
  '.[] | [.short_id, .time, .hostname, (.summary.total_bytes_processed|tostring)] | @tsv' | sort -k2
```

`ar:location:<name>` is the reliable selector. Live snapshots carry three tags — the user
tag, `ar:cron`, and `ar:location:<name>` — and only the last is unambiguous.

**Do not use `--latest 1`.** It returns the last snapshot *for each host and path group*,
and the `hostname` restic records here is the container ID, so a recreate opens a new group.
On 2026-08-30 a `--latest 1` query for ie01 `volumes` returned six rows spanning 08-07 to
08-28. Sort by `time` and take the tail.

**Sanity-check the byte count before trusting the snapshot.** `summary.total_bytes_processed`
is per snapshot: a real ie01 `volumes` snapshot was 545,062,246,937 B, the `check` location's
no-op is 255 B, and the hollow one ie02 `media` wrote on 2026-08-16 was 0 B across six trees.
A snapshot two orders of magnitude below its neighbors is the thing you came to find.

`jq` is present in this image; verify with `command -v jq` before relying on it, or pipe the
JSON back to the Mac and parse it with `python3`.

## Step 2 — look inside before extracting anything

```sh
# [host]
restic -r '<repo>' ls <short_id> /backup/volumes/<service> --recursive --long | head -40
restic -r '<repo>' dump <short_id> /backup/staging/postgres/dump.sql | head -5   # to stdout
restic -r '<repo>' find --snapshot <short_id> '<filename>'
```

**Paths inside a snapshot are the CONTAINER's paths, not the host's.** A snapshot of ie01
records `/backup/volumes/caddy/data`; the same bytes live at `/volumes/caddy/data` on the
host. Translating in the wrong direction is why a first-attempt restore lists nothing and
looks like data loss. Derive the mapping rather than assuming it:

```sh
# [host]
sudo -n docker inspect autorestic --format '{{json .Mounts}}' | python3 -c \
  'import sys,json; [print(x["Source"],"->",x["Destination"],"RW=",x["RW"]) for x in json.load(sys.stdin)]'
```

## Step 3 — choose a target that is legal, then dry-run

Almost every mount in this container is **read-only** by design: on ie01, `/backup/volumes`
is RO, and on ie02 so are `/backup/critical` and `/backup/media`. A restore cannot land on
the paths the snapshot records. Read the `RW=` column from Step 2 and pick a writable one —
in practice the staging mount.

```sh
# [host] zero writes; prints exactly what would be extracted
restic -r '<repo>' restore <short_id> --target /backup/staging/restore-drill \
  --include '/backup/volumes/<service>' --dry-run -vv | head -40
```

Restic recreates absolute paths **under** the target, so this yields
`/backup/staging/restore-drill/backup/volumes/<service>/...`.

Two flags to keep away from a real tree:

- `--overwrite` defaults to **`always`**. A target pointed at live data overwrites it.
- `--delete` removes files in the target that are absent from the snapshot. Never combine it
  with a shared directory.

## Step 4 — extract, verify, and account for where it landed

```sh
# [host]
restic -r '<repo>' restore <short_id> --target /backup/staging/restore-drill \
  --include '/backup/volumes/<service>' --verify
```

`--verify` re-reads the restored files and compares content, which is the difference between
"restic exited 0" and "the bytes are right". Then verify at the application layer, because a
byte-perfect copy of a torn database is still a torn database:

- Postgres dump: `head -5` should show a `pg_dumpall` header, and `grep -c '^CREATE DATABASE'`
  should match the databases you expect.
- SQLite: `sqlite3 <file> 'PRAGMA integrity_check;'` must print `ok`.
- A service tree: compare the file count and total size against the live directory.

**Then clean up, and understand why it matters.** `/backup/staging` maps to
`/volumes/backup-staging` on the host, which is itself inside the `volumes` location's
`from:` list. A restore tree left there is silently included in the next nightly snapshot —
doubling its size and, for anything sensitive, storing a second copy under different
retention. `sudo -n rm -rf /volumes/backup-staging/restore-drill` on the host when done, and
confirm the next snapshot's byte count returns to its baseline.

## Cost and blast radius

- Restoring from a `b2:` repo is Backblaze egress and is billed. On a dual-backend host
  rehearse against the Garage repo (`s3:https://s3.stump.rocks/...`), which is on-cluster.
- `restic check --read-data` downloads **every pack in the repository**. It is the strongest
  integrity assertion available and the most expensive; the scheduled weekly check
  deliberately does not use it. Do not run it on a whim, and never on B2 without saying so.
- `restic stats <short_id>` gives a size without transferring the data.

## Two restores that are not rehearsals

Stop and hand these to a human. Both replace live state and neither has a non-destructive
half worth practicing alone.

- **A Postgres import.** Restoring the dump file is safe; loading it into the running
  cluster is not — `pg_dumpall` output includes role and database creation, so replaying it
  against a live cluster is a fleet-wide event, not a service-level one.
- **An OpenBao raft snapshot restore.** `bao operator raft snapshot restore` replaces the
  entire secrets cluster. Every service on the host reads its credentials from it. The
  staged snapshot at `/backup/staging/openbao/raft.snap` is produced by
  `openbao-raft-snapshot.timer`, not by the backup; if it is stale, fix the timer, because
  the backup will refuse to run rather than ship a stale copy of the secrets cluster.

Both belong in a postmortem-grade change with a human present, not in a drill.
