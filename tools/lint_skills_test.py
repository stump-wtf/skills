#!/usr/bin/env python3
"""
Tests For lint_skills.py

Each test corresponds to a defect that really shipped, so the suite doubles as
the record of what `make lint` exists to prevent: duplicate frontmatter blocks
and over-long descriptions (claude-personal commit dbfb53f), an unquoted `: `
that makes frontmatter unparseable, and the harness-agnosticism rules this repo
was founded on.

The frontmatter parser is hand-rolled, so `test_matches_pyyaml` re-checks it
against PyYAML on the repo's real skills whenever PyYAML happens to be
installed. It is skipped rather than failed when absent, which keeps the suite
runnable on a bare runner while still catching parser drift locally.

Run with `make test-tools`, or `pytest tools`.

@joestump-agent 08/19/2026 - Initial version in claude-personal.
@joestump-agent 08/30/2026 - Forked into stumpcloud/skills. Added coverage for
the 900-char cap, the quote warning, the Crush name pattern and length, the
180-line body cap, harness-only constructs, cross-skill relative paths, nested
SKILL.md, metadata typing, and the compatibility cap.
"""

import unittest
from pathlib import Path

import lint_skills
from lint_test_helpers import (
    GOOD_SKILL,
    REPO_ROOT,
    TempRepo,
    skill_with_body,
    write_file,
    write_skill,
)

DESC = "A perfectly ordinary skill description."


class FrontmatterParsingTests(unittest.TestCase):
    def parse(self, text):
        block, _ = lint_skills.split_frontmatter(text)
        return lint_skills.parse_frontmatter(block)

    def test_plain_scalar(self):
        self.assertEqual(self.parse(GOOD_SKILL)["name"], "demo")

    def test_folded_block_scalar_joins_lines_with_spaces(self):
        got = self.parse("---\nname: demo\ndescription: >\n  one\n  two\n---\n")
        self.assertEqual(got["description"].strip(), "one two")

    def test_literal_block_scalar_keeps_newlines(self):
        got = self.parse("---\nname: demo\ndescription: |\n  one\n  two\n---\n")
        self.assertEqual(got["description"].strip(), "one\ntwo")

    def test_quoted_scalar(self):
        got = self.parse('---\nname: demo\ndescription: "has: a colon"\n---\n')
        self.assertEqual(got["description"], "has: a colon")

    def test_single_quoted_scalar_unescapes(self):
        got = self.parse("---\nname: demo\ndescription: 'it''s fine'\n---\n")
        self.assertEqual(got["description"], "it's fine")

    def test_flow_sequence(self):
        got = self.parse("---\nname: demo\nallowed-tools: [Read, Bash]\n---\n")
        self.assertEqual(got["allowed-tools"], ["Read", "Bash"])

    def test_nested_mapping(self):
        got = self.parse("---\nname: demo\nmetadata:\n  owner: joe\n  tier: '2'\n---\n")
        self.assertEqual(got["metadata"], {"owner": "joe", "tier": "2"})

    def test_flow_mapping(self):
        got = self.parse("---\nname: demo\nmetadata: {owner: joe}\n---\n")
        self.assertEqual(got["metadata"], {"owner": "joe"})

    def test_unquoted_integer_in_nested_mapping_is_typed_non_string(self):
        got = self.parse("---\nname: demo\nmetadata:\n  tier: 2\n---\n")
        self.assertEqual(got["metadata"]["tier"], lint_skills.NonString("2"))

    def test_bare_colon_space_is_rejected(self):
        # The live bug in message-auto-reply and stumpcloud-monitor: YAML reads
        # this as a nested mapping and refuses the whole block.
        with self.assertRaises(lint_skills.LintError) as ctx:
            self.parse("---\nname: demo\ndescription: Auto-reply: things\n---\n")
        self.assertIn("quotes", str(ctx.exception))

    def test_bare_hash_comment_is_rejected(self):
        # YAML would silently truncate the description at the ` #`.
        with self.assertRaises(lint_skills.LintError):
            self.parse("---\nname: demo\ndescription: tags #todo and more\n---\n")

    def test_duplicate_key_is_rejected(self):
        with self.assertRaises(lint_skills.LintError):
            self.parse("---\nname: demo\nname: other\ndescription: x\n---\n")

    def test_missing_opening_delimiter_is_rejected(self):
        with self.assertRaises(lint_skills.LintError):
            self.parse("# No frontmatter\n")

    def test_unclosed_frontmatter_is_rejected(self):
        with self.assertRaises(lint_skills.LintError):
            self.parse("---\nname: demo\ndescription: x\n")

    def test_tab_indentation_is_rejected(self):
        with self.assertRaises(lint_skills.LintError):
            self.parse("---\nname: demo\n\tdescription: x\n---\n")

    def test_two_level_nesting_is_rejected(self):
        with self.assertRaises(lint_skills.LintError):
            self.parse("---\nname: demo\nmetadata:\n  a:\n    b: c\n---\n")


