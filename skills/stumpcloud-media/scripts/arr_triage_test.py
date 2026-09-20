"""
Tests For arr_triage.py

Every test here corresponds to a mistake really made against live infrastructure
during the 2026-08-19 incident, or to a heuristic that was already shipped wrong
once. The suite doubles as the record of what this script exists to get right:

  * `stalledUP` counted as a jam -- a finished torrent with no leechers is the
    normal resting state of a seed box, and flagging it trains you to ignore the
    report entirely.
  * a starvation alarm raised without checking `dont_count_slow_torrents`, which
    fires confidently on a perfectly healthy box that simply seeds a lot.
  * reading only SABnzbd's global `paused` flag, which is how five jobs sat
    individually paused for three weeks while the queue called itself idle.

Everything under test is a pure function over already-fetched data, so this
needs no network, no credentials, and no live *arr instance.

Run with `make test-scripts`, or `pytest skills/stumpcloud-media/scripts`.

@joestump-agent 08/19/2026 - Initial version.
@joestump-agent 08/20/2026 - Moved to pytest and the foo_test.py convention;
split arr_purge coverage into arr_purge_test.py.
@joestump-agent 08/30/2026 - Added the structural-checks placement cases when
the skill moved to stumpcloud/skills and the host stopped being a constant.
"""

import subprocess

import arr_triage
from arr_test_helpers import (
    make_prefs,
    make_record,
    make_sab_queue,
    make_sab_slot,
    make_torrent,
)


# --------------------------------------------------------------- wedge detection
# A wedge is "work is queued and nothing is moving", not "the limits look small".
# Ground truth first; the limits only ever explain WHY.

def test_queued_work_with_no_throughput_is_wedged():
    verdict = arr_triage.assess_qb(
        make_prefs(), [make_torrent("a", "queuedDL")] * 3, {"dl_info_speed": 0})
    assert verdict["wedged"]


def test_queued_work_with_throughput_is_not_wedged():
    verdict = arr_triage.assess_qb(
        make_prefs(),
        [make_torrent("a", "queuedDL"), make_torrent("b", "downloading")],
        {"dl_info_speed": 50_000_000},
    )
    assert not verdict["wedged"]


def test_empty_queue_is_never_wedged():
    """Nothing queued means nothing to be wedged on, even at 0 B/s."""
    verdict = arr_triage.assess_qb(
        make_prefs(), [make_torrent("a", "queuedUP")], {"dl_info_speed": 0})
    assert not verdict["wedged"]


# ---------------------------------------------------------- starvation heuristic
# max_active_torrents counts seeders -- but dont_count_slow_torrents exempts idle
# ones. Checking the limit alone is a false alarm on a healthy seed box, which is
# exactly what the first version of this script did.

def test_seeders_over_limit_with_exemption_off_is_starved():
    torrents = [make_torrent(str(i), "queuedUP") for i in range(10)]
    torrents.append(make_torrent("dl", "queuedDL"))
    verdict = arr_triage.assess_qb(
        make_prefs(max_active_torrents=5, dont_count_slow_torrents=False),
        torrents, {"dl_info_speed": 0})
    assert verdict["starved_by_limit"]


def test_seeders_over_limit_with_exemption_on_is_not_starved():
    """The regression that mattered: 542 seeders against a limit of 150, healthy."""
    torrents = [make_torrent(str(i), "queuedUP") for i in range(10)]
    torrents.append(make_torrent("dl", "queuedDL"))
    verdict = arr_triage.assess_qb(
        make_prefs(max_active_torrents=5, dont_count_slow_torrents=True),
        torrents, {"dl_info_speed": 0})
    assert not verdict["starved_by_limit"]


def test_seeders_under_limit_is_not_starved():
    verdict = arr_triage.assess_qb(
        make_prefs(max_active_torrents=150, dont_count_slow_torrents=False),
        [make_torrent("a", "queuedUP")], {"dl_info_speed": 0})
    assert not verdict["starved_by_limit"]


# ------------------------------------------------------------- stalled detection
# Only stalledDL is a jam. stalledUP is a seed box at rest.

