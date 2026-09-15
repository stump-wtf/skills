#!/usr/bin/env python3
"""
Skill Frontmatter And Body Linter

Validates every `skills/<name>/SKILL.md` against the constraints that actually
break a harness in the wild, plus the house rules this repo adds on top. The
harness constraints are read off Crush's own validator
(`internal/skills/skills.go`), which is stricter than Claude Code: it rejects a
second frontmatter block, a name that does not match its directory, a name over
64 characters or outside `^[a-zA-Z0-9]+(-[a-zA-Z0-9]+)*$`, a description over
1024 characters, and a compatibility string over 500. Every one of those drops
the skill silently -- nobody notices until it never triggers.

The house rules are tighter than the harness where experience says they should
be. Descriptions cap at 900 rather than 1024 because Crush HTML-escapes the
description into every session system prompt (skills.go:31, promptReplacer), so
one apostrophe costs six characters there and a description sized to the hard
cap can cross it. Bodies cap at 180 lines because a body is loaded whole and
anything longer belongs in references/. Claude-Code-only constructs fail
outright because this repo is harness-agnostic by charter and a Crush or Codex
session cannot act on them. Cross-skill relative paths fail because Crush joins
a relative path against the WORKING DIRECTORY, not the skill directory
(crush/internal/agent/tools/view.go:113, SmartJoin) -- so `../sibling/x.md`
resolves wherever the session happens to be sitting.

The YAML here is deliberately parsed by hand rather than with PyYAML. Skill
frontmatter is a narrow shape -- a flat mapping of scalars, plus a one-level
`metadata` mapping -- so a small strict parser covers it, keeps this script
dependency-free on any runner, and makes local and CI runs byte-identical.
Anything it does not understand is a failure rather than a silent pass;
`make lint` is the place to find that out.

Problems fail the build. Warnings print and do not. A line carrying the marker
`lint-skills:allow` is exempt from the body-content checks, which exists for
the one skill whose subject is these rules and which therefore has to name the
constructs it bans.

@joestump-agent 08/19/2026 - Initial version in claude-personal: one-block,
parseable, name matches directory, description present and under the Crush
limit.
@joestump-agent 08/30/2026 - Forked into stumpcloud/skills. Added the house
caps (description 900, body 180 lines), the Crush name pattern and length
check, the harness-construct and cross-skill-path body checks, nested-SKILL.md
detection, `metadata` string-value typing, the `compatibility` cap, and a
warning channel that does not fail the build. An empty skills/ is no longer an
error: the directory is the contract, its contents arrive per skill.
"""

import re
import sys
from pathlib import Path

# Crush's hard cap. Over this the skill is dropped with no diagnostic.
CRUSH_MAX_DESCRIPTION = 1024

# The house cap. Lower than Crush's on purpose -- see the module docstring.
MAX_DESCRIPTION = 900

# Crush: MaxNameLength / MaxCompatibilityLength in internal/skills/skills.go.
MAX_NAME = 64
MAX_COMPATIBILITY = 500

# A body is loaded whole into context. Longer than this goes to references/.
MAX_BODY_LINES = 180

REQUIRED_KEYS = ("name", "description")

# Keys we expect to see at the top of a frontmatter block. Used to tell a
# genuine duplicate block apart from a `---` thematic break in the prose.
FRONTMATTER_KEYS = (
    "name",
    "description",
    "model",
    "allowed-tools",
    "license",
    "compatibility",
    "metadata",
    "user-invocable",
)

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):(?:[ \t]+(.*))?$")

# House convention: lowercase kebab, no leading, trailing or doubled hyphen.
# A strict subset of Crush's namePattern, which also permits uppercase.
DIR_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Crush's namePattern, verbatim (internal/skills/skills.go:30).
CRUSH_NAME_RE = re.compile(r"^[a-zA-Z0-9]+(-[a-zA-Z0-9]+)*$")

BLOCK_HEADER_RE = re.compile(r"^([>|])([+-]?)([0-9]*)$")

# YAML core scalar resolution, enough to tell a string from a non-string.
YAML_INT_RE = re.compile(r"^[-+]?(0|[1-9][0-9_]*)$|^0[xXoObB][0-9a-fA-F_]+$")
YAML_FLOAT_RE = re.compile(r"^[-+]?([0-9][0-9_]*)?\.[0-9_]*([eE][-+]?[0-9]+)?$")
YAML_BOOL = {"true", "false", "yes", "no", "on", "off"}
YAML_NULL = {"null", "~", ""}

