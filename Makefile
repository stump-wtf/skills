# Build Targets For stump.wtf/skills
#
# The house contract is that `make test`, `make lint` and `make check` work from
# a clean checkout in every repo, whatever the toolchain underneath. Here the
# toolchain is python3 and pytest, nothing else: the linters are stdlib-only on
# purpose so they behave identically on this Mac and on a bare Gitea runner with
# no venv to activate, and CI installs pytest from apt rather than pip for the
# same reason.
#
# CI invokes these same targets (.gitea/workflows/ci.yaml). If CI ever runs
# something else, a green local run stops meaning anything.
#
# SCRIPT_DIRS is globbed, not enumerated. Eleven skills land in this repo in
# parallel and a hand-maintained list would be stale before the first merge; a
# skill that bundles Python is picked up by both `lint-py` and `test-scripts`
# the moment its scripts/ directory exists.
#
# @joestump-agent 08/30/2026 - Initial version.
#
# @joestump 09/14/2026 - Carried verbatim into stump.wtf/skills, the single
# skills repo (coding, operating, review families).

PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest

# Every skill that bundles Python. Empty until the first one lands, which the
# guards below tolerate rather than failing on.
SCRIPT_DIRS := $(wildcard skills/*/scripts)

.DEFAULT_GOAL := check
.PHONY: check test test-tools test-scripts lint lint-skills lint-manifests lint-py help

## check: run everything CI runs
check: lint test

## lint: validate skill frontmatter and bodies, the plugin manifests, and bundled Python
lint: lint-skills lint-manifests lint-py

## lint-skills: every skills/*/SKILL.md parses, fits the caps, and stays harness-agnostic
lint-skills:
	@$(PYTHON) tools/lint_skills.py

## lint-manifests: .claude-plugin/*.json parse, with no silently-dropped duplicate keys
lint-manifests:
	@$(PYTHON) tools/lint_manifests.py

## lint-py: byte-compile bundled Python so a syntax error fails the PR, not the run
lint-py:
	@$(PYTHON) -m compileall -q tools >/dev/null
	@for d in $(SCRIPT_DIRS); do \
		$(PYTHON) -m compileall -q "$$d" >/dev/null || exit 1; \
	done

## test: run every suite in the repo
test: test-tools test-scripts

## test-tools: unit tests for the linters in tools/
test-tools:
	@$(PYTEST) tools

## test-scripts: unit tests for Python bundled inside skills/
test-scripts:
	@if [ -n "$(SCRIPT_DIRS)" ]; then \
		$(PYTEST) $(SCRIPT_DIRS); \
	else \
		echo "no skills/*/scripts yet; nothing to test"; \
	fi

## help: list the targets above
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'
