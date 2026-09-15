"""
Sample Workflows For The Gitea Workflow Lint Suite

Builders for the two shapes the rules care about: a healthy composed pipeline
(the caller, which owns the event gate) and a healthy reusable callee. Tests
start from one of these and mutate exactly the field under test, so a passing
negative case proves the rule is discriminating rather than silent.

The module name is globally unique on purpose. Two modules called `helpers` in
different skills collide in sys.modules regardless of how paths are set up, so
the house convention is <prefix>_test_helpers.py.

@joestump-agent 08/30/2026 - Initial version.
"""


def make_caller(**over):
    """A composing pipeline: real events, so an event gate here is correct."""
    doc = {
        "name": "pipeline",
        # Written as the string 'on' rather than the bare key, so a test can
        # switch to True and prove both spellings are found.
        "on": {"push": {"branches": ["main"]}, "pull_request": None},
        "jobs": {
            "secrets": {
                "uses": "https://gitea.stump.rocks/stump.wtf/ci/"
                        ".gitea/workflows/gitleaks.yaml@main",
            },
            "review": {
                "needs": ["secrets"],
                "if": "${{ github.event_name == 'pull_request' }}",
                "uses": "https://gitea.stump.rocks/stump.wtf/ci/"
                        ".gitea/workflows/aibot.yaml@main",
            },
        },
    }
    doc.update(over)
    return doc


def make_callee(**over):
    """A reusable workflow: no event context, so gates must come from inputs."""
    doc = {
        "name": "static-site",
        "on": {
            "workflow_call": {
                "inputs": {
                    "publish": {"type": "string", "required": False, "default": "true"},
                },
            },
        },
        "jobs": {
            "site": {
                "name": "build + publish",
                "runs-on": "ubuntu-latest",
                "steps": [
                    {"uses": "actions/checkout@v4"},
                    {
                        "name": "Deploy to Gitea Pages",
                        "if": "${{ inputs.publish != 'false' "
                              "&& github.ref == 'refs/heads/main' }}",
                        "uses": "https://gitea.stump.rocks/stumpcloud/"
                                "garage-pages-deploy@v2",
                    },
                ],
            },
        },
    }
    doc.update(over)
    return doc


def make_gate(needs, asserts):
    """A fan-in job under `if: always()`, asserting some subset of its needs."""
    script = "\n".join(f"echo '${{{{ needs.{n}.result }}}}'" for n in asserts)
    return {
        "name": "CI",
        "on": {"pull_request": None},
        "jobs": {
            "gate": {
                "name": "all checks passed",
                "needs": list(needs),
                "if": "always()",
                "steps": [{"name": "Require checks to have passed", "run": script}],
            },
        },
    }


def make_step_job(step):
    """One job wrapping one step, for the rules that only look at steps."""
    return {
        "name": "build",
        "on": {"push": {"branches": ["main"]}},
        "jobs": {"image": {"runs-on": "ubuntu-latest", "steps": [step]}},
    }