# Constructs only Claude Code understands. A Crush or Codex session reading
# these is being told to use something it does not have.
HARNESS_CONSTRUCTS = (
    ("mcp__", "an mcp__ MCP tool name"),
    ("task tool", "the Task tool"),
    ("skill tool", "the Skill tool"),
    ("subagent", "a subagent"),
)

# Directory names that must never appear after `../`: a sibling skill (each
# skill owns its own files) or a repo-root shared directory (only paths under a
# configured skills path get read without a permission prompt).
FORBIDDEN_PARENT_TARGETS = frozenset({"references", "scripts", "tools", "skills"})

REL_PARENT_RE = re.compile(r"\.\./([A-Za-z0-9][A-Za-z0-9._-]*)")

# Opt out of the body-content checks on one line. For the skill that documents
# these rules and therefore has to spell the banned constructs out.
ALLOW_MARKER = "lint-skills:allow"


class LintError(Exception):
    """A frontmatter problem worth failing the build over."""


def strip_plain_comment(text):
    """Drop a trailing ` # comment` the way a YAML plain scalar does."""
    idx = text.find(" #")
    return text[:idx] if idx != -1 else text


def chomp(text, indicator):
    """Apply YAML block-scalar chomping: clip (default), strip (-), keep (+)."""
    if indicator == "+":
        return text
    text = text.rstrip("\n")
    if indicator == "-":
        return text
    return text + "\n" if text else text


def scalar_is_string(raw):
    """True when an UNQUOTED plain scalar resolves to a YAML string.

    `metadata` is typed `map[string]string` in Crush, so `count: 5` fails to
    unmarshal and takes the whole skill with it. Quoted scalars never reach
    here -- the parser marks those as strings by construction.
    """
    text = raw.strip()
    low = text.lower()
    if low in YAML_NULL or low in YAML_BOOL:
        return False
    if YAML_INT_RE.match(text) or YAML_FLOAT_RE.match(text):
        return False
    return True


def parse_block_scalar(lines, start, style, indicator):
    """Consume an indented block scalar starting at `start`. Returns (value, next)."""
    body, i = [], start
    while i < len(lines):
        line = lines[i]
        if line.strip() and not line[:1].isspace():
            break
        body.append(line)
        i += 1

    while body and not body[-1].strip():
        body.pop()
    if not body:
        return "", i

    indent = min(len(ln) - len(ln.lstrip()) for ln in body if ln.strip())
    stripped = [ln[indent:] if ln.strip() else "" for ln in body]

    if style == "|":
        return chomp("\n".join(stripped) + "\n", indicator), i

    # Folded: blank lines become newlines, everything else joins with a space.
    out, pending_newlines = "", 0
    for line in stripped:
        if not line:
            pending_newlines += 1
            continue
        if not out:
            out = line
        elif pending_newlines:
            out += "\n" * pending_newlines + line
        else:
            out += " " + line
        pending_newlines = 0
    return chomp(out + "\n" * (pending_newlines + 1), indicator), i


def parse_quoted(lines, start, first, quote):
    """Consume a quoted scalar that may wrap across lines. Returns (value, next)."""
    buf, i = [first], start
    while True:
        candidate = "\n".join(buf)
        # A closing quote is one that is not itself escaped/doubled.
        body = candidate[1:]
        if quote == '"':
            closed = re.search(r'(?<!\\)"$', body) is not None
        else:
            closed = body.endswith("'") and not body.endswith("''")
        if closed:
            break
        i += 1
        if i >= len(lines):
            raise LintError("unterminated quoted string in frontmatter")
        buf.append(lines[i].strip())

    raw = "\n".join(buf)
    inner = raw[1:-1]
    inner = re.sub(r"\s*\n\s*", " ", inner)
    if quote == "'":
        return inner.replace("''", "'"), i + 1
    for esc, char in (("\\n", "\n"), ("\\t", "\t"), ('\\"', '"'), ("\\\\", "\\")):
        inner = inner.replace(esc, char)
    return inner, i + 1


