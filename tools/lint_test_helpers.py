"""
Shared Helpers For The Repo Linter Tests

Deliberately NOT named conftest.py -- a helper module imported by two suites
under one pytest run is safer with a globally unique name, and the convention
here is `<prefix>_test_helpers.py` per test module family. The conftest.py
files in this repo do path setup and nothing else.

REPO_ROOT is this repo, so the suites can assert the repo's own skills and
manifests pass the linters they ship.

@joestump-agent 08/20/2026 - Split out of conftest.py in claude-personal.
@joestump-agent 08/30/2026 - Forked into stumpcloud/skills. Added the builders
the new lint_skills checks need: a body of a given length, and a skill with
supporting files.
"""

import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GOOD_SKILL = """---
name: demo
description: A perfectly ordinary skill description.
---

# Demo
"""


def write_skill(root, name, body):
    """Materialise skills/<name>/SKILL.md under a temp root."""
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


def write_file(skill_dir, relpath, body):
    """Materialise a supporting file inside a skill directory."""
    path = skill_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def skill_with_body(lines):
    """A valid skill whose body is exactly `lines` lines of prose."""
    return GOOD_SKILL.replace("# Demo", "\n".join(f"Line {n}." for n in range(lines)))


class TempRepo:
    """A throwaway repo root, so linting a fixture never touches the real tree."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        return Path(self._tmp.name)

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False
