---
name: stumpcloud-media
description:   Diagnose and clean up Sonarr, Radarr, and Lidarr download backlogs across qBittorrent and SABnzbd on StumpCloud. Use whenever the arr queue or activity page is called a mess, stuck, wedged, or full of junk; whenever a show, movie, or album will not download, keeps failing, or never showed up; whenever downloads look stalled, paused, or slow; whenever the media pool fills up, imports fail, or hardlinks look broken; whenever Gluetun or the VPN-routed clients are unreachable; whenever a Jellyseerr or Overseerr request fails or will not submit; and whenever someone asks to clear, purge, triage, or unstick a queue. Use it proactively before blaming indexers, Prowlarr, or the VPN for missing media, because the cause is almost always the download client or a stale path. Use it before changing any client setting in a WebUI, because the converge owns those values and will undo you.
---

# StumpCloud media stack

Sonarr, Radarr and Lidarr feed two download clients that share Gluetun's network namespace: **qBittorrent** and **SABnzbd**. That shared plumbing is the key fact. A problem in one client shows up as a mess in all three apps, and "Sonarr is broken" is almost always "the client stopped moving bytes and Sonarr faithfully queued work behind it."

Order of investigation is always **client first, queue second**. Diagnosing the queue first sends you reading release names for an hour when a single integer in qBittorrent's settings was the answer.

## Never hardcode the host or the pool

Derive them. On the operator Mac, in a `stumpcloud/ansible` checkout:

```bash
ansible-inventory -i dub.yaml --graph media          # which host runs the arr stack
grep -n 'media:' dub.yaml | head                     # all.vars paths.media, plus per-host overrides
```

Dated snapshot, 2026-09-20: the consolidation **happened**. `media` resolves to `ie02`, and ie02 overrides `paths.media` to `/voltron/Media` (`dub.yaml`, ie02 host block) against the fleet default of `/tank/media`. The previous snapshot here said ie01 and `/tank/media` and was wrong for weeks; if this one disagrees with `ansible-inventory`, the inventory wins.

**The stale copy on ie01 is the live hazard.** ie01 still holds exited `gluetun`, `qbittorrent`, `sabnzbd`, `prowlarr`, `sonarr` and `radarr` containers with their old `/tank/media` mounts, and `docker ps -a` on the wrong host shows you a complete, plausible, entirely dead stack. Always confirm you are on the running one:

```bash
for h in ie01 ie02; do echo "== $h"; ssh $h.stump.rocks 'sudo docker ps -a --format "{{.Names}} {{.Status}}"' | grep -E 'sonarr|radarr|gluetun'; done
```

`Exited` on one host and `Up` on the other means you found the museum. Retiring those containers is a repo change, so it is an issue, not a cleanup you do in passing.

Container paths are the single `/data` root everywhere: `/data/TV`, `/data/Movies`, `/data/Music`, `/data/Downloads`. Host paths are `{{ paths.media }}/...`.

**Container path and host path are not interchangeable, and confusing them sends you to the wrong fix.** `/data/TV` is a path inside the *Sonarr* container. Anything that hands Sonarr a root folder — Jellyseerr, a script, you with curl — is passing a string that Sonarr resolves in *its own* namespace. So a caller running on a different host than Sonarr is not a problem to solve, and "move the caller" is not the fix for a rejected path. See the next section.

## Start here, every time

```bash
# operator Mac
python3 scripts/arr_triage.py
python3 scripts/arr_triage.py --media-host ie02.stump.rocks --media-root /voltron/Media
```

Read-only — it never removes, blocklists, resumes, or writes config, so run it before forming any theory. It authenticates, cross-references each app's queue against both clients, and says what is actually broken. `--json` for your own analysis, `--app sonarr` to narrow, `--no-ssh` to skip the host checks.

**It is a starting point, not the investigation.** It answers "is this wedged right now" and is deliberately conservative. If its numbers do not explain what Joe is seeing, keep digging: the history endpoint, link counts on disk, container logs, real byte progress over 30 seconds.

## Reading the result

| Bucket | Meaning | Action |
|---|---|---|
| `dead (zero-seeder)` | queued, zero seeders in the swarm | safe to purge |
| `import-blocked, terminal` | finished, unimportable for a reason that can never resolve | safe to purge |
| `import-blocked, needs a human` | finished, blocked on a judgment call | report, do not purge |
| `paused in SABnzbd` | partially downloaded, sitting paused | resume, do not purge |
| `viable` | has seeders, progressing or legitimately waiting | leave alone |

Counts read as **records / torrents**. Always reason about the torrent count — see the fan-out trap.

## The three wedges