# A plain (unquoted) scalar may not contain ": " -- YAML reads it as a nested
# mapping and the whole block fails to parse. " #" starts a comment there, which
# silently truncates the value instead. Both mean "you needed quotes here".
PLAIN_TRAPS = ((": ", 'a bare ": "'), (" #", 'a bare " #"'))


def check_plain(key, text):
    for needle, label in PLAIN_TRAPS:
        if needle in text:
            raise LintError(
                f"unquoted value for {key!r} contains {label} -- wrap it in quotes "
                "or use a `>` block scalar (YAML cannot parse it as written)"
            )


def parse_plain(lines, start, first, key):
    """Consume a plain scalar plus any more-indented continuation lines."""
    check_plain(key, first)
    parts, i = [strip_plain_comment(first).strip()], start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            nxt = i + 1
            if nxt < len(lines) and line[:1] == "" and lines[nxt][:1].isspace():
                parts.append("")
                i = nxt
                continue
            break
        if not line[:1].isspace():
            break
        check_plain(key, line)
        parts.append(strip_plain_comment(line).strip())
        i += 1

    out = ""
    for part in parts:
        if not part:
            out += "\n"
        elif not out or out.endswith("\n"):
            out += part
        else:
            out += " " + part
    return out, i


def parse_flow_mapping(key, rest):
    """Parse `{a: b, c: d}` on one line into a dict of typed values."""
    if not rest.endswith("}"):
        raise LintError(f"multi-line flow mapping on {key!r} is not supported")
    out = {}
    inner = rest[1:-1].strip()
    if not inner:
        return out
    for item in inner.split(","):
        if ":" not in item:
            raise LintError(f"flow mapping entry {item.strip()!r} on {key!r} has no value")
        sub, val = item.split(":", 1)
        sub, val = sub.strip().strip("\"'"), val.strip()
        if not sub:
            raise LintError(f"flow mapping on {key!r} has an empty key")
        if sub in out:
            raise LintError(f"duplicate key {sub!r} in {key!r}")
        if val[:1] in ("'", '"'):
            out[sub] = val[1:-1]
        else:
            out[sub] = val if scalar_is_string(val) else NonString(val)
    return out


class NonString:
    """A scalar YAML resolves to something other than a string.

    Kept as a distinct type rather than coerced so `metadata` typing can be
    reported precisely: Crush unmarshals metadata as map[string]string, and an
    int or bool there fails the whole file.
    """

    def __init__(self, raw):
        self.raw = raw

    def __repr__(self):
        return f"NonString({self.raw!r})"

    def __eq__(self, other):
        return isinstance(other, NonString) and other.raw == self.raw


def parse_nested_mapping(lines, start, key):
    """Consume an indented one-level mapping. Returns (dict, next index)."""
    out, i, indent = {}, start, None
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if not line[:1].isspace():
            break
        width = len(line) - len(line.lstrip())
        if indent is None:
            indent = width
        elif width != indent:
            raise LintError(f"inconsistent indentation under {key!r} at line {i + 1}")
        match = KEY_RE.match(line.strip())
        if not match:
            raise LintError(f"cannot parse line {i + 1} under {key!r}: {line!r}")
        sub, val = match.group(1), (match.group(2) or "").strip()
        if sub in out:
            raise LintError(f"duplicate key {sub!r} in {key!r}")
        if not val:
            raise LintError(f"{key}.{sub} has no value (only one level of nesting is supported)")
        if val[0] in ("'", '"'):
            out[sub] = val[1:-1]
        else:
            out[sub] = val if scalar_is_string(val) else NonString(val)
        i += 1
    if indent is None:
        raise LintError(f"key {key!r} has no value")
    return out, i


