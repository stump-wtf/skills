"""
Regression Suite For The Gitea Workflow Linter

One test per outage. Each docstring names the incident the rule encodes, so a
future reader can decide whether a rule is still real instead of guessing. The
negative cases matter as much as the positives: a rule that fires on the healthy
caller would be turned off within a week.

Everything runs against dicts built in gitea_workflow_test_helpers.py -- no
network, no credentials, no checkout of any other repo. The two tests that read
a real file live in stumpcloud/ansible and SKIP when it is absent, because a
bare CI container has no checkout of it.

@joestump-agent 08/30/2026 - Initial version.
"""

import os

import pytest
import yaml

from gitea_workflow_lint import (
    ERROR,
    WARNING,
    analyze,
    load_actions_repos,
    repo_name_for,
    trigger_block,
)
from gitea_workflow_test_helpers import make_callee, make_caller, make_gate, make_step_job

ANSIBLE_REPO = os.path.expanduser("~/src/ansible")


def rules(findings):
    return [f.rule for f in findings]


# --- rule 1: the event gate a callee cannot see -----------------------------

def test_callee_event_gate_is_an_error():
    """stump.wtf/ci#7: aibot's gate was event-based inside a callee, so AI review
    had never run once on any stump.wtf repo -- it reported `skipped`, which reads
    as a config choice. Fixed in stump.wtf/ci#8 by gating in the caller."""
    doc = make_callee()
    doc["jobs"]["site"]["if"] = "${{ github.event_name == 'pull_request' }}"
    findings = analyze(doc)
    assert "callee-event-gate" in rules(findings)
    assert findings[0].level == ERROR


def test_callee_event_gate_fires_on_a_step_too():
    """stump.wtf/harness run 3348 and stump.wtf/cairn run 3531: the skipped gate
    was a STEP `if:` inside static-site.yaml, not a job one, and silently
    suppressed two real publishes."""
    doc = make_callee()
    doc["jobs"]["site"]["steps"][1]["if"] = (
        "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}"
    )
    assert "callee-event-gate" in rules(analyze(doc))


def test_callee_ref_and_inputs_gates_are_clean():
    """github.ref, github.repository and inputs.* ARE populated in a callee --
    which is why the static-site fix was to drop the event clause and keep the
    ref clause, not to remove the fence."""
    assert rules(analyze(make_callee())) == []


def test_event_gate_in_a_non_callee_is_clean():
    """The same expression is correct in a caller. stumpcloud/ansible's pages,
    detect and deploy jobs all gate on github.event_name and must not be flagged."""
    assert "callee-event-gate" not in rules(analyze(make_caller()))


def test_event_gate_found_when_pyyaml_resolved_on_to_true():
    """PyYAML resolves a bare `on:` key to the boolean True -- ci.yaml's top-level
    keys parse as ['name', True, 'jobs']. A rule that only looks up 'on' finds
    nothing and silently passes every real file."""
    doc = make_callee()
    doc[True] = doc.pop("on")
    doc["jobs"]["site"]["if"] = "${{ github.event_name == 'push' }}"
    assert "callee-event-gate" in rules(analyze(doc))


# --- rule 2: boolean workflow_call inputs -----------------------------------

def test_boolean_input_is_an_error():
    """ci run 3149: toJSON(inputs).publish_image == false while the caller passed
    true. This act_runner drops caller values for boolean-typed inputs, so the
    callee always sees the default. stump.wtf/ci's aibot.yaml still declares
    is_draft this way; it is tolerable only because the action re-checks draft
    state authoritatively."""
    doc = make_callee()
    doc["on"]["workflow_call"]["inputs"]["publish"]["type"] = "boolean"
    findings = [f for f in analyze(doc) if f.rule == "boolean-input"]
    assert findings and findings[0].level == ERROR


def test_string_input_is_clean():
    """Run 3150: a caller's bare YAML true arrives as the string 'true', so
    string-typed flags are the working shape."""
    assert "boolean-input" not in rules(analyze(make_callee()))


# --- rule 3: registry auth --------------------------------------------------

def test_registry_login_with_the_run_token_is_an_error():
    """The per-run token authenticates against the Gitea API and is rejected 401
    by the Gitea registry (verified on 1.27.0). That mis-diagnosis is what left
    gitea-aibot:v6 unpublished, which in turn made every joestump/dotfiles PR
    unmergeable for two weeks."""
    doc = make_step_job({
        "uses": "docker/login-action@v3",
        "with": {
            "registry": "gitea.stump.rocks",
            "username": "joestump",
            "password": "${{ secrets.GITHUB_TOKEN }}",
        },
    })
    findings = [f for f in analyze(doc) if f.rule == "registry-token"]
    assert findings and findings[0].level == ERROR