def test_stalled_upload_is_not_reported_as_stalled():
    verdict = arr_triage.assess_qb(
        make_prefs(),
        [make_torrent(str(i), "stalledUP") for i in range(124)],
        {"dl_info_speed": 0},
    )
    assert verdict["stalled_dl"] == 0


def test_stalled_download_is_reported():
    verdict = arr_triage.assess_qb(
        make_prefs(),
        [make_torrent("a", "stalledDL"), make_torrent("b", "stalledUP")],
        {"dl_info_speed": 0},
    )
    assert verdict["stalled_dl"] == 1


# ---------------------------------------------------------- dead vs unreachable
# Viability is judged by swarm seeders (num_complete), never by currently
# connected peers (num_seeds). A live swarm we cannot reach is a connectivity
# problem -- purging it throws away a good grab and the replacement stalls
# identically. Broken VPN port forwarding is what makes this distinction matter.

def test_zero_swarm_seeders_is_dead():
    verdict = arr_triage.assess_qb(
        make_prefs(), [make_torrent("a", "queuedDL", swarm=0)], {"dl_info_speed": 0})
    assert verdict["dead_zero_seeder"] == 1
    assert verdict["dead_hashes"] == ["a"]


def test_live_swarm_with_no_connected_peers_is_not_dead():
    verdict = arr_triage.assess_qb(
        make_prefs(),
        [make_torrent("a", "queuedDL", swarm=4, connected=0)],
        {"dl_info_speed": 0},
    )
    assert verdict["dead_zero_seeder"] == 0


# -------------------------------------------------------------------- seed limits
# With no limit ever satisfiable, an *arr can never reclaim the seed pool --
# it will not remove a torrent that is still seeding.

def test_all_seed_limits_off_is_flagged():
    verdict = arr_triage.assess_qb(
        make_prefs(max_ratio_enabled=False, max_seeding_time_enabled=False,
                   max_inactive_seeding_time_enabled=False),
        [make_torrent("a", "queuedUP")], {"dl_info_speed": 0})
    assert verdict["no_seed_limit"]


def test_any_single_seed_limit_is_enough():
    verdict = arr_triage.assess_qb(
        make_prefs(max_ratio_enabled=False, max_seeding_time_enabled=False,
                   max_inactive_seeding_time_enabled=True),
        [make_torrent("a", "queuedUP")], {"dl_info_speed": 0})
    assert not verdict["no_seed_limit"]


def test_seeding_size_counts_only_completed_torrents():
    verdict = arr_triage.assess_qb(
        make_prefs(),
        [make_torrent("a", "queuedUP", progress=1.0, size=2_000_000_000_000),
         make_torrent("b", "downloading", progress=0.5, size=9_000_000_000_000)],
        {"dl_info_speed": 0},
    )
    assert verdict["seeding_tb"] == 2.0


# ------------------------------------------------------------- SABnzbd pausing
# The global flag lies; the slots tell the truth.

def test_individually_paused_jobs_found_while_queue_reports_healthy():
    queue = make_sab_queue(
        status="Idle", paused=False,
        slots=[make_sab_slot("n1", pct="30", mbleft="1024"),
               make_sab_slot("n2", pct="57", mbleft="2048")],
    )
    verdict = arr_triage.assess_sab(queue, {"fulldisk_autoresume": "0"})
    assert verdict["globally_paused"] is False
    assert verdict["paused_jobs"] == 2
    assert [d["nzo_id"] for d in verdict["paused_detail"]] == ["n1", "n2"]


def test_downloading_jobs_are_not_counted_as_paused():
    queue = make_sab_queue(
        status="Downloading", slots=[make_sab_slot("n1", status="Downloading")])
    verdict = arr_triage.assess_sab(queue, {"fulldisk_autoresume": "1"})
    assert verdict["paused_jobs"] == 0


def test_autoresume_flag_is_surfaced():
    verdict = arr_triage.assess_sab(make_sab_queue(), {"fulldisk_autoresume": "0"})
    assert verdict["fulldisk_autoresume"] == "0"


