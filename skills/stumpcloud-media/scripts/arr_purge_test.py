"""
Tests For arr_purge.py

This script deletes things, so its selection logic is the highest-stakes code in
the skill: too greedy and it throws away real downloads, too timid and the queue
never drains. The tests below pin both edges.

The expensive one is `test_one_record_selected_per_download_id`. The *arr apps
store one queue record per EPISODE, so a 25-episode season pack is 25 records
sharing a single downloadId. Removing any one of them removes the whole torrent,
which makes the other 24 vanish -- and the bulk endpoint 404s the ENTIRE batch
if any id in it is already gone. Doing this by hand during the 2026-08-19
incident, 2,505 of 2,710 removals failed that way.

Run with `make test-scripts`, or `pytest skills/stumpcloud-media/scripts`.

@joestump-agent 08/20/2026 - Split out of test_arr_triage.py when the suite
moved to pytest and the foo_test.py convention.
"""

import subprocess

import arr_purge
import arr_triage
from arr_test_helpers import make_record


# ------------------------------------------------------------ selection: dedupe

def test_one_record_selected_per_download_id():
    """25 records, one torrent, one removal -- or the whole batch 404s."""
    records = [make_record(i, "SEASONPACK") for i in range(25)]
    picked = arr_purge.selectable(records, {"seasonpack"})
    assert len(picked) == 1


def test_distinct_torrents_are_all_selected():
    records = [make_record(1, "AAA"), make_record(2, "BBB"), make_record(3, "CCC")]
    picked = arr_purge.selectable(records, {"aaa", "bbb", "ccc"})
    assert len(picked) == 3


def test_download_ids_are_matched_case_insensitively():
    """The apps report uppercase hashes; qBittorrent reports lowercase."""
    picked = arr_purge.selectable([make_record(1, "ABCDEF")], {"abcdef"})
    assert len(picked) == 1


# ------------------------------------------------------- selection: what is safe

def test_viable_records_are_never_selected():
    assert arr_purge.selectable([make_record(1, "AAA")], set()) == {}


def test_import_blocked_needing_a_human_is_never_selected():
    """'Has unmatched tracks' needs a person to map the release, not a purge."""
    records = [make_record(1, "AAA", state="importFailed",
                           messages=["Has unmatched tracks"])]
    assert arr_purge.selectable(records, set()) == {}


def test_terminal_import_block_is_selected_even_with_seeders():
    """It already finished downloading, so the swarm is irrelevant -- it can never import."""
    records = [make_record(1, "AAA", state="importBlocked",
                           messages=["Extras are not supported"])]
    picked = arr_purge.selectable(records, set())
    assert len(picked) == 1
    assert picked["aaa"][2] == "terminal import block"


def test_dead_torrent_is_selected_with_its_reason():
    picked = arr_purge.selectable([make_record(1, "AAA")], {"aaa"})
    assert picked["aaa"][2] == "zero-seeder"


def test_records_without_a_download_id_are_skipped():
    """A record with no downloadId cannot be joined to any client torrent."""
    assert arr_purge.selectable([make_record(1, "")], set()) == {}


# ------------------------------------------------------------ credential handling
# A host without the vault CLI should skip the credential the way arr_triage
# does, not crash the purge with a raw traceback (review fix for PR #13).

def test_vault_field_returns_none_when_binary_is_missing(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("vault")

    monkeypatch.setattr(subprocess, "run", boom)
    assert arr_purge.vault_field("ie01/arr", "sonarr_api_key") is None


def test_vault_field_returns_none_on_nonzero_exit(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: Result())
    assert arr_purge.vault_field("ie01/arr", "sonarr_api_key") is None


def test_vault_field_strips_trailing_newline(monkeypatch):
    """A trailing newline silently breaks header auth and fingerprint compares."""
    class Result:
        returncode = 0
        stdout = "s3cret\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: Result())
    assert arr_purge.vault_field("ie01/arr", "sonarr_api_key") == "s3cret"


# --------------------------------------------------------------- shared contract
# The two scripts must agree on what "terminal" means, or the triage report
# promises a purge that the purge script then declines to make.

def test_terminal_reasons_match_across_scripts():
    assert set(arr_purge.TERMINAL_IMPORT_REASONS) == set(arr_triage.TERMINAL_IMPORT_REASONS)


def test_vault_prefix_matches_across_scripts():
    """A prefix split is silent: triage reads the keys, purge skips every app.

    Both default to the literal written by playbooks/services/arr-keys.yaml
    (`path: ie01/arr`). If that playbook moves the path, both defaults move
    together or the two scripts disagree about which stack they are looking at
    -- and the only symptom is arr_purge printing "no api key in OpenBao,
    skipping" while arr_triage reports a queue full of purgeable records.
    """
    assert arr_purge.DEFAULT_VAULT_PREFIX == arr_triage.DEFAULT_VAULT_PREFIX