def test_registry_user_read_as_a_secret_is_an_error():
    """REGISTRY_USER is provisioned as a repo VARIABLE and REGISTRY_TOKEN as a
    SECRET. secrets.REGISTRY_USER resolves empty, which looks like a missing
    secret and tempts a swap to the per-run token -- the exact hour-burning
    detour recorded against gitea-aibot-action."""
    doc = make_step_job({
        "uses": "docker/login-action@v3",
        "with": {
            "registry": "${{ vars.LOCAL_REGISTRY }}",
            "username": "${{ secrets.REGISTRY_USER }}",
            "password": "${{ secrets.REGISTRY_TOKEN }}",
        },
    })
    assert "registry-user-is-a-variable" in rules(analyze(doc))


def test_correct_registry_pair_is_clean():
    doc = make_step_job({
        "uses": "docker/login-action@v3",
        "with": {
            "registry": "${{ vars.LOCAL_REGISTRY }}",
            "username": "${{ vars.REGISTRY_USER }}",
            "password": "${{ secrets.REGISTRY_TOKEN }}",
        },
    })
    assert rules(analyze(doc)) == []


def test_run_token_outside_a_registry_step_is_clean():
    """The per-run token is the RIGHT credential for the Gitea API. Flagging every
    use of it would make the rule noise."""
    doc = make_step_job({"run": "curl -H \"Authorization: token ${{ github.token }}\" $API"})
    assert "registry-token" not in rules(analyze(doc))


def test_an_action_fetched_from_the_registry_host_is_not_a_registry_step():
    """Caught by running an early draft over the real stump.wtf/ci tree: every
    action in these repos is fetched from https://gitea.stump.rocks/..., and both
    gitleaks-action and gitea-aibot-action are handed the per-run token on
    purpose. Matching the hostname flagged both. The rule keys on a registry
    OPERATION instead."""
    doc = make_step_job({
        "uses": "https://gitea.stump.rocks/stumpcloud/gitleaks-action@v1",
        "with": {"gitea-token": "${{ github.token }}"},
    })
    assert "registry-token" not in rules(analyze(doc))


# --- rule 4: needs that nothing asserts -------------------------------------

def test_unasserted_need_under_always_is_an_error():
    """ci.yaml:391-393 states this rule in prose and nothing enforces it: with
    `if: always()` the gate step runs whatever the upstream results were, so a
    job in needs: alone can fail while the required check goes green. gitleaks
    was folded in on 2026-08-09 and had to be added to BOTH lists."""
    doc = make_gate(needs=["check", "test", "gitleaks"], asserts=["check", "test"])
    findings = [f for f in analyze(doc) if f.rule == "unasserted-need"]
    assert len(findings) == 1
    assert "gitleaks" in findings[0].message


def test_fully_asserted_gate_is_clean():
    """ci.yaml's gate as it stands today asserts all three of its needs."""
    doc = make_gate(needs=["check", "test", "gitleaks"], asserts=["check", "test", "gitleaks"])
    assert rules(analyze(doc)) == []


def test_need_read_through_outputs_in_the_gate_is_clean():
    """converge-apps needs [detect, converge-base] and reads needs.detect.outputs
    in its own `if:`. That gates: if detect fails the output is empty, so the
    comparison is false and the job skips. Flagging it would have been a false
    positive on the shipping pipeline -- which is how this rule was caught."""
    doc = make_gate(needs=["detect", "converge-base"], asserts=[])
    doc["jobs"]["gate"]["if"] = (
        "always() && needs.detect.outputs.has-apps == 'true' "
        "&& needs.converge-base.result != 'failure'"
    )
    assert "unasserted-need" not in rules(analyze(doc))


def test_needs_without_always_is_clean():
    """Without `if: always()` a needs: entry really does gate, so there is nothing
    to assert and the rule must stay quiet."""
    doc = make_gate(needs=["check"], asserts=[])
    doc["jobs"]["gate"].pop("if")
    assert "unasserted-need" not in rules(analyze(doc))


# --- rule 5: workflow-level path filters ------------------------------------

def test_path_filter_is_a_warning():
    """ci.yaml:96-105: a path-filtered workflow does not run on a PR that touches
    nothing matching, so no status is posted and a required check inside it never
    arrives. Warning, not error, because a workflow with no required job may
    legitimately filter."""
    doc = make_caller()
    doc["on"]["pull_request"] = {"paths": ["docs/**"]}
    findings = [f for f in analyze(doc) if f.rule == "path-filter"]
    assert findings and findings[0].level == WARNING


def test_unfiltered_workflow_is_clean():
    assert "path-filter" not in rules(analyze(make_caller()))


# --- rule 6: mutable Docker action images -----------------------------------