class LintCase(unittest.TestCase):
    """Lint one skill in a throwaway repo root. Carries no tests of its own."""

    def lint(self, name, body, extra=None):
        with TempRepo() as root:
            skill_dir = write_skill(root, name, body)
            for relpath, content in (extra or {}).items():
                write_file(skill_dir, relpath, content)
            problems, warnings, count = lint_skills.lint_all(root)
            self.assertEqual(count, 1)
            self.last_warnings = warnings
            return problems


class SkillLintTests(LintCase):
    """The checks inherited from claude-personal, plus the empty-tree contract."""

    def test_valid_skill_is_clean(self):
        self.assertEqual(self.lint("demo", GOOD_SKILL), [])

    def test_second_frontmatter_block_is_flagged(self):
        # The navidrome-ldap-sync bug: Crush rejects the skill outright.
        body = GOOD_SKILL.replace("# Demo", "---\nname: demo\nmodel: haiku\n---\n\n# Demo")
        problems = self.lint("demo", body)
        self.assertTrue(any("second frontmatter block" in p for p in problems), problems)

    def test_thematic_break_in_prose_is_not_a_duplicate_block(self):
        body = GOOD_SKILL + "\nSome prose.\n\n---\n\nMore prose with a colon here.\n"
        self.assertEqual(self.lint("demo", body), [])

    def test_name_must_match_directory(self):
        problems = self.lint("demo", GOOD_SKILL.replace("name: demo", "name: mismatch"))
        self.assertTrue(any("but the directory is" in p for p in problems), problems)

    def test_missing_description_is_flagged(self):
        problems = self.lint("demo", "---\nname: demo\n---\n\n# Demo\n")
        self.assertTrue(any("missing required key 'description'" in p for p in problems), problems)

    def test_missing_skill_md_is_flagged(self):
        with TempRepo() as root:
            (root / "skills" / "empty").mkdir(parents=True)
            problems, _, _ = lint_skills.lint_all(root)
            self.assertTrue(any("no SKILL.md" in p for p in problems), problems)

    def test_non_kebab_directory_is_flagged(self):
        problems = self.lint("Demo_Skill", GOOD_SKILL.replace("name: demo", "name: Demo_Skill"))
        self.assertTrue(any("kebab-case" in p for p in problems), problems)

    def test_empty_skills_tree_is_not_an_error(self):
        # The repo is vended before its skills land. An empty skills/ is the
        # contract being present, not a defect.
        with TempRepo() as root:
            (root / "skills").mkdir()
            self.assertEqual(lint_skills.lint_all(root), ([], [], 0))

    def test_absent_skills_directory_is_an_error(self):
        with TempRepo() as root:
            problems, _, _ = lint_skills.lint_all(root)
            self.assertTrue(any("no skills/ directory" in p for p in problems), problems)


