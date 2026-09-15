"""
Import Path Setup For Every Skill Test Suite

pytest imports this automatically at the repo root. It exists only to put each
`skills/<name>/scripts` directory on sys.path, so a suite can `import
arr_triage` without the skill shipping its own conftest.py.

Doing it here rather than eleven times is deliberate. Each skill owns its files
by charter, but path setup is not a fact about a skill -- it is a fact about how
this repo is laid out, and a per-skill copy would be eleven chances to write it
differently. The glob also means adding a skill needs no edit here.

Test modules themselves still need globally unique names. Two modules called
`helpers` in different directories collide in sys.modules regardless of what
this file does, which is why the convention is `<prefix>_test_helpers.py`.

@joestump-agent 08/30/2026 - Initial version.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for scripts in sorted((ROOT / "skills").glob("*/scripts")):
    if scripts.is_dir():
        sys.path.insert(0, str(scripts))
