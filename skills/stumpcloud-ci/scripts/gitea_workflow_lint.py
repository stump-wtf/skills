#!/usr/bin/env python3
"""
Gitea Workflow Lint

Static, offline analysis of Gitea Actions workflow files for six structural
mistakes that YAML syntax, yamllint, and review all pass. Each rule is an outage
that already happened on this instance, and every one of them presented as green
or skipped rather than as an error -- which is the whole reason a linter beats
prose here. A reader skims "remember these six rules"; a non-zero exit does not.

Nothing here touches the network, a forge, or a credential. The tested surface is
analyze(), a pure function over an already-parsed workflow dict, so the suite
runs with no token, no checkout of any other repo, and no fixtures on disk.
PyYAML is required and is NOT stdlib.

Two parser facts this file owns. PyYAML resolves a bare `on:` key to the boolean
True, so ci.yaml's top-level keys parse as ['name', True, 'jobs'] and every rule
must look the trigger block up under both spellings. And the site inventories
carry `!unsafe` tags, so the optional --inventory path registers a tolerant
constructor before loading or it throws before reading a byte.

@joestump-agent 08/30/2026 - Initial version. Six rules plus the optional
conf.actions.repos cross-check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import namedtuple

import yaml

ERROR = "error"
WARNING = "warning"

Finding = namedtuple("Finding", "level rule where message")

# `github.event_name` and `github.event.*` are the two contexts a workflow_call
# callee does not get. `github.ref`, `github.repository` and `inputs.*` are fine
# there, so they must not match.
EVENT_CONTEXT_RE = re.compile(r"\b(?:github|gitea)\.(?:event_name\b|event\.)")

# The per-run token, under all three spellings it appears in these repos.
RUN_TOKEN_RE = re.compile(
    r"(?:secrets\.GITHUB_TOKEN|secrets\.GITEA_TOKEN|github\.token|gitea\.token)"
)
# A registry OPERATION, not merely the registry hostname. Every action in these
# repos is fetched from `https://gitea.stump.rocks/...`, so matching the host
# would flag every step that passes the per-run token to an ordinary action --
# which is exactly the credential those actions want.
REGISTRY_OP_RE = re.compile(r"docker\s+(?:login|push)\b")
NEED_RESULT_RE = re.compile(r"needs\.([A-Za-z0-9_-]+)\.result")
# Any reference in the job's own `if:` -- `.result` or `.outputs.*` -- really does
# gate, because an upstream failure empties the outputs too. Only a need that
# nothing anywhere reads is the silent hole.
NEED_REF_RE = re.compile(r"needs\.([A-Za-z0-9_-]+)\.")


def trigger_block(doc):
    """Return the `on:` mapping, whichever way PyYAML resolved the key."""
    if not isinstance(doc, dict):
        return {}
    for key in (True, "on", "true"):
        value = doc.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return {name: None for name in value}
        if isinstance(value, str):
            return {value: None}
    return {}


def _text(obj):
    """Flatten any node to searchable text without caring about its shape.

    No sort_keys: a workflow's top-level mapping mixes the boolean True (PyYAML's
    reading of a bare `on:`) with string keys, and sorting them raises TypeError.
    """
    return json.dumps(obj, default=str)


def _jobs(doc):
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    return jobs if isinstance(jobs, dict) else {}


def _steps(job):
    steps = job.get("steps") if isinstance(job, dict) else None
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def _needs(job):
    needs = job.get("needs")
    if isinstance(needs, str):
        return [needs]
    return [n for n in needs if isinstance(n, str)] if isinstance(needs, list) else []


def _check_callee_event_gates(doc, out):
    """Rule 1: an event-based `if:` inside a workflow_call callee is never true."""
    if "workflow_call" not in trigger_block(doc):
        return
    for job_name, job in _jobs(doc).items():
        if not isinstance(job, dict):
            continue
        places = [(f"jobs.{job_name}.if", job.get("if"))]
        for index, step in enumerate(_steps(job)):
            places.append((f"jobs.{job_name}.steps[{index}].if", step.get("if")))
        for where, expr in places:
            if isinstance(expr, str) and EVENT_CONTEXT_RE.search(expr):
                out.append(Finding(
                    ERROR, "callee-event-gate", where,
                    "gates on the triggering event inside a workflow_call callee, where "
                    "event_name is the literal 'workflow_call' and github.event does not "
                    "exist -- this is permanently false and the job or step skips on every "
                    "run. Move the gate to the caller and pass an input down; github.ref "
                    "IS the caller's ref and is safe here.",
                ))


def _check_boolean_inputs(doc, out):
    """Rule 2: this act_runner drops caller-passed boolean workflow_call inputs."""
    call = trigger_block(doc).get("workflow_call")
    inputs = call.get("inputs") if isinstance(call, dict) else None
    if not isinstance(inputs, dict):
        return
    for name, spec in inputs.items():
        if isinstance(spec, dict) and spec.get("type") == "boolean":
            out.append(Finding(
                ERROR, "boolean-input", f"on.workflow_call.inputs.{name}",
                "is type: boolean, and this act_runner drops the caller's value so the "
                "callee always sees the default (ci run 3149). Declare it type: string "
                "and compare == 'true'; never bare truthiness, because the non-empty "
                "string 'false' is truthy.",
            ))


def _is_registry_step(step):
    """True only for a step that actually logs in to or pushes to a registry."""
    uses = step.get("uses")
    if isinstance(uses, str) and "docker/login-action" in uses:
        return True
    with_block = step.get("with")
    if isinstance(with_block, dict) and "registry" in with_block:
        return True
    run = step.get("run")
    return isinstance(run, str) and bool(REGISTRY_OP_RE.search(run))


def _check_registry_auth(doc, out):
    """Rule 3: the per-run token is 401-rejected by the Gitea container registry."""
    for job_name, job in _jobs(doc).items():
        if not isinstance(job, dict):
            continue
        for index, step in enumerate(_steps(job)):
            blob = _text(step)
            where = f"jobs.{job_name}.steps[{index}]"
            if _is_registry_step(step) and RUN_TOKEN_RE.search(blob):
                out.append(Finding(
                    ERROR, "registry-token", where,
                    "authenticates to the Gitea container registry with the per-run token. "
                    "It works against the Gitea API and is rejected 401 by the registry "
                    "(verified on Gitea 1.27.0). Use vars.REGISTRY_USER plus "
                    "secrets.REGISTRY_TOKEN, and give the job packages: write.",
                ))
            if "secrets.REGISTRY_USER" in blob:
                out.append(Finding(
                    ERROR, "registry-user-is-a-variable", where,
                    "reads secrets.REGISTRY_USER, which is provisioned as a repo VARIABLE. "
                    "It resolves to the empty string, which looks like a missing secret and "
                    "tempts a swap to the per-run token. Use vars.REGISTRY_USER.",
                ))


def _check_unasserted_needs(doc, out):
    """Rule 4: under `if: always()`, needs: alone does not gate on anything."""
    for job_name, job in _jobs(doc).items():
        if not isinstance(job, dict):
            continue
        gate = job.get("if")
        if not (isinstance(gate, str) and "always()" in gate):
            continue
        asserted = set(NEED_RESULT_RE.findall(_text(job.get("steps"))))
        asserted |= set(NEED_REF_RE.findall(gate))
        for need in _needs(job):
            if need not in asserted:
                out.append(Finding(
                    ERROR, "unasserted-need", f"jobs.{job_name}.needs",
                    f"lists '{need}' but never reads needs.{need}.result. With if: always() "
                    "the job runs whatever the upstream results were, so this dependency "
                    f"can fail while {job_name} goes green. Add it to BOTH lists.",
                ))


def _check_path_filter(doc, out):
    """Rule 5: a path-filtered workflow posts no status on a non-matching PR."""
    for event, spec in trigger_block(doc).items():
        if not isinstance(spec, dict):
            continue
        for key in ("paths", "paths-ignore"):
            if key in spec:
                out.append(Finding(
                    WARNING, "path-filter", f"on.{event}.{key}",
                    "filters the whole workflow. A PR touching nothing that matches does "
                    "not run it, so NO status is posted -- any required check in this "
                    "workflow then never arrives and the PR wedges forever. Gate the job "
                    "with an if: instead.",
                ))


def _check_action_pins(doc, out):
    """Rule 6: act_runner caches Docker action images with forcePull=false."""
    for job_name, job in _jobs(doc).items():
        if not isinstance(job, dict):
            continue
        for index, step in enumerate(_steps(job)):
            uses = step.get("uses")
            if not isinstance(uses, str):
                continue
            where = f"jobs.{job_name}.steps[{index}].uses"
            if uses.startswith("docker://"):
                tag = uses.rsplit("/", 1)[-1]
                if ":" not in tag or tag.endswith(":latest"):
                    out.append(Finding(
                        ERROR, "mutable-action-image", where,
                        f"'{uses}' resolves a mutable image tag. act_runner pulls a Docker "
                        "action's image with forcePull=false, so a runner holding a cached "
                        "copy never sees a rebuild. Pin an immutable tag and bump it with "
                        "the build.",
                    ))
            elif "@" not in uses and not uses.startswith("./"):
                out.append(Finding(
                    WARNING, "unpinned-action", where,
                    f"'{uses}' names no ref. Pin at least a major tag.",
                ))


def _check_registry_provisioning(doc, out, actions_repos, repo_name):
    """Cross-check: only repos in conf.actions.repos are given REGISTRY_TOKEN."""
    if actions_repos is None or not repo_name:
        return
    if "secrets.REGISTRY_TOKEN" not in _text(doc):
        return
    if repo_name not in actions_repos:
        out.append(Finding(
            ERROR, "registry-token-unprovisioned", "secrets.REGISTRY_TOKEN",
            f"is referenced, but '{repo_name}' is not in all.vars.gitea.actions.repos "
            "(what playbook prose calls conf.actions.repos), which is what provisions "
            "that credential. The secret resolves empty and the push 401s on every run. "
            "Add the repo to the inventory list; do not set a secret by hand.",
        ))


def analyze(doc, name="<workflow>", actions_repos=None, repo_name=None):
    """Every finding for one parsed workflow. Pure: no I/O, no network."""
    out = []
    if not isinstance(doc, dict):
        return [Finding(ERROR, "not-a-workflow", name, "did not parse as a mapping")]
    _check_callee_event_gates(doc, out)
    _check_boolean_inputs(doc, out)
    _check_registry_auth(doc, out)
    _check_unasserted_needs(doc, out)
    _check_path_filter(doc, out)
    _check_action_pins(doc, out)
    _check_registry_provisioning(doc, out, actions_repos, repo_name)
    return out


def load_workflow(path):
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_actions_repos(path):
    """Read the Gitea Actions repo list out of a site inventory.

    The list lives at `all.vars.gitea.actions.repos`. Playbook prose calls it
    `conf.actions.repos` because playbooks/services/gitea.yaml sets
    `conf: "{{ gitea }}"` -- `conf` is the playbook's alias, not an inventory key,
    and looking for a literal `conf:` finds nothing.

    A plain safe_load throws on the `!unsafe` tags these inventories carry, so a
    tolerant constructor is registered first.
    """
    class Tolerant(yaml.SafeLoader):
        pass

    Tolerant.add_multi_constructor(
        "", lambda loader, suffix, node: loader.construct_scalar(node)
        if isinstance(node, yaml.ScalarNode) else None
    )
    with open(path, encoding="utf-8") as handle:
        data = yaml.load(handle, Loader=Tolerant)  # noqa: S506 - Tolerant subclasses SafeLoader
    gitea = ((data or {}).get("all", {}).get("vars", {}) or {}).get("gitea", {}) or {}
    repos = (gitea.get("actions", {}) or {}).get("repos", []) or []
    return {r for r in repos if isinstance(r, str)}


def repo_name_for(path):
    """The repo a workflow belongs to: the directory that contains its .gitea/."""
    current = os.path.dirname(os.path.abspath(path))
    while current != os.path.dirname(current):
        if os.path.basename(current) == ".gitea":
            return os.path.basename(os.path.dirname(current))
        current = os.path.dirname(current)
    return ""


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Lint Gitea Actions workflows for six structural mistakes yamllint cannot "
            "see: an event gate inside a workflow_call callee, a type: boolean "
            "workflow_call input, registry auth with the per-run token, a needs: entry "
            "no step asserts under if: always(), a workflow-level paths: filter, and a "
            "mutable Docker action image tag."
        )
    )
    parser.add_argument("workflows", nargs="+")
    parser.add_argument("--inventory", help="site inventory to read conf.actions.repos from")
    parser.add_argument("--repo", help="repo name for --inventory (default: inferred)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    repos = load_actions_repos(args.inventory) if args.inventory else None

    findings = []
    for path in args.workflows:
        doc = load_workflow(path)
        name = args.repo or repo_name_for(path)
        for finding in analyze(doc, path, repos, name):
            findings.append((path, finding))

    if args.format == "json":
        print(json.dumps(
            [dict(file=p, **f._asdict()) for p, f in findings], indent=2, sort_keys=True
        ))
    else:
        for path, finding in findings:
            print(f"{finding.level}: {path}: {finding.where}: {finding.message}")
        errors = sum(1 for _, f in findings if f.level == ERROR)
        warnings = len(findings) - errors
        print(f"{errors} error(s), {warnings} warning(s) across {len(args.workflows)} file(s)")

    return 1 if any(f.level == ERROR for _, f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