def parse_frontmatter(lines):
    """Parse a flat YAML mapping. Raises LintError on anything unsupported."""
    data, i = {}, 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[:1].isspace():
            raise LintError(f"unexpected indentation at frontmatter line {i + 1}")
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise LintError(f"tab indentation at frontmatter line {i + 1}")

        match = KEY_RE.match(line)
        if not match:
            raise LintError(f"cannot parse frontmatter line {i + 1}: {line!r}")
        key, rest = match.group(1), (match.group(2) or "").strip()
        if key in data:
            raise LintError(f"duplicate key {key!r} in frontmatter")

        header = BLOCK_HEADER_RE.match(rest)
        if header:
            if header.group(3):
                raise LintError(f"explicit block indent on {key!r} is not supported")
            value, i = parse_block_scalar(lines, i + 1, header.group(1), header.group(2))
        elif rest.startswith(('"', "'")):
            value, i = parse_quoted(lines, i, rest, rest[0])
        elif rest.startswith("["):
            if not rest.endswith("]"):
                raise LintError(f"multi-line flow sequence on {key!r} is not supported")
            items = [it.strip().strip("\"'") for it in rest[1:-1].split(",")]
            value, i = [it for it in items if it], i + 1
        elif rest.startswith("{"):
            value, i = parse_flow_mapping(key, rest), i + 1
        elif not rest:
            value, i = parse_nested_mapping(lines, i + 1, key)
        else:
            if rest[0] in "&*!%@`":
                raise LintError(f"value for {key!r} starts with the YAML indicator {rest[0]!r}")
            value, i = parse_plain(lines, i + 1, rest, key)

        data[key] = value
    return data


