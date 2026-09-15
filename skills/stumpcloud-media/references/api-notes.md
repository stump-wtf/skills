# Arr stack API notes

Everything the triage scripts rely on, plus the calls they do not cover. Load this when a script fails, when you need an unscripted endpoint, or when the environment is fighting you.

## Credentials

OpenBao at `https://vault.stump.rocks`, KV mount `secret`. The path prefix follows the playbook that writes it, not the host that runs the containers:

Re-derive it with `grep -n 'path:' playbooks/services/arr-keys.yaml` in a `stumpcloud/ansible` checkout. Dated snapshot, 2026-08-30 — `arr-keys.yaml` writes `path: ie01/arr`:

| Path | Fields |
|---|---|
| `secret/ie01/arr` | `sonarr_api_key`, `radarr_api_key`, `lidarr_api_key`, `prowlarr_api_key` |
| `secret/ie01/qbittorrent` | `homepage_password` (the `admin` WebUI password) |
| `secret/ie01/sabnzbd` | `homepage_key` (the API key) |

```bash
# operator Mac. VAULT_ADDR is NOT in the environment; export it yourself.
export VAULT_ADDR=https://vault.stump.rocks
vault kv get -mount=secret -field=sonarr_api_key ie01/arr
```

The `*arr` keys are read out of the running containers by [`playbooks/services/arr-keys.yaml`](https://gitea.stump.rocks/stumpcloud/ansible/src/branch/main/playbooks/services/arr-keys.yaml). If a key stops working, re-run that playbook — it re-reads `config.xml`, so the key was probably rotated inside the app.

**Never print these.** Let the scripts hold them in memory, or fingerprint rather than echo. If one reaches a transcript it is burned: say so and rotate it.

## Environment quirks on the operator Mac

- **`bao` is not OpenBao here.** On this Mac `bao` is the BLAKE3 hashing tool. Use `vault`.
- **`vault` reads `~/.vault-token`,** but only after you export `VAULT_ADDR` yourself. The shell is non-interactive and has no credentials in it. A `VAULT_TOKEN` in the environment, if one appears, may be a read-only session token that shadows the file token: reads work, writes need the file.
- **Short host names do not resolve.** Use `ie01.stump.rocks`, not `ie01`.
- **SSH lands as `joestump`, not root,** so `docker` needs `sudo`: `ssh ie01.stump.rocks 'sudo docker ps'`. (`ansible_user: root` in the inventory is Ansible's per-host connection identity — a different thing.)
- **The Mac is BSD.** `timeout`, `gtimeout`, `gdate` and `gstat` are absent; `date -d`, `stat -c` and `head -n -1` fail. Every GNU-ism in the triage script — `find -printf`, `-newermt`, `df -BG` — runs over ssh on the Linux media host, never locally.

## Reaching the services

Everything is a Caddy vhost on 443. That matters more than it sounds:

- **The `*arr` containers are not published to the host.** `docker ps` on the media host shows `8989/tcp` with no host mapping, so `curl http://127.0.0.1:8989` returns connection-refused. Go through `https://sonarr.stump.rocks`. This is pinned by [`tests/test_arr_path_migration.py`](https://gitea.stump.rocks/stumpcloud/ansible/src/branch/main/tests/test_arr_path_migration.py), which exists because the path-migration playbook was first written against localhost and could never run.
- **qBittorrent and SABnzbd *are* published to the host** — by Gluetun, since they live in its network namespace. That asymmetry is why the two halves of `downloads.yaml` reach their APIs differently.
- `https://qbittorrent.stump.rocks` is the qBittorrent WebUI. `ie01:8080` is a *different* service and answers `API Key Required`.

Because these are vhosts, they survive a host move. The media **host** and **pool root** do not — derive those (`ansible-inventory -i dub.yaml --graph media`, `grep -n 'media:' dub.yaml`) rather than assuming.

## The arr apps

Same API shape, **different versions** — the single most common scripting mistake:

| App | Base URL | API |
|---|---|---|
| Sonarr | `https://sonarr.stump.rocks` | **v3** |
| Radarr | `https://radarr.stump.rocks` | **v3** |
| Lidarr | `https://lidarr.stump.rocks` | **v1** |

Auth is the `X-Api-Key` header on every request.

### Queue

```
GET /api/{ver}/queue?pageSize=2000&page=1
GET /api/v3/queue?pageSize=2000&includeSeries=true&includeEpisode=true   # Sonarr
GET /api/v3/queue?pageSize=2000&includeMovie=true                        # Radarr
```

Default `pageSize` is small — set it explicitly or you silently analyze the first 20 items.

Fields that matter: `id` (the queue record id, what you delete), `downloadId` (the client's torrent hash or SAB `nzo_id` — **the real identity**), `status`, `trackedDownloadStatus`, `trackedDownloadState`, `statusMessages[].messages[]`, `size`, `sizeleft`, `added`, `downloadClient`.

`downloadId` is a torrent hash for qBittorrent items and a `SABnzbd_nzo_*` string for Usenet ones. Anything joining against qBittorrent must lowercase both sides — the apps report uppercase hashes, qBittorrent lowercase.

### Removing from the queue

```
DELETE /api/{ver}/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=true
Body: {"ids": [1, 2, 3]}
```

The endpoint **404s the entire batch if any id is already gone**, which happens constantly because removing one record of a season pack removes all of its siblings. Send one record per `downloadId` per pass and iterate; `scripts/arr_purge.py` does exactly this.

`blocklist=true` stops the app re-grabbing the same release. `skipRedownload=true` suppresses the immediate re-search, which is what you want for bulk work.

Single-item form: `DELETE /api/{ver}/queue/{id}?...`.

### Searching and other useful reads

```
POST /api/v3/command   {"name": "SeasonSearch", "seriesId": 141, "seasonNumber": 2}
POST /api/v3/command   {"name": "MissingEpisodeSearch"}
POST /api/v3/command   {"name": "MoviesSearch", "movieIds": [12]}
GET  /api/v3/command/{id}      # poll: queued -> started -> completed
GET  /api/{ver}/health         # "update available" is normal noise
GET  /api/{ver}/diskspace      # as the container sees it, incl. bind mounts
GET  /api/{ver}/blocklist?pageSize=50&seriesIds=141
GET  /api/v3/episode?seriesId=N          # hasFile/monitored per episode
GET  /api/{ver}/downloadclient           # host/port/user; passwords redacted
GET  /api/v3/rootfolder                  # the /data paths the app has stored
```

Season searches are the right tool after clearing a blocker — they report `Season search completed. N reports downloaded.`, so you can confirm real releases were grabbed rather than assuming.

## qBittorrent

Cookie auth:

```bash
# operator Mac
curl -s -c cookie -d "username=admin&password=$PW" \
  "https://qbittorrent.stump.rocks/api/v2/auth/login"     # returns "Ok."
curl -s -b cookie "https://qbittorrent.stump.rocks/api/v2/torrents/info"
```

| Endpoint | Use |
|---|---|
| `GET /api/v2/torrents/info` | every torrent + state |
| `GET /api/v2/transfer/info` | `dl_info_speed` / `up_info_speed`, bytes/s |
| `GET /api/v2/app/preferences` | the limits |
| `POST /api/v2/app/setPreferences` | `--data-urlencode 'json={...}'` — but see the ownership section |
| `POST /api/v2/torrents/resume` \| `/pause` | body `hashes=<hash>` or `hashes=all` |
| `POST /api/v2/torrents/reannounce` | re-contact trackers for stalled items |

Torrent fields worth knowing:

- `state` — `downloading`, `queuedDL`, `stalledDL`, `queuedUP`, `stalledUP`, `uploading`, `pausedDL`, `metaDL`, `error`
- `num_complete` — **seeders in the swarm.** Zero means the release is dead. Judge viability by this.
- `num_seeds` — seeders *currently connected*. Zero here with `num_complete > 0` is a reachability problem (tracker, VPN, port), not a dead release. Do not purge on it.
- `progress` (0–1), `size`, `completed`, `added_on` (epoch)

Queueing settings and what they actually mean:

| Setting | Gotcha |
|---|---|
| `max_active_torrents` | counts **seeding and downloading together**. The classic wedge. |
| `max_active_downloads` | downloads only |
| `max_active_uploads` | uploads only |
| `dont_count_slow_torrents` | exempts idle torrents from the above. With it on, a low `max_active_torrents` is harmless — check both before declaring a wedge. |
| `slow_torrent_inactive_timer` | seconds before "slow" applies |
| `save_path` | must sit under the one `/data` mount root, or hardlink imports fail with `EXDEV` |

## SABnzbd

`https://nzb.stump.rocks`, API key as a query param. Everything is `GET /api?mode=...&output=json&apikey=$KEY`.

| `mode=` | Use |
|---|---|
| `queue` | jobs + `status`, `paused`, `speed`, `diskspace1`, `diskspacetotal1` |
| `history&limit=20` | completed and failed, with `fail_message` |
| `get_config&section=misc` | the settings below |
| `set_config&section=misc&keyword=K&value=V` | write one setting |
| `queue&name=resume&value=<nzo_id>` | resume one job |
| `queue&name=pause&value=<nzo_id>` | pause one job |
| `pause` / `resume` | the whole queue |

**Per-job pause is invisible at the top level.** `queue.paused` is the *global* flag; individual jobs carry their own `status: "Paused"` in `queue.slots[]`. A queue can report `paused: false, status: "Idle"` while every job in it is paused. Always iterate the slots.

Settings that cause silent stalls:

- `fulldisk_autoresume` — with it off, jobs paused by a full disk never resume, even after space is freed. The highest-value setting here, and the one setting in this doc that the converge does **not** own.
- `download_free` / `complete_free` — the free-space floors that trigger the pause.
- `download_dir` / `complete_dir` — SABnzbd resolves a *relative* value against its own config directory, so `Downloads/incomplete` silently means `/config/Downloads/incomplete` on the container's local disk. Both are pinned absolute under `/data`.

`queue.diskspace1` is free GB on the download volume — a disk reading without SSH.

## Quality profiles

Size problems are usually profile problems.

```
GET /api/v3/qualityprofile          # list
GET /api/v3/qualityprofile/{id}     # one
PUT /api/v3/qualityprofile/{id}     # full object back, returns 202
```

To restrict: fetch the profile, flip `allowed: false` on the qualities you want gone inside `items[]`, PUT the whole object. Some entries are quality *groups* (carrying `name` and nested `items`) rather than single qualities (`quality.name`), so match on `.quality.name // .name`. The expensive ones, largest first: `BR-DISK` (60–100 GB), `Remux-2160p` (40–90 GB), `Bluray-2160p` (15–40 GB), `Remux-1080p` (~20–30 GB). Radarr's stock `Any` profile permits everything including `BR-DISK` — on a finite pool that is how an average movie becomes 57 GB.

Diff the allowed set before and after, so you can prove only what you intended moved:

```bash
# operator Mac
diff <(jq -r '[.items[]|select(.allowed)|(.quality.name//.name)]|.[]' before.json) \
     <(jq -r '[.items[]|select(.allowed)|(.quality.name//.name)]|.[]' after.json)
```

## Who owns which setting

[`playbooks/services/downloads.yaml`](https://gitea.stump.rocks/stumpcloud/ansible/src/branch/main/playbooks/services/downloads.yaml) declares `qbittorrent_queue_prefs`, `qbittorrent_share_prefs` and `sabnzbd_folder_prefs`, detects drift against the live API, and **re-asserts the declared values on every converge**. So a `setPreferences` or `set_config` write to any of those keys is temporary — the next converge reverts it and logs the drift.

Verify which keys are owned rather than trusting this list:

```bash
# operator Mac, in a stumpcloud/ansible checkout
grep -n 'queue_prefs\|share_prefs\|folder_prefs' -A20 playbooks/services/downloads.yaml
python3 -m pytest tests/test_qbittorrent_queue_prefs.py tests/test_sabnzbd_folder_paths.py
```

Anything owned there changes by PR — edit the playbook vars and the matching guard test in `tests/` in the same commit. Anything *not* owned (today: `fulldisk_autoresume`, quality profiles, indexer config) is yours to set through the API, and worth an issue proposing it be pinned.

Background reading, all in `stumpcloud/ansible`: [ADR-0038](https://gitea.stump.rocks/stumpcloud/ansible/src/branch/main/docs/adrs/ADR-0038-inventory-driven-gluetun-vpn-aggregation.md) (Gluetun netns aggregation, amended 2026-08-21), [ADR-0039](https://gitea.stump.rocks/stumpcloud/ansible/src/branch/main/docs/adrs/ADR-0039-media-acquisition-and-streaming-pipeline.md) (`proposed`), [ADR-0059](https://gitea.stump.rocks/stumpcloud/ansible/src/branch/main/docs/adrs/ADR-0059-media-consolidation-onto-voltron.md) (`proposed`). Check the `status:` line before treating any of them as a description of what runs.
