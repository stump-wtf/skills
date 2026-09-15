"""
Sample-Data Builders For The Arr Triage Tests

Deliberately NOT named conftest.py. Two conftest modules in one pytest run
collide in sys.modules -- whichever directory is collected first wins, and the
other suite's `import conftest` silently gets the wrong module. A unique module
name per directory is what keeps `pytest` at the repo root working.

The builders take keyword overrides and default to a *healthy* box, so each test
states only the one thing it is actually about.

@joestump-agent 08/20/2026 - Split out of conftest.py after the collision.
"""



def make_torrent(hash_, state, *, swarm=5, size=1_000_000_000, progress=0.5, connected=2):
    """One qBittorrent torrent as its API reports it."""
    return {
        "hash": hash_,
        "state": state,
        "num_complete": swarm,      # seeders in the swarm -- judges viability
        "num_seeds": connected,     # seeders we can actually reach -- does not
        "size": size,
        "progress": progress,
        "name": f"torrent-{hash_}",
    }


def make_prefs(**over):
    """qBittorrent preferences for a healthy box; override the one under test."""
    base = {
        "max_active_downloads": 8,
        "max_active_uploads": 20,
        "max_active_torrents": 150,
        "dont_count_slow_torrents": True,
        "queueing_enabled": True,
        "max_ratio_enabled": True,
        "max_seeding_time_enabled": True,
        "max_inactive_seeding_time_enabled": False,
        "listen_port": 6881,
        "random_port": False,
    }
    base.update(over)
    return base


def make_record(rec_id, download_id, *, state="downloading", messages=(), title="thing"):
    """One *arr queue record. Remember: one per EPISODE, not per torrent."""
    return {
        "id": rec_id,
        "downloadId": download_id,
        "trackedDownloadState": state,
        "title": title,
        "statusMessages": [{"messages": list(messages)}] if messages else [],
    }


def make_sab_queue(*, status="Idle", paused=False, slots=(), free_gb="500"):
    """A SABnzbd queue payload. `paused` is the GLOBAL flag -- slots carry their own."""
    return {
        "status": status,
        "paused": paused,
        "speed": "0",
        "diskspace1": free_gb,
        "diskspacetotal1": "21504",
        "slots": list(slots),
    }


def make_sab_slot(nzo_id, status="Paused", *, pct="30", mbleft="1024", name=None):
    return {
        "status": status,
        "filename": name or f"job-{nzo_id}",
        "percentage": pct,
        "mbleft": mbleft,
        "nzo_id": nzo_id,
    }

