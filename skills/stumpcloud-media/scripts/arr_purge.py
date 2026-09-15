#!/usr/bin/env python3
"""
Arr Backlog Purge — remove + blocklist dead and terminally-blocked queue items
#
# Destructive. Defaults to --dry-run so an accidental invocation reports instead
# of deleting. Pass --apply to actually act, and only after a human has seen the
# counts.
#
# This exists because the naive approach fails in a specific, expensive way. The
# *arr apps store one queue record per EPISODE (or track), so a season pack is
# 25 records sharing a single downloadId. Removing one record removes the whole
# torrent, which makes its 24 siblings vanish -- and the bulk endpoint returns
# 404 for the whole batch if ANY id in it is already gone, taking the other 199
# legitimate removals down with it. Doing this by hand, you watch 2,500 removals
# fail and 205 succeed.
#
# The fix is to send exactly one record per downloadId per pass, then re-fetch
# and repeat until a pass finds nothing. Each pass is internally consistent, so
# nothing 404s.
#
# @joestump-agent 08/19/2026 - Initial version, from the OMG 2026-08-19 cleanup.
#
# @joestump-agent 08/30/2026 - Moved to stumpcloud/skills. The OpenBao path
# prefix became --vault-prefix instead of a literal `ie01`, so the script does
# not carry a frozen host. The *arr and client URLs are Caddy vhosts and are
# stable across a host move, so they stay constants.

Usage:
    python3 arr_purge.py --app sonarr                    # dry run, shows counts
    python3 arr_purge.py --app sonarr --apply            # actually purge
    python3 arr_purge.py --app all --apply --no-blocklist
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

VAULT_ADDR = os.environ.get("VAULT_ADDR", "https://vault.stump.rocks")

# Must match arr_triage.py, or triage promises a purge that this script then
# silently declines to make ("no api key in OpenBao, skipping"). Pinned by
# arr_purge_test.py::test_vault_prefix_matches_across_scripts.
DEFAULT_VAULT_PREFIX = "ie01"

VAULT_PREFIX = DEFAULT_VAULT_PREFIX

APPS = {
    "sonarr": ("https://sonarr.stump.rocks", "v3"),
    "radarr": ("https://radarr.stump.rocks", "v3"),
    "lidarr": ("https://lidarr.stump.rocks", "v1"),
}
QB_URL = "https://qbittorrent.stump.rocks"

TERMINAL_IMPORT_REASONS = (
    "extras are not supported",
    "sample",
    "already imported",
    "no files found are eligible",
    "single episode file contains all episodes",
)


def vault_field(path, field):
    try:
        out = subprocess.run(
            ["vault", "kv", "get", "-mount=secret", f"-field={field}", path],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "VAULT_ADDR": VAULT_ADDR},
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:
        return None


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def qb_dead_hashes():
    """Hashes of torrents that are queued but have zero seeders in the swarm."""
    pw = vault_field(f"{VAULT_PREFIX}/qbittorrent", "homepage_password")
    if not pw:
        return set()
    data = urllib.parse.urlencode({"username": "admin", "password": pw}).encode()
    req = urllib.request.Request(f"{QB_URL}/api/v2/auth/login", data=data)
    cookie = None
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.read().decode().strip() != "Ok.":
            return set()
        for k, v in r.getheaders():
            if k.lower() == "set-cookie" and "SID=" in v:
                cookie = v.split(";")[0]
    if not cookie:
        return set()
    torrents = get_json(f"{QB_URL}/api/v2/torrents/info", {"Cookie": cookie})
    return {
        t["hash"].lower() for t in torrents
        if t.get("num_complete", 0) == 0 and t["state"] in ("queuedDL", "stalledDL")
    }


def selectable(records, dead):
    """One record per downloadId, for records that are safe to purge."""
    picked = {}
    for r in records:
        dlid = (r.get("downloadId") or "").lower()
        if not dlid or dlid in picked:
            continue
        state = r.get("trackedDownloadState") or ""
        msgs = " ".join(
            m for sm in (r.get("statusMessages") or []) for m in (sm.get("messages") or [])
        ).lower()
        reason = None
        if dlid in dead:
            reason = "zero-seeder"
        elif "import" in state and any(t in msgs for t in TERMINAL_IMPORT_REASONS):
            reason = "terminal import block"
        if reason:
            picked[dlid] = (r["id"], (r.get("title") or "?")[:60], reason)
    return picked


def purge(app, key, dead, apply_it, blocklist):
    base, ver = APPS[app]
    total = 0
    for attempt in range(1, 12):
        records = get_json(
            f"{base}/api/{ver}/queue?pageSize=2000&page=1", {"X-Api-Key": key}
        ).get("records", [])
        picked = selectable(records, dead)
        if not picked:
            if attempt == 1:
                print(f"  {app}: nothing to purge ({len(records)} records, all healthy)")
            break

        by_reason = {}
        for _, _, reason in picked.values():
            by_reason[reason] = by_reason.get(reason, 0) + 1
        print(f"  {app} pass {attempt}: {len(picked)} torrents "
              f"({', '.join(f'{v} {k}' for k, v in by_reason.items())}) "
              f"of {len(records)} records")

        if not apply_it:
            for _, (_, title, reason) in list(picked.items())[:5]:
                print(f"      would remove [{reason}] {title}")
            if len(picked) > 5:
                print(f"      ... and {len(picked) - 5} more")
            return len(picked)

        ids = [v[0] for v in picked.values()]
        # Chunk only to keep request bodies sane; every id here is a distinct
        # torrent, so no chunk can collide with another.
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            q = urllib.parse.urlencode({
                "removeFromClient": "true",
                "blocklist": "true" if blocklist else "false",
                "skipRedownload": "true",
            })
            req = urllib.request.Request(
                f"{base}/api/{ver}/queue/bulk?{q}",
                data=json.dumps({"ids": chunk}).encode(),
                headers={"X-Api-Key": key, "Content-Type": "application/json"},
                method="DELETE",
            )
            try:
                urllib.request.urlopen(req, timeout=180).read()
                total += len(chunk)
            except Exception as e:
                print(f"      chunk failed: {e}")
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="all", choices=list(APPS) + ["all"])
    ap.add_argument("--apply", action="store_true",
                    help="actually remove; without this it only reports")
    ap.add_argument("--no-blocklist", action="store_true",
                    help="skip blocklisting (rarely right -- the app will re-grab the same release)")
    ap.add_argument("--vault-prefix", default=DEFAULT_VAULT_PREFIX,
                    help="OpenBao path prefix, i.e. secret/<prefix>/arr")
    args = ap.parse_args()

    global VAULT_PREFIX
    VAULT_PREFIX = args.vault_prefix

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== arr purge [{mode}] ===")
    if args.apply and args.no_blocklist:
        print("WARNING: removing without blocklisting. The *arr app is free to re-grab "
              "these exact releases on its next RSS pass.")

    dead = qb_dead_hashes()
    print(f"qBittorrent reports {len(dead)} zero-seeder torrents\n")

    apps = list(APPS) if args.app == "all" else [args.app]
    grand = 0
    for app in apps:
        key = vault_field(f"{VAULT_PREFIX}/arr", f"{app}_api_key")
        if not key:
            print(f"  {app}: no api key in OpenBao, skipping")
            continue
        grand += purge(app, key, dead, args.apply, not args.no_blocklist)

    print(f"\n{'Removed' if args.apply else 'Would remove'}: {grand} torrents")
    if not args.apply:
        print("Re-run with --apply once a human has approved these counts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
