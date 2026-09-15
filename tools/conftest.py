"""
Path Setup For The Repo Linter Tests

pytest imports this automatically. It exists only to put this directory on
sys.path so `import lint_skills` works regardless of where pytest is invoked.
Shared helpers live in lint_test_helpers.py -- see the note there on why.

@joestump-agent 08/20/2026 - Reduced to path setup only.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