@pytest.mark.parametrize("image", [
    "docker://gitea.stump.rocks/stumpcloud/garage-pages-deploy:latest",
    "docker://gitea.stump.rocks/stumpcloud/garage-pages-deploy",
])
def test_mutable_docker_action_image_is_an_error(image):
    """garage-pages-deploy#1: act_runner pulls a Docker action's image with
    forcePull=false, so the slugify fix shipped green and still ran the old
    entrypoint from a cached :latest."""
    doc = make_step_job({"uses": image})
    findings = [f for f in analyze(doc) if f.rule == "mutable-action-image"]
    assert findings and findings[0].level == ERROR


def test_pinned_docker_action_image_is_clean():
    doc = make_step_job({"uses": "docker://ghcr.io/gitleaks/gitleaks:v8.30.1"})
    assert rules(analyze(doc)) == []


def test_action_with_no_ref_is_a_warning():
    doc = make_step_job({"uses": "actions/checkout"})
    findings = [f for f in analyze(doc) if f.rule == "unpinned-action"]
    assert findings and findings[0].level == WARNING


# --- the inventory cross-check ----------------------------------------------

def test_registry_token_without_provisioning_is_an_error():
    """gitea-aibot-action referenced REGISTRY_TOKEN while absent from
    conf.actions.repos, so the secret resolved empty, the push 401'd on every run,
    and :v6 was never published. The durable fix is the inventory list, which is
    why this rule reads it rather than carrying a hardcoded repo list."""
    doc = make_step_job({"run": "echo ${{ secrets.REGISTRY_TOKEN }} | docker login"})
    findings = analyze(doc, actions_repos={"ansible", "docker"}, repo_name="gitea-aibot-action")
    assert "registry-token-unprovisioned" in rules(findings)


def test_provisioned_repo_is_clean_on_the_cross_check():
    doc = make_step_job({"run": "echo ${{ secrets.REGISTRY_TOKEN }} | docker login"})
    findings = analyze(doc, actions_repos={"ansible", "gitea-aibot-action"},
                       repo_name="gitea-aibot-action")
    assert "registry-token-unprovisioned" not in rules(findings)


def test_cross_check_is_inert_without_an_inventory():
    """--inventory is optional, so the default path must not invent findings."""
    doc = make_step_job({"run": "echo ${{ secrets.REGISTRY_TOKEN }} | docker login"})
    assert "registry-token-unprovisioned" not in rules(analyze(doc))


# --- parsing helpers --------------------------------------------------------

def test_trigger_block_handles_every_on_spelling():
    assert "pull_request" in trigger_block({True: {"pull_request": None}})
    assert "pull_request" in trigger_block({"on": ["pull_request"]})
    assert "workflow_call" in trigger_block({"on": "workflow_call"})
    assert trigger_block({"name": "x"}) == {}


def test_repo_name_is_the_directory_holding_dot_gitea():
    assert repo_name_for("/tmp/src/ansible/.gitea/workflows/ci.yaml") == "ansible"
    assert repo_name_for("/tmp/loose.yaml") == ""


def test_analyze_rejects_a_non_mapping():
    assert analyze([], "x.yaml")[0].rule == "not-a-workflow"


# --- the two that read stumpcloud/ansible, and skip when it is absent -------

@pytest.mark.skipif(not os.path.isdir(ANSIBLE_REPO), reason="no stumpcloud/ansible checkout")
def test_inventory_loads_despite_unsafe_tags():
    """dub.yaml carries !unsafe tags, so a naive yaml.safe_load throws before
    reading a byte -- the same swallowed failure that makes ci.yaml's reconcile
    job a no-op on dub and dtw."""
    with pytest.raises(yaml.YAMLError):
        with open(os.path.join(ANSIBLE_REPO, "dub.yaml"), encoding="utf-8") as handle:
            yaml.safe_load(handle)
    repos = load_actions_repos(os.path.join(ANSIBLE_REPO, "dub.yaml"))
    assert "ansible" in repos


@pytest.mark.skipif(not os.path.isdir(ANSIBLE_REPO), reason="no stumpcloud/ansible checkout")
def test_the_real_ci_yaml_reports_no_errors():
    """The shipping pipeline must stay clean, or the linter is crying wolf. This
    also exercises the whole-document scan on a file whose top-level mapping mixes
    the boolean True (PyYAML's reading of a bare `on:`) with string keys -- which
    is where a sorted json.dumps raises TypeError and takes the run with it."""
    path = os.path.join(ANSIBLE_REPO, ".gitea", "workflows", "ci.yaml")
    with open(path, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    repos = load_actions_repos(os.path.join(ANSIBLE_REPO, "dub.yaml"))
    assert [f for f in analyze(doc, path, repos, "ansible") if f.level == ERROR] == []