class DescriptionTests(LintCase):
    """The 900-char house cap, and the quote warning behind it."""

    def test_over_900_char_description_fails(self):
        long = "x" * (lint_skills.MAX_DESCRIPTION + 1)
        problems = self.lint("demo", GOOD_SKILL.replace(DESC, long))
        self.assertTrue(any("over this repo" in p for p in problems), problems)

    def test_description_at_900_is_allowed(self):
        exact = "x" * lint_skills.MAX_DESCRIPTION
        self.assertEqual(self.lint("demo", GOOD_SKILL.replace(DESC, exact)), [])

    def test_house_cap_is_below_the_crush_hard_cap(self):
        # 900 exists to leave room for Crush's HTML escaping; if someone raises
        # it to the hard cap the margin is gone.
        self.assertLess(lint_skills.MAX_DESCRIPTION, lint_skills.CRUSH_MAX_DESCRIPTION)

    def test_apostrophe_in_description_warns_but_does_not_fail(self):
        body = GOOD_SKILL.replace(DESC, "Joe's ordinary description.")
        self.assertEqual(self.lint("demo", body), [])
        self.assertTrue(any("HTML-escapes" in w for w in self.last_warnings), self.last_warnings)

    def test_double_quote_in_description_warns(self):
        body = GOOD_SKILL.replace("description: " + DESC, "description: 'say \"go\" here'")
        self.assertEqual(self.lint("demo", body), [])
        self.assertTrue(any("HTML-escapes" in w for w in self.last_warnings), self.last_warnings)

    def test_clean_description_produces_no_warning(self):
        self.assertEqual(self.lint("demo", GOOD_SKILL), [])
        self.assertEqual(self.last_warnings, [])


class NameTests(LintCase):
    """Crush's namePattern and MaxNameLength, verified against skills.go."""

    def test_name_over_64_chars_fails(self):
        long = "a" * (lint_skills.MAX_NAME + 1)
        problems = self.lint(long, GOOD_SKILL.replace("name: demo", f"name: {long}"))
        self.assertTrue(any("over the 64-char limit" in p for p in problems), problems)

    def test_name_at_64_chars_is_allowed(self):
        exact = "a" * lint_skills.MAX_NAME
        self.assertEqual(self.lint(exact, GOOD_SKILL.replace("name: demo", f"name: {exact}")), [])

    def test_consecutive_hyphens_fail_the_crush_pattern(self):
        problems = self.lint("de--mo", GOOD_SKILL.replace("name: demo", "name: de--mo"))
        self.assertTrue(any("Crush's pattern" in p for p in problems), problems)

    def test_trailing_hyphen_fails_the_crush_pattern(self):
        problems = self.lint("demo-", GOOD_SKILL.replace("name: demo", "name: demo-"))
        self.assertTrue(any("Crush's pattern" in p for p in problems), problems)


class BodyLengthTests(LintCase):
    def test_body_over_180_lines_fails(self):
        problems = self.lint("demo", skill_with_body(lint_skills.MAX_BODY_LINES + 1))
        self.assertTrue(any("over the 180-line cap" in p for p in problems), problems)

    def test_body_at_180_lines_is_allowed(self):
        self.assertEqual(self.lint("demo", skill_with_body(lint_skills.MAX_BODY_LINES)), [])

    def test_trailing_blank_lines_do_not_count(self):
        body = skill_with_body(lint_skills.MAX_BODY_LINES) + "\n\n\n\n"
        self.assertEqual(self.lint("demo", body), [])


class HarnessConstructTests(LintCase):
    """This repo is harness-agnostic. A Crush session cannot act on these."""

    def check(self, snippet):
        return self.lint("demo", GOOD_SKILL.replace("# Demo", f"# Demo\n\n{snippet}\n"))

    def test_mcp_tool_name_fails(self):
        problems = self.check("Call mcp__gitea__list_issues to read the tracker.")
        self.assertTrue(any("mcp__ MCP tool name" in p for p in problems), problems)

    def test_task_tool_fails(self):
        problems = self.check("Fan the work out with the Task tool.")
        self.assertTrue(any("the Task tool" in p for p in problems), problems)

    def test_skill_tool_fails(self):
        problems = self.check("Load it with the Skill tool first.")
        self.assertTrue(any("the Skill tool" in p for p in problems), problems)

    def test_subagent_fails(self):
        problems = self.check("Delegate the sweep to a subagent.")
        self.assertTrue(any("a subagent" in p for p in problems), problems)

    def test_allow_marker_exempts_the_line(self):
        # The skill that teaches these rules has to be able to name them.
        problems = self.check("Never write mcp__ names. <!-- lint-skills:allow -->")
        self.assertEqual(problems, [])

    def test_a_reference_file_is_checked_too(self):
        problems = self.lint(
            "demo",
            GOOD_SKILL,
            extra={"references/notes.md": "Use the Task tool.\n"},
        )
        self.assertTrue(any("references/notes.md" in p for p in problems), problems)