def split_frontmatter(text, path=None):
    """Return (frontmatter lines, index of the first body line)."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        raise LintError("file does not open with a `---` frontmatter delimiter")
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == "---":
            return lines[1:idx], idx + 1
    raise LintError("frontmatter block is never closed with `---`")


def find_duplicate_block(lines, start):
    """Locate a second frontmatter block, the failure mode that hid skills in Crush."""
    for idx in range(start, len(lines)):
        if lines[idx].rstrip() != "---":
            continue
        for peek in range(idx + 1, len(lines)):
            if not lines[peek].strip():
                continue
            key = KEY_RE.match(lines[peek])
            if key and key.group(1) in FRONTMATTER_KEYS:
                return idx + 1
            break
    return None


def body_line_count(lines, start):
    """Body length, blank lines at either end excluded."""
    body = lines[start:]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return len(body)


def check_markdown_content(lines, where, siblings):
    """Harness-only constructs and cross-skill paths in one markdown file."""
    problems = []
    for num, line in enumerate(lines, start=1):
        if ALLOW_MARKER in line:
            continue
        low = line.lower()
        for needle, label in HARNESS_CONSTRUCTS:
            if needle in low:
                problems.append(
                    f"{where}:{num}: names {label}, which only Claude Code provides "
                    "(this repo is harness-agnostic; describe the capability instead)"
                )
        for target in REL_PARENT_RE.findall(line):
            if target in siblings or target in FORBIDDEN_PARENT_TARGETS:
                problems.append(
                    f"{where}:{num}: relative path '../{target}' escapes this skill "
                    "(a harness resolves it against the working directory, not the "
                    "skill directory -- each skill owns its own files)"
                )
    return problems


def lint_skill(skill_dir, root, siblings=frozenset()):
    """Lint one skill directory. Returns (problems, warnings)."""
    problems, warnings = [], []
    name = skill_dir.name
    rel = skill_dir.relative_to(root)

    if not DIR_NAME_RE.match(name):
        problems.append(f"{rel}: directory name is not lowercase-kebab-case")

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        problems.append(f"{rel}: no SKILL.md")
        return problems, warnings

    for nested in sorted(skill_dir.rglob("SKILL.md")):
        if nested.parent != skill_dir:
            problems.append(
                f"{nested.relative_to(root)}: nested SKILL.md -- a harness treats every "
                "SKILL.md as its own skill, so this one loads under the wrong name"
            )

    rel_md = skill_md.relative_to(root)
    text = skill_md.read_text(encoding="utf-8")
    all_lines = text.split("\n")

    # Run before the frontmatter parse, not after: an unparseable block would
    # otherwise mask every content problem in the file behind one YAML error,
    # and the author fixes them one round-trip at a time.
    for md in [skill_md] + sorted(p for p in skill_dir.rglob("*.md") if p != skill_md):
        content = md.read_text(encoding="utf-8").split("\n")
        problems.extend(
            check_markdown_content(content, str(md.relative_to(root)), siblings)
        )

    try:
        block, after = split_frontmatter(text)
        data = parse_frontmatter(block)
    except LintError as exc:
        problems.append(f"{rel_md}: {exc}")
        return problems, warnings

    dup = find_duplicate_block(all_lines, after)
    if dup is not None:
        problems.append(
            f"{rel_md}:{dup}: second frontmatter block "
            "(Crush rejects the whole skill; keep exactly one)"
        )

    for key in REQUIRED_KEYS:
        if key not in data:
            problems.append(f"{rel_md}: frontmatter is missing required key {key!r}")

    declared = data.get("name")
    if declared is not None:
        if not isinstance(declared, str) or not declared.strip():
            problems.append(f"{rel_md}: `name` is empty")
        else:
            declared = declared.strip()
            if declared != name:
                problems.append(
                    f"{rel_md}: `name` is {declared!r} but the directory is {name!r}"
                )
            if len(declared) > MAX_NAME:
                problems.append(
                    f"{rel_md}: `name` is {len(declared)} chars, over the "
                    f"{MAX_NAME}-char limit Crush enforces"
                )
            if not CRUSH_NAME_RE.match(declared):
                problems.append(
                    f"{rel_md}: `name` {declared!r} does not match Crush's pattern "
                    "^[a-zA-Z0-9]+(-[a-zA-Z0-9]+)*$ (no leading, trailing or "
                    "consecutive hyphens, no underscores or spaces)"
                )

    description = data.get("description")
    if description is not None:
        if not isinstance(description, str) or not description.strip():
            problems.append(f"{rel_md}: `description` is empty")
        else:
            description = description.strip()
            length = len(description)
            if length > MAX_DESCRIPTION:
                problems.append(
                    f"{rel_md}: `description` is {length} chars, over this repo's "
                    f"{MAX_DESCRIPTION}-char cap (Crush drops it silently at "
                    f"{CRUSH_MAX_DESCRIPTION}; the margin absorbs HTML escaping)"
                )
            quoted = sorted({ch for ch in "'\"" if ch in description})
            if quoted:
                warnings.append(
                    f"{rel_md}: `description` contains {', '.join(repr(q) for q in quoted)} "
                    "-- Crush HTML-escapes the description into every system prompt "
                    "(skills.go:31), so each one costs 6 characters there"
                )

    compatibility = data.get("compatibility")
    if isinstance(compatibility, str) and len(compatibility.strip()) > MAX_COMPATIBILITY:
        problems.append(
            f"{rel_md}: `compatibility` is {len(compatibility.strip())} chars, over the "
            f"{MAX_COMPATIBILITY}-char limit Crush enforces"
        )

    metadata = data.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            problems.append(f"{rel_md}: `metadata` must be a mapping of string values")
        else:
            for key, value in sorted(metadata.items()):
                if not isinstance(value, str):
                    raw = value.raw if isinstance(value, NonString) else value
                    problems.append(
                        f"{rel_md}: metadata.{key} is {raw!r}, which YAML resolves to a "
                        "non-string -- Crush types metadata as map[string]string, so "
                        "this fails to unmarshal and drops the skill (quote it)"
                    )

    if data.get("user-invocable") in (True, "true", "True", "yes"):
        warnings.append(
            f"{rel_md}: `user-invocable: true` makes Crush HTML-escape the ENTIRE body "
            "when the skill is invoked as a command, mangling >, <, quotes and "
            "apostrophes in every shell snippet"
        )

    length = body_line_count(all_lines, after)
    if length > MAX_BODY_LINES:
        problems.append(
            f"{rel_md}: body is {length} lines, over the {MAX_BODY_LINES}-line cap "
            "(the body loads whole; move the detail into references/)"
        )

    return problems, warnings


def lint_all(root):
    """Lint every skill under `root`. Returns (problems, warnings, skill count)."""
    skills_root = Path(root) / "skills"
    if not skills_root.is_dir():
        return [f"no skills/ directory under {root}"], [], 0
    skill_dirs = sorted(p for p in skills_root.iterdir() if p.is_dir())
    siblings = frozenset(p.name for p in skill_dirs)
    problems, warnings = [], []
    for skill_dir in skill_dirs:
        skill_problems, skill_warnings = lint_skill(skill_dir, Path(root), siblings)
        problems.extend(skill_problems)
        warnings.extend(skill_warnings)
    return problems, warnings, len(skill_dirs)


def main():
    problems, warnings, count = lint_all(REPO_ROOT)

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        print(f"\n{len(problems)} problem(s) across {count} skill(s)", file=sys.stderr)
        return 1

    suffix = f" ({len(warnings)} warning(s))" if warnings else ""
    print(f"ok: {count} skills have valid frontmatter and bodies{suffix}")
    return 0


REPO_ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    sys.exit(main())