# ------------------------------------------------------- queue classification
# Buckets decide what is safe to purge, so mis-bucketing deletes real work.

def test_terminal_import_reasons_are_separated_from_ones_needing_a_human():
    records = [
        make_record(1, "AAA", state="importBlocked",
                    messages=["Extras are not supported"]),
        make_record(2, "BBB", state="importFailed",
                    messages=["Has unmatched tracks"]),
    ]
    summary, buckets = arr_triage.classify(records, [], [])
    assert summary["terminal_import"]["records"] == 1
    assert summary["other_import"]["records"] == 1
    assert buckets["other_import"][0]["id"] == 2


def test_download_ids_are_matched_case_insensitively():
    """The apps report uppercase hashes; qBittorrent reports lowercase."""
    summary, _ = arr_triage.classify([make_record(1, "ABCDEF")], ["abcdef"], [])
    assert summary["dead"]["records"] == 1


def test_torrent_count_deduplicates_a_fanned_out_season_pack():
    records = [make_record(i, "SEASONPACK") for i in range(25)]
    summary, _ = arr_triage.classify(records, ["seasonpack"], [])
    assert summary["dead"]["records"] == 25
    assert summary["dead"]["torrents"] == 1


def test_healthy_records_are_viable():
    summary, _ = arr_triage.classify([make_record(1, "AAA")], [], [])
    assert summary["viable"]["records"] == 1


# ------------------------------------------------------- host and pool placement
# The media tier finished its move: the whole arr stack now runs on ie02 at
# /voltron/Media, and ie01 keeps an EXITED copy of every container with its old
# /tank/media mounts. A hardcoded host or pool root does not error when it goes
# stale -- it measures the wrong filesystem and reports it confidently, which is
# exactly how a `df` reading gets quoted at someone as proof the wrong pool is
# full. Here it is worse than usual, because the stale target still exists and
# still answers.


def test_media_defaults_point_at_the_running_stack():
    """The ie01 defaults outlived the move and probed the exited copy.

    These are a dated snapshot by design -- the script documents how to
    re-derive them -- but a snapshot nobody notices going stale is how the
    default invocation ended up reporting on a host that runs nothing. Pin them
    so the update is a deliberate edit here, with the derivation in the diff.
    """
    assert arr_triage.DEFAULT_MEDIA_HOST == "ie02.stump.rocks"
    assert arr_triage.DEFAULT_MEDIA_ROOT == "/voltron/Media"


def test_vault_prefix_did_not_follow_the_services():
    """The credential path stayed behind, and that is not a typo.

    arr-keys.yaml still writes `path: ie01/arr`, so the OpenBao prefix is ie01
    even though the services are on ie02. Anyone 'fixing' this to match the host
    breaks every credential lookup, so the asymmetry is pinned deliberately.
    """
    assert arr_triage.DEFAULT_VAULT_PREFIX == "ie01"

def test_structural_checks_uses_the_host_it_was_given(monkeypatch):
    """A frozen ssh target silently diagnoses a machine nobody asked about."""
    seen = []

    class Result:
        returncode = 0
        stdout = ""

    def record(cmd, **kw):
        seen.append(cmd)
        return Result()

    monkeypatch.setattr(subprocess, "run", record)
    arr_triage.structural_checks("ie02.stump.rocks", "/voltron/Media")
    assert all("ie02.stump.rocks" in cmd for cmd in seen)
    assert not any("ie01.stump.rocks" in c for cmd in seen for c in cmd)


def test_structural_checks_probes_the_pool_root_it_was_given(monkeypatch):
    """The link-count and df probes must follow paths.media, not /tank/media."""
    seen = []

    class Result:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (seen.append(cmd), Result())[1])
    arr_triage.structural_checks("ie02.stump.rocks", "/voltron/Media")
    joined = " ".join(c for cmd in seen for c in cmd)
    assert "/voltron/Media/TV" in joined
    assert "df -BG /voltron/Media" in joined
    assert "/tank/media" not in joined
