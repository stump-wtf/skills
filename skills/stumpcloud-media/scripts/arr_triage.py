#!/usr/bin/env python3
"""
Arr Backlog Triage — read-only diagnosis
#
# Cross-references the *arr queues (Sonarr/Radarr/Lidarr) against both download
# clients (qBittorrent + SABnzbd) and reports what is actually wrong. Read-only:
# it never removes, blocklists, resumes, or writes config. Nothing here can make
# the situation worse, so it is always safe to run first.
#
# The whole point is that an *arr queue is a SYMPTOM. A thousand stuck items
# almost always means the download client is wedged, not that Sonarr is broken.
# So this reports client health first and the queues second.
#
# The client and *arr URLs are Caddy vhosts and are stable across a host move,
# so they stay constants. The media HOST and POOL ROOT are not: derive them
# with `ansible-inventory -i dub.yaml --graph media` and `grep -n 'media:'
# dub.yaml`, and pass them in. The defaults below are a dated snapshot
# (2026-08-30), not a fact -- ADR-0059 proposes moving this tier to ie02
# /voltron/Media, and pinchflat already went on 2026-08-27.
#
# @joestump-agent 08/19/2026 - Initial version, extracted from the 3-month
# qBittorrent max_active_torrents wedge (OMG 2026-08-19).
#
# @joestump-agent 08/30/2026 - Moved to stumpcloud/skills. Replaced the
# hardcoded ie01.stump.rocks / /tank/media / OpenBao prefix with --media-host,
# --media-root and --vault-prefix, and reworded the no-seed-limit warning as
# converge drift now that downloads.yaml pins those preferences (#286, #320).

Usage:
    python3 arr_triage.py                 # human-readable report
    python3 arr_triage.py --json          # machine-readable, for further jq work
    python3 arr_triage.py --app sonarr    # limit to one app
    python3 arr_triage.py --media-host ie02.stump.rocks --media-root /voltron/Media
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

VAULT_ADDR = os.environ.get("VAULT_ADDR", "https://vault.stump.rocks")

# OpenBao path prefix for this stack's credentials. Written literally as
# `path: ie01/arr` by playbooks/services/arr-keys.yaml, so it follows the
# playbook rather than the host -- re-derive with:
#   grep -n 'path: .*arr' playbooks/services/arr-keys.yaml
DEFAULT_VAULT_PREFIX = "ie01"

# Dated snapshot, 2026-08-30. See the module docstring for the derivation.
DEFAULT_MEDIA_HOST = "ie01.stump.rocks"
DEFAULT_MEDIA_ROOT = "/tank/media"

VAULT_PREFIX = DEFAULT_VAULT_PREFIX

APPS = {
    # name:    (base url,                        api version, queue path)
    "sonarr": ("https://sonarr.stump.rocks", "v3", "queue"),
    "radarr": ("https://radarr.stump.rocks", "v3", "queue"),
    "lidarr": ("https://lidarr.stump.rocks", "v1", "queue"),
}

QB_URL = "https://qbittorrent.stump.rocks"
SAB_URL = "https://nzb.stump.rocks"

# Import-blocked reasons that will NEVER resolve on their own. An item parked on
# one of these is not "in progress" — it is silently blocking re-search for every
# episode/movie it was mapped onto, forever. These are the safe-to-purge class.
TERMINAL_IMPORT_REASONS = (
    "extras are not supported",
    "sample",
    "already imported",
    "no files found are eligible",
    "single episode file contains all episodes",
)


def vault_field(path, field):
    """Read one field from OpenBao. Never prints the value."""
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


def get_json(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- download clients

def qb_session():
    """Log in to qBittorrent, return a cookie header value or None."""
    pw = vault_field(f"{VAULT_PREFIX}/qbittorrent", "homepage_password")
    if not pw:
        return None
    data = urllib.parse.urlencode({"username": "admin", "password": pw}).encode()
    req = urllib.request.Request(f"{QB_URL}/api/v2/auth/login", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.read().decode().strip() != "Ok.":
                return None
            for k, v in r.getheaders():
                if k.lower() == "set-cookie" and "SID=" in v:
                    return v.split(";")[0]
    except Exception:
        return None
    return None


def qb_state(cookie):
    """qBittorrent health: the limits, the transfer rate, and per-torrent state."""
    if not cookie:
        return {"error": "could not authenticate to qBittorrent"}
    h = {"Cookie": cookie}
    return assess_qb(
        get_json(f"{QB_URL}/api/v2/app/preferences", h),
        get_json(f"{QB_URL}/api/v2/torrents/info", h),
        get_json(f"{QB_URL}/api/v2/transfer/info", h),
    )


def assess_qb(prefs, torrents, xfer):
    """
    Pure verdict from qBittorrent data. Separate from fetching so the heuristics
    are testable -- they are subtle enough to have been wrong twice already
    (counting stalledUP as a jam, and flagging a starve without checking
    dont_count_slow_torrents), and both mistakes produce confident false alarms.
    """
    states = {}
    for t in torrents:
        states[t["state"]] = states.get(t["state"], 0) + 1

    seeding = sum(v for k, v in states.items() if k.endswith("UP") or k == "uploading")
    dead = [t for t in torrents if t.get("num_complete", 0) == 0 and t["state"] == "queuedDL"]
    # Only stalledDL matters. A stalledUP torrent is finished and merely has no
    # leechers right now — that is the normal resting state for a seed box, and
    # flagging it trains you to ignore this report.
    jamming = [t for t in torrents if t["state"] == "stalledDL"]
    waiting = states.get("queuedDL", 0)
    moving = states.get("downloading", 0) + states.get("forcedDL", 0) + states.get("metaDL", 0)

    max_torrents = prefs.get("max_active_torrents", -1)
    slow_exempt = bool(prefs.get("dont_count_slow_torrents"))
    return {
        "reachable": True,
        "torrents": len(torrents),
        "states": states,
        "seeding": seeding,
        "dl_mbps": round(xfer.get("dl_info_speed", 0) / 1e6, 1),
        "limits": {
            "max_active_downloads": prefs.get("max_active_downloads"),
            "max_active_uploads": prefs.get("max_active_uploads"),
            "max_active_torrents": max_torrents,
            "dont_count_slow_torrents": prefs.get("dont_count_slow_torrents"),
            "queueing_enabled": prefs.get("queueing_enabled"),
        },
        "dead_zero_seeder": len(dead),
        "dead_hashes": [t["hash"].lower() for t in dead],
        "queued_dl": waiting,
        "moving": moving,
        "stalled_dl": len(jamming),
        "stalled_detail": [
            {"name": t["name"][:60], "pct": round(t["progress"] * 100),
             "size_gb": round(t["size"] / 1e9), "connected_seeds": t.get("num_seeds", 0),
             "swarm_seeds": t.get("num_complete", 0)}
            for t in jamming
        ],
        # Ground truth beats theorising about limits: if work is queued and the
        # wire is silent, it is wedged, whatever the cause. Everything below is
        # only there to explain WHY.
        # Nothing here is ever cleaned up if no seeding limit can be satisfied:
        # an *arr will not remove a torrent that is still seeding, so with all
        # three limits off the seed pool grows without bound forever.
        "seed_limits": {
            "max_ratio_enabled": prefs.get("max_ratio_enabled"),
            "max_seeding_time_enabled": prefs.get("max_seeding_time_enabled"),
            "max_inactive_seeding_time_enabled": prefs.get("max_inactive_seeding_time_enabled"),
        },
        "no_seed_limit": not any((
            prefs.get("max_ratio_enabled"),
            prefs.get("max_seeding_time_enabled"),
            prefs.get("max_inactive_seeding_time_enabled"),
        )),
        "seeding_tb": round(sum(t["size"] for t in torrents if t.get("progress") == 1) / 1e12, 2),
        "listen_port": prefs.get("listen_port"),
        "random_port": prefs.get("random_port"),
        "wedged": waiting > 0 and xfer.get("dl_info_speed", 0) < 100_000,
        # max_active_torrents counts SEEDING torrents against the same budget --
        # unless dont_count_slow_torrents is on, which exempts idle seeds. Both
        # halves matter, so check them together or you will cry wolf on a
        # perfectly healthy box that simply seeds a lot.
        "starved_by_limit": (
            isinstance(max_torrents, int) and max_torrents > 0
            and not slow_exempt and seeding >= max_torrents
        ),
    }


def sab_state():
    """SABnzbd health: global pause, per-job pause, and disk headroom."""
    key = vault_field(f"{VAULT_PREFIX}/sabnzbd", "homepage_key")
    if not key:
        return {"error": "could not read SABnzbd api key"}
    q = get_json(f"{SAB_URL}/api?mode=queue&output=json&apikey={key}")["queue"]
    cfg = get_json(f"{SAB_URL}/api?mode=get_config&section=misc&output=json&apikey={key}")
    return assess_sab(q, cfg.get("config", {}).get("misc", {}))


def assess_sab(q, misc):
    """
    Pure verdict from SABnzbd data. The subtlety worth testing: queue.paused is
    the GLOBAL flag, and a queue can report paused=false / status=Idle while
    every job inside it is individually paused. Reading only the top level is
    how five jobs sat frozen for three weeks unnoticed.
    """
    slots = q.get("slots", [])
    paused_jobs = [s for s in slots if s.get("status") == "Paused"]
    return {
        "reachable": True,
        "status": q.get("status"),
        "globally_paused": q.get("paused"),
        "speed": q.get("speed"),
        "jobs": len(slots),
        "paused_jobs": len(paused_jobs),
        "paused_detail": [
            {"name": s["filename"][:60], "pct": s.get("percentage"),
             "gb_left": round(float(s.get("mbleft", 0)) / 1024, 1), "nzo_id": s.get("nzo_id")}
            for s in paused_jobs
        ],
        "gb_left_total": round(sum(float(s.get("mbleft", 0)) for s in slots) / 1024, 1),
        "disk_free_gb": round(float(q.get("diskspace1", 0))),
        "disk_total_gb": round(float(q.get("diskspacetotal1", 0))),
        # If SAB ever paused on a full disk with this off, it stays paused even
        # after space is freed. That is a silent, indefinite stall.
        "fulldisk_autoresume": misc.get("fulldisk_autoresume"),
    }


# ---------------------------------------------------------------- *arr queues

def structural_checks(host, media_root):
    """
    Slow-burn problems that never show up as a stalled queue.

    These are the ones that make the disk fill and torrents refuse to connect
    no matter how healthy the queue looks, and none of them are visible from
    any API -- they need the host. Best-effort: if SSH is unavailable the rest
    of the report is still valid.
    """
    out = {}
    # Lands as `joestump`, not root, so docker needs sudo. The GNU-isms below
    # (find -printf, -newermt, df -BG) run on the Linux media host, never on
    # the BSD Mac this script is invoked from.
    ssh = ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", host]

    # Hardlink check. The *arrs advertise copyUsingHardlinks, but if the link
    # silently falls back to a copy every import writes a SECOND full copy and
    # the pool fills at twice the expected rate. A hardlinked file has link
    # count >= 2; a copy has 1.
    try:
        r = subprocess.run(
            ssh + [f'sudo find {media_root}/TV -name "*.mkv" -newermt "-7 days" '
                   '-printf "%n\\n" 2>/dev/null | sort | uniq -c'],
            capture_output=True, text=True, timeout=90)
        counts = {}
        for line in r.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) == 2:
                counts[int(parts[1])] = int(parts[0])
        total = sum(counts.values())
        singles = counts.get(1, 0)
        out["hardlinks"] = {
            "recent_imports_checked": total,
            "link_count_1_copies": singles,
            "broken": total > 0 and singles == total,
        }
    except Exception as e:
        out["hardlinks"] = {"error": str(e)}

    # VPN port forwarding. qBittorrent shares gluetun's netns, so a dead
    # forwarded port means zero inbound peers -- which presents as torrents
    # "stalled with no connections" on releases whose swarm is actually alive.
    try:
        r = subprocess.run(
            ssh + ['sudo docker exec gluetun sh -c "cat /tmp/gluetun/forwarded_port 2>/dev/null"'],
            capture_output=True, text=True, timeout=60)
        port = r.stdout.strip()
        out["vpn_forwarded_port"] = {"port": port or None, "working": bool(port)}
    except Exception as e:
        out["vpn_forwarded_port"] = {"error": str(e)}

    try:
        r = subprocess.run(ssh + [f"df -BG {media_root} | tail -1"],
                           capture_output=True, text=True, timeout=60)
        f = r.stdout.split()
        out["disk"] = {"used": f[2], "free": f[3], "pct": f[4]} if len(f) >= 5 else {"raw": r.stdout}
    except Exception as e:
        out["disk"] = {"error": str(e)}

    return out


def arr_queue(app, key):
    base, ver, _ = APPS[app]
    url = f"{base}/api/{ver}/queue?pageSize=2000&page=1"
    return get_json(url, {"X-Api-Key": key}).get("records", [])


def classify(records, dead_hashes, sab_paused_ids):
    """Bucket queue records. Counts are per-record; torrents are deduped."""
    dead = set(dead_hashes)
    sabp = set(sab_paused_ids)
    buckets = {"dead": [], "terminal_import": [], "other_import": [], "sab_paused": [], "viable": []}

    for r in records:
        dlid = (r.get("downloadId") or "").lower()
        state = r.get("trackedDownloadState") or ""
        msgs = " ".join(
            m for sm in (r.get("statusMessages") or []) for m in (sm.get("messages") or [])
        ).lower()

        if dlid in dead:
            buckets["dead"].append(r)
        elif "import" in state:
            if any(t in msgs for t in TERMINAL_IMPORT_REASONS):
                buckets["terminal_import"].append(r)
            else:
                buckets["other_import"].append(r)
        elif r.get("downloadId") in sabp or dlid in sabp:
            buckets["sab_paused"].append(r)
        else:
            buckets["viable"].append(r)

    def uniq(rs):
        return len({(r.get("downloadId") or "").lower() for r in rs})

    return {
        k: {"records": len(v), "torrents": uniq(v),
            "sample": [(r.get("title") or "?")[:60] for r in v[:3]]}
        for k, v in buckets.items()
    }, buckets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--app", choices=list(APPS), help="limit to one app")
    ap.add_argument("--no-ssh", action="store_true",
                    help="skip the host-level structural checks")
    ap.add_argument("--media-host", default=DEFAULT_MEDIA_HOST,
                    help="ssh target running the arr stack; derive with "
                         "`ansible-inventory -i dub.yaml --graph media`")
    ap.add_argument("--media-root", default=DEFAULT_MEDIA_ROOT,
                    help="host path of paths.media on that host; derive with "
                         "`grep -n 'media:' dub.yaml`")
    ap.add_argument("--vault-prefix", default=DEFAULT_VAULT_PREFIX,
                    help="OpenBao path prefix, i.e. secret/<prefix>/arr")
    args = ap.parse_args()

    global VAULT_PREFIX
    VAULT_PREFIX = args.vault_prefix

    report = {"clients": {}, "apps": {}, "structural": {}}
    if not args.no_ssh:
        report["structural"] = structural_checks(args.media_host, args.media_root)

    cookie = qb_session()
    try:
        report["clients"]["qbittorrent"] = qb_state(cookie)
    except Exception as e:
        report["clients"]["qbittorrent"] = {"error": str(e)}
    try:
        report["clients"]["sabnzbd"] = sab_state()
    except Exception as e:
        report["clients"]["sabnzbd"] = {"error": str(e)}

    dead_hashes = report["clients"].get("qbittorrent", {}).get("dead_hashes", []) or []
    sab_paused_ids = [
        d["nzo_id"] for d in report["clients"].get("sabnzbd", {}).get("paused_detail", []) or []
    ]

    wanted = [args.app] if args.app else list(APPS)
    for app in wanted:
        key = vault_field(f"{VAULT_PREFIX}/arr", f"{app}_api_key")
        if not key:
            report["apps"][app] = {"error": f"no api key in OpenBao (secret/{VAULT_PREFIX}/arr)"}
            continue
        try:
            recs = arr_queue(app, key)
            summary, _ = classify(recs, dead_hashes, sab_paused_ids)
            report["apps"][app] = {"queue_records": len(recs), "buckets": summary}
        except Exception as e:
            report["apps"][app] = {"error": str(e)}

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    # ------------------------------------------------------------ human report
    qb = report["clients"].get("qbittorrent", {})
    sab = report["clients"].get("sabnzbd", {})

    print("=" * 68)
    print("DOWNLOAD CLIENTS  (diagnose these first — the *arr queue is a symptom)")
    print("=" * 68)

    if qb.get("error"):
        print(f"qBittorrent: ERROR {qb['error']}")
    else:
        lim = qb["limits"]
        print(f"qBittorrent: {qb['torrents']} torrents, {qb['dl_mbps']} MB/s down "
              f"({qb['moving']} moving, {qb['queued_dl']} queued, {qb['seeding']} seeding)")
        print(f"  limits: downloads={lim['max_active_downloads']} uploads={lim['max_active_uploads']} "
              f"torrents={lim['max_active_torrents']} dont_count_slow={lim['dont_count_slow_torrents']}")
        if qb["wedged"]:
            print(f"  *** WEDGED: {qb['queued_dl']} torrents queued and ~0 MB/s moving. ***")
            if qb["starved_by_limit"]:
                print(f"      Cause: {qb['seeding']} seeding torrents vs max_active_torrents="
                      f"{lim['max_active_torrents']}, and dont_count_slow_torrents is OFF, so"
                      f" seeds consume every slot.")
            elif qb["stalled_dl"]:
                print("      Cause: the active download slots are held by stalled torrents (below).")
            else:
                print("      Cause not obvious from limits — check VPN/gluetun, disk, and trackers.")
        else:
            print("  no wedge: work is queued and bytes are moving (or the queue is empty)")
        if qb["stalled_dl"]:
            print(f"  {qb['stalled_dl']} stalled DOWNLOAD(s) — qBittorrent never evicts these from"
                  f" their slots:")
            for d in qb["stalled_detail"][:6]:
                print(f"      {d['pct']:>3}% {d['size_gb']:>4}G  swarm={d['swarm_seeds']} "
                      f"connected={d['connected_seeds']}  {d['name']}")
        if qb["dead_zero_seeder"]:
            print(f"  {qb['dead_zero_seeder']} queued torrents have ZERO swarm seeders (will never download)")

    print()
    if sab.get("error"):
        print(f"SABnzbd: ERROR {sab['error']}")
    else:
        print(f"SABnzbd: status={sab['status']} globally_paused={sab['globally_paused']} "
              f"speed={sab['speed']} jobs={sab['jobs']}")
        print(f"  disk: {sab['disk_free_gb']} GB free of {sab['disk_total_gb']} GB")
        if sab["paused_jobs"]:
            print(f"  *** {sab['paused_jobs']} individually PAUSED job(s), {sab['gb_left_total']} GB outstanding: ***")
            for d in sab["paused_detail"][:6]:
                print(f"      {d['pct']:>3}%  {d['gb_left']:>6.1f}G left  {d['name']}")
        if str(sab.get("fulldisk_autoresume")).lower() in ("0", "false", "none"):
            print("  *** fulldisk_autoresume is OFF — if SAB ever pauses on a full disk "
                  "it will stay paused forever, even after space is freed. ***")

    print()
    print("=" * 68)
    print("*ARR QUEUES")
    print("=" * 68)
    for app, data in report["apps"].items():
        if data.get("error"):
            print(f"{app}: ERROR {data['error']}")
            continue
        b = data["buckets"]
        print(f"\n{app}: {data['queue_records']} queue records")
        for name, label in (
            ("dead", "dead (zero-seeder)"),
            ("terminal_import", "import-blocked, terminal"),
            ("other_import", "import-blocked, needs a human"),
            ("sab_paused", "paused in SABnzbd"),
            ("viable", "viable"),
        ):
            v = b[name]
            if v["records"]:
                print(f"  {label:<32} {v['records']:>5} records / {v['torrents']:>4} torrents")
                if name in ("terminal_import", "other_import") and v["sample"]:
                    for s in v["sample"]:
                        print(f"       e.g. {s}")

    st = report.get("structural") or {}
    if st:
        print()
        print("=" * 68)
        print("STRUCTURAL  (slow burns — these never show up as a stalled queue)")
        print("=" * 68)

        d = st.get("disk", {})
        if d.get("free"):
            print(f"disk: {d['free']} free, {d['pct']} used on "
                  f"{args.media_root} ({args.media_host})")
            print("  if this reads full, check `zfs get quota,used,available` before "
                  "concluding the pool is out of space -- a dataset quota and a full "
                  "pool have different fixes (ADR-0059).")

        hl = st.get("hardlinks", {})
        if hl.get("broken"):
            print(f"  *** HARDLINKS BROKEN: all {hl['recent_imports_checked']} recent imports have "
                  f"link count 1. Every import writes a SECOND full copy, so the pool fills at "
                  f"twice the expected rate and the staging dir never shrinks. ***")
        elif hl.get("recent_imports_checked"):
            print(f"  hardlinks OK ({hl['recent_imports_checked'] - hl['link_count_1_copies']}"
                  f"/{hl['recent_imports_checked']} recent imports linked)")

        if not qb.get("error"):
            if qb.get("no_seed_limit"):
                print(f"  *** NO SEEDING LIMIT: ratio, seed-time and inactive-seed-time are all "
                      f"off, so the {qb['seeding_tb']} TB seed pool can never be reclaimed — an "
                      f"*arr will not remove a torrent that is still seeding. This is CONVERGE "
                      f"DRIFT: playbooks/services/downloads.yaml pins max_ratio_enabled and "
                      f"max_seeding_time_enabled true and re-asserts them (stumpcloud#320). "
                      f"Re-run the converge; do not write these back through the WebUI. ***")
            else:
                print(f"  seeding limits set ({qb['seeding_tb']} TB seeding)")

        pf = st.get("vpn_forwarded_port", {})
        if pf.get("working") is False:
            print("  *** VPN PORT FORWARDING DEAD: gluetun's forwarded_port is empty, so "
                  "qBittorrent accepts no inbound peers. Low-seed torrents will sit 'stalled "
                  "with no connections' on releases whose swarm is actually alive. ***")
        elif pf.get("port") and not qb.get("error"):
            if str(pf["port"]) != str(qb.get("listen_port")) and not qb.get("random_port"):
                print(f"  *** PORT MISMATCH: gluetun forwards {pf['port']} but qBittorrent "
                      f"listens on {qb.get('listen_port')}. Inbound peers cannot reach it. ***")
            else:
                print(f"  VPN port forwarding OK (port {pf['port']})")

    print()
    print("Note: *arr queues store one record per EPISODE/TRACK, not per torrent.")
    print("Always act on the torrent count, not the record count.")
    print("This script is a starting point, not the whole investigation — if the numbers")
    print("do not explain what the user is seeing, keep digging past it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
