#!/usr/bin/env python3
"""
Tests For lint_manifests.py

Covers the failure this linter exists for: a duplicate JSON key, which every
parser silently discards rather than rejecting (hit in plugin.json, fixed
2026-08-19). A manifest can therefore be perfectly valid JSON and still not mean
what it looks like it means.

Run with `make test-tools`, or `pytest tools`.

@joestump-agent 08/19/2026 - Initial version.
@joestump-agent 08/20/2026 - Split from test_lint.py for the foo_test.py convention.
"""

import json
import unittest

import lint_manifests
from lint_test_helpers import REPO_ROOT, TempRepo


class ManifestLintTests(unittest.TestCase):
    PLUGIN = {"name": "personal", "version": "0.1.0", "description": "A plugin."}
    MARKETPLACE = {
        "name": "claude-personal",
        "owner": {"name": "Joe Stump"},
        "plugins": [{"name": "personal", "source": ".", "description": "Bundle."}],
    }

    def lint(self, plugin=None, marketplace=None, raw_plugin=None):
        with TempRepo() as root:
            (root / ".claude-plugin").mkdir()
            (root / "skills").mkdir()
            text = raw_plugin if raw_plugin is not None else json.dumps(plugin or self.PLUGIN)
            (root / ".claude-plugin" / "plugin.json").write_text(text)
            (root / ".claude-plugin" / "marketplace.json").write_text(
                json.dumps(marketplace or self.MARKETPLACE)
            )
            return lint_manifests.lint(root)

    def test_valid_manifests_are_clean(self):
        self.assertEqual(self.lint(), [])

    def test_duplicate_json_key_is_flagged(self):
        # The plugin.json bug: json.load keeps the last one and drops the rest.
        raw = '{"name": "personal", "version": "0.1.0", "description": "one", "description": "two"}'
        problems = self.lint(raw_plugin=raw)
        self.assertTrue(any("duplicate key 'description'" in p for p in problems), problems)

    def test_invalid_json_is_flagged(self):
        problems = self.lint(raw_plugin='{"name": "personal",}')
        self.assertTrue(any("invalid JSON" in p for p in problems), problems)

    def test_missing_required_key_is_flagged(self):
        problems = self.lint(plugin={"name": "personal", "version": "0.1.0"})
        self.assertTrue(any("missing required key" in p for p in problems), problems)

    def test_name_disagreement_is_flagged(self):
        problems = self.lint(plugin={"name": "renamed", "version": "0.1.0", "description": "A plugin."})
        self.assertTrue(any("but plugin.json declares" in p for p in problems), problems)

    def test_missing_source_directory_is_flagged(self):
        marketplace = dict(self.MARKETPLACE)
        marketplace["plugins"] = [{"name": "personal", "source": "nope", "description": "d"}]
        problems = self.lint(marketplace=marketplace)
        self.assertTrue(any("is not a directory" in p for p in problems), problems)

    def test_missing_file_is_flagged(self):
        with TempRepo() as root:
            (root / ".claude-plugin").mkdir()
            problems = lint_manifests.lint(root)
            self.assertTrue(any("file is missing" in p for p in problems), problems)


class RealRepoManifestTests(unittest.TestCase):
    """The repo's own manifests must pass."""

    def test_repo_manifests_are_clean(self):
        self.assertEqual(lint_manifests.lint(REPO_ROOT), [])