class CrossSkillPathTests(LintCase):
    """A harness joins a relative path against the WORKING DIRECTORY."""

    def test_sibling_skill_path_fails(self):
        with TempRepo() as root:
            write_skill(root, "other", GOOD_SKILL.replace("name: demo", "name: other"))
            write_skill(
                root,
                "demo",
                GOOD_SKILL.replace("# Demo", "See ../other/references/x.md for the table."),
            )
            problems, _, count = lint_skills.lint_all(root)
            self.assertEqual(count, 2)
            self.assertTrue(any("escapes this skill" in p for p in problems), problems)

    def test_repo_root_references_path_fails(self):
        problems = self.lint("demo", GOOD_SKILL.replace("# Demo", "See ../references/x.md."))
        self.assertTrue(any("escapes this skill" in p for p in problems), problems)

    def test_an_unrelated_relative_path_is_not_flagged(self):
        # ../roles/service names a path in another repo, not a sibling skill.
        self.assertEqual(
            self.lint("demo", GOOD_SKILL.replace("# Demo", "Compare with ../roles/service/.")),
            [],
        )


class StructureTests(LintCase):
    def test_nested_skill_md_fails(self):
        problems = self.lint("demo", GOOD_SKILL, extra={"references/SKILL.md": GOOD_SKILL})
        self.assertTrue(any("nested SKILL.md" in p for p in problems), problems)


class MetadataAndCompatibilityTests(LintCase):
    def test_non_string_metadata_value_fails(self):
        body = GOOD_SKILL.replace("---\n\n# Demo", "metadata:\n  tier: 2\n---\n\n# Demo")
        problems = self.lint("demo", body)
        self.assertTrue(any("non-string" in p for p in problems), problems)

    def test_quoted_metadata_value_is_allowed(self):
        body = GOOD_SKILL.replace("---\n\n# Demo", "metadata:\n  tier: '2'\n---\n\n# Demo")
        self.assertEqual(self.lint("demo", body), [])

    def test_boolean_metadata_value_fails(self):
        body = GOOD_SKILL.replace("---\n\n# Demo", "metadata:\n  live: true\n---\n\n# Demo")
        problems = self.lint("demo", body)
        self.assertTrue(any("non-string" in p for p in problems), problems)

    def test_compatibility_over_500_fails(self):
        long = "y" * (lint_skills.MAX_COMPATIBILITY + 1)
        body = GOOD_SKILL.replace("---\n\n# Demo", f"compatibility: {long}\n---\n\n# Demo")
        problems = self.lint("demo", body)
        self.assertTrue(any("over the 500-char limit" in p for p in problems), problems)

    def test_compatibility_at_500_is_allowed(self):
        exact = "y" * lint_skills.MAX_COMPATIBILITY
        body = GOOD_SKILL.replace("---\n\n# Demo", f"compatibility: {exact}\n---\n\n# Demo")
        self.assertEqual(self.lint("demo", body), [])

    def test_user_invocable_true_warns(self):
        body = GOOD_SKILL.replace("---\n\n# Demo", "user-invocable: true\n---\n\n# Demo")
        self.assertEqual(self.lint("demo", body), [])
        self.assertTrue(any("HTML-escape the ENTIRE body" in w for w in self.last_warnings))


class RealRepoSkillTests(unittest.TestCase):
    """The repo's own skills must pass, and the parser must agree with PyYAML."""

    def test_repo_is_clean(self):
        problems, _, _ = lint_skills.lint_all(REPO_ROOT)
        self.assertEqual(problems, [])

    def test_matches_pyyaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed; stdlib parser is the source of truth")

        paths = sorted((Path(REPO_ROOT) / "skills").glob("*/SKILL.md"))
        if not paths:
            self.skipTest("no skills yet")
        for path in paths:
            with self.subTest(skill=path.parent.name):
                block, _ = lint_skills.split_frontmatter(path.read_text(encoding="utf-8"))
                mine = lint_skills.parse_frontmatter(block)
                theirs = yaml.safe_load("\n".join(block))
                # Only compare keys the hand parser returns as plain strings or
                # lists; typed metadata scalars are deliberately a distinct type.
                for key, value in mine.items():
                    if isinstance(value, (str, list)):
                        got = theirs[key]
                        if isinstance(got, str):
                            got = got.strip()
                        self.assertEqual(
                            value.strip() if isinstance(value, str) else value, got
                        )