**`max_active_torrents` counts seeding torrents.** It is not a download limit. A box seeding 500 finished torrents with the limit at 5 never starts a download again, and nothing reports an error — torrents sit at `queuedDL` looking like a healthy backlog. That was a three-month outage (OMG 2026-08-19). The paired setting is `dont_count_slow_torrents`: with it on, idle seeds are exempt and a low limit is harmless. Check both, or you cry wolf on a healthy seed box.

**A stalled download never yields its slot.** qBittorrent has no eviction policy for a torrent holding an active slot while transferring nothing, so a handful of zero-peer torrents block the queue permanently. `stalledUP` is *not* this — a finished torrent with no leechers is the resting state of a seed box.

**SABnzbd pauses individual jobs and may never resume them.** `queue.paused` is the global flag; jobs carry their own `status: Paused` in `queue.slots[]`. A queue reports `paused: false, status: Idle` while every job inside it is frozen at 30-50%. With `fulldisk_autoresume` off they stay paused after space is freed — indefinitely.

## The fix belongs in the converge, not the WebUI

`playbooks/services/downloads.yaml` pins the qBittorrent queue and share preferences and the SABnzbd folder paths, then **re-asserts them over the API on every converge** with a drift detector (stumpcloud#286, #319, #320). Guarded by `tests/test_qbittorrent_queue_prefs.py`, `tests/test_sabnzbd_folder_paths.py`, `tests/test_media_single_mount_root.py`.

So a WebUI or `setPreferences` fix to any of those values is undone by the next converge. If you find one wrong, that is **drift**, not a human error — report it as such and land the durable change as a PR against `downloads.yaml` plus the guard test, rather than writing the value back over the API.

The exception worth knowing: `fulldisk_autoresume` appears nowhere in `stumpcloud/ansible` (verified 2026-08-30, `grep -rn fulldisk_autoresume`). It is genuinely unpinned, so set it through the SABnzbd API *and* file an issue to pin it.

## The VPN namespace

Members share Gluetun's netns via `network_mode: container:gluetun`. Membership is per-service `vpn.enabled` plus the `vpn_members` index, asserted equal at deploy (ADR-0038, amended 2026-08-21; `tests/test_gluetun_vpn_member_index.py`).

The stranding trap: Compose resolves `service:`/`container:` to Gluetun's **container ID at creation**, so a Gluetun recreate leaves cross-project members pinned to a dead ID. They keep running and keep reporting `healthy` — the namespace survives while the process does — and fail on next boot. This happened on 2026-08-21 to qbittorrent, nzb and pinchflat during the `lir` recovery (ADR-0038, Phase 2 motivating incident).

The right test is **resolved-ID equality**, not the literal string:

```bash
# on the media host, over ssh as joestump
ssh ie02.stump.rocks 'GL=$(sudo docker inspect -f "{{.Id}}" gluetun); \
  for c in qbittorrent sabnzbd; do \
    echo "$c $(sudo docker inspect -f "{{.HostConfig.NetworkMode}}" $c) want container:$GL"; done'
```

`.claude-ops/checks/verify-gluetun.md` and `verify-vpn.md` still assert the string `container:gluetun` and still list `pinchflat` as a member. Both are wrong today. Fix them at source rather than copying them.

## The slow burns

**Hardlinking silently falling back to copy.** `link()` returns `EXDEV` across two bind mounts even when both resolve to the same filesystem, so an import writes a *second full copy* with `copyUsingHardlinks` still reading true. That shipped 2026-03-20 and ran five months, wasting ~3 TB (stumpcloud#319, #321, OMG 2026-08-22). EXDEV lives in the **container's** mount namespace, so a host-side check reports healthy. The authoritative probe runs in-container:

```bash
# operator Mac, in a stumpcloud/ansible checkout
ansible-playbook -i dub.yaml playbooks/services/arr-hardlinks.yaml
```

The link-count check in `arr_triage.py --structural` is a cheap corroborating signal, not the authority.

**No seeding limit means nothing is ever reclaimed.** An *arr will not remove a torrent that is still seeding. `downloads.yaml` pins `max_ratio_enabled` and `max_seeding_time_enabled` true, so seeing them off is converge drift.

**Dead VPN port forwarding looks exactly like dead torrents.** An empty forwarded port means zero inbound peers, and low-seed torrents sit "stalled with no connections" on swarms that are perfectly alive. Judge viability by `num_complete` (swarm seeders), never `num_seeds` (currently connected). Purging a live-swarm-no-connections torrent throws away a good grab and the replacement stalls identically.

## The request front-end submits a path, and nobody validates it

Jellyseerr (`jellyseerr.stump.rocks`, on ie01) is where the household actually asks for things. It mounts **only** its own config — no media, ever. When someone requests a show it calls Sonarr's API with a `rootFolderPath` string it has stored, and that string lives in `settings.json` inside its config volume as `activeDirectory`, one per configured server.

**That file is runtime state, not Ansible-managed.** `dub.yaml`'s jellyseerr block pins the image, port, volumes, env and labels; it says nothing about root folders. So a host move, a mount change or a consolidation updates the `*arr` and leaves the request front-end pointing at a path that no longer exists, and nothing in the converge notices.

Its failure signature is the reason this section exists:

- Every endpoint is 200 and every container is healthy.
- Sonarr and Radarr are fine, with root folders reporting `accessible: true`.
- Every user request fails, and only the user sees it.

```
[Radarr]: Failed to add movie to Radarr ... "errorCode": "RootFolderExistsValidator",
          "errorMessage": "Root folder '/movies' does not exist"
[Media Request]: Something went wrong sending movie request to Radarr, marking status as FAILED
```

Seen 2026-09-20: the stack moved ie01 → ie02, the mount went from `/media/TV:/tv` to `/voltron/Media:/data`, and Jellyseerr kept submitting `/tv` and `/movies` for **eight days** while the daily sweep reported three green endpoints.

### Check the coupling, not the endpoints

Ask each `*arr` what its root folders actually are, then compare against what the front-end stores. Never read one and assume the other. `accessible: false` is a broken mount, which is a repo change and so an issue; a *mismatch* between the two lists is the front-end's own stale setting, and that one you may correct.

Back up `settings.json`, patch only `activeDirectory`, restart, then verify by **retrying a failed request and reading the log** — re-reading the setting only proves the write landed. Retrying re-drives real acquisitions, so report how many you retried and the failed count afterwards.

Exact calls, including the API-key handling and the retry endpoint: `references/request-paths.md`.

## Two traps that cost an hour

**The queue fans out per episode, not per torrent.** A 25-episode season pack is 25 records sharing one `downloadId`. Removing one removes the torrent, so its 24 siblings vanish — and `DELETE /queue/bulk` 404s the *entire batch* if any id is already gone. By hand during the 2026-08-19 cleanup: 2,505 failures against 205 successes. `arr_purge.py` sends one record per `downloadId` per pass and iterates. Use it.

**Import-blocked items freeze their episodes out of acquisition.** An app will not re-search for something it believes is in flight, and `completed / importBlocked` counts as in flight forever. When Joe names a show that will not download, check the queue for a blocked item mapped onto it before touching indexers or search settings.

## Acting on it

Free to do — reversible, and leaving them undone is the actual harm:

- Resume paused SABnzbd jobs; turn on `fulldisk_autoresume` (then file the pinning issue).
- Un-pause a globally paused client.
- Re-run a search for something whose blocker you just cleared.

Propose with counts, then wait, before:

- **Any bulk removal or blocklisting.** Dry-run `arr_purge.py` and show the numbers per app and per reason.
- **Quality profile changes.** Size problems are usually profile problems, but which quality Joe wants is taste, not a bug.
- **Anything that deletes files on disk** rather than queue entries.
- **Writing any value that `downloads.yaml` pins.** That is a PR, not an API call.

```bash
# operator Mac
python3 scripts/arr_purge.py --app all              # dry run — show Joe this
python3 scripts/arr_purge.py --app sonarr --apply   # after approval
```

Always blocklist, or the app re-grabs the identical dead release on its next RSS pass. Use `skipRedownload=true` for bulk work, or a thousand removals fire a thousand searches at once. Re-run `arr_triage.py` afterward to confirm the buckets emptied and bytes are moving.

## Always check the disk

```bash
ssh ie02.stump.rocks 'df -h /voltron/Media'   # substitute the DERIVED host and root
ssh ie02.stump.rocks 'zfs get -H quota,used,available voltron/Media'
```

Run the second one before declaring the pool full. ADR-0059 records `tank/media` at a 21 TB **quota**, 0 bytes available, over a pool with 8.03 TB still free — a quota wall, not a full pool, and the two have completely different fixes. An unwedged queue starts consuming disk immediately, so fixing a stall can turn a dormant space problem into an active one within the hour.

## Reporting back

Lead with the root cause in one sentence, then a before/after table of the numbers that moved (queue records, torrents, throughput, disk free).

Say **explicitly whether the fix is persisted** — meaning, is it in `downloads.yaml`. "Fixed" and "fixed until the next converge" are different claims.

Anything broken for a long time or that would have stayed silent is StumpCloud infra: propose an incident postmortem rather than footnoting it, and send Joe a note on his usual channel when work lands.

## Details worth loading when you need them

`references/api-notes.md` — credential paths, the API version split between the apps, endpoint shapes, the qBittorrent and SABnzbd auth dances, quality-profile mechanics, and the environment quirks that waste time on this Mac. Read it when a script fails or you need a call the scripts do not cover.
