#!/usr/bin/env python3
"""
Plugin Manifest Linter

Validates `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`.
Both parse as JSON by definition, so the interesting failure is the one JSON
parsers do not report: a duplicate key. `json.load` keeps the last occurrence
and discards the rest silently, which is how plugin.json carried two
`description` keys until 2026-08-19 -- every tool read the second one and
nobody could tell the first existed.

`json.load` is therefore given an `object_pairs_hook` that refuses duplicates
at any depth, plus a few shape checks so a manifest cannot advertise a plugin
name that disagrees with the plugin it points at.

@joestump-agent 08/19/2026 - Initial version.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PLUGIN_REQUIRED = ("name", "version", "description")
MARKETPLACE_REQUIRED = ("name", "owner", "plugins")
MARKETPLACE_PLUGIN_REQUIRED = ("name", "source", "description")


class LintError(Exception):
    """A manifest problem worth failing the build over."""


def no_duplicate_keys(pairs):
    """object_pairs_hook that turns JSON's silent last-wins into a hard error."""
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise LintError(
                f"duplicate key {key!r} -- JSON parsers keep only the last one, "
                "so the earlier value is silently discarded"
            )
        seen[key] = value
    return seen


def load(path):
    if not path.is_file():
        raise LintError("file is missing")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise LintError(f"invalid JSON: {exc}") from exc


def require_keys(data, keys, where):
    missing = [k for k in keys if k not in data]
    if missing:
        return [f"{where} is missing required key(s): {', '.join(missing)}"]
    empty = [k for k in keys if isinstance(data[k], str) and not data[k].strip()]
    return [f"{where} has an empty {k!r}" for k in empty]


def lint(root=REPO_ROOT):
    plugin_dir = root / ".claude-plugin"
    problems = []
    plugin = marketplace = None

    for path, required, name in (
        (plugin_dir / "plugin.json", PLUGIN_REQUIRED, "plugin"),
        (plugin_dir / "marketplace.json", MARKETPLACE_REQUIRED, "marketplace"),
    ):
        rel = path.relative_to(root)
        try:
            data = load(path)
        except LintError as exc:
            problems.append(f"{rel}: {exc}")
            continue
        if not isinstance(data, dict):
            problems.append(f"{rel}: top level is {type(data).__name__}, expected an object")
            continue
        problems.extend(require_keys(data, required, str(rel)))
        if name == "plugin":
            plugin = data
        else:
            marketplace = data

    if marketplace is not None:
        rel = (plugin_dir / "marketplace.json").relative_to(root)
        entries = marketplace.get("plugins")
        if not isinstance(entries, list) or not entries:
            problems.append(f"{rel}: `plugins` must be a non-empty array")
        else:
            for idx, entry in enumerate(entries):
                where = f"{rel}: plugins[{idx}]"
                if not isinstance(entry, dict):
                    problems.append(f"{where} is not an object")
                    continue
                problems.extend(require_keys(entry, MARKETPLACE_PLUGIN_REQUIRED, where))
                source = entry.get("source")
                # A relative source must actually resolve, or `czu` installs nothing.
                if isinstance(source, str) and not source.startswith(("http://", "https://")):
                    if not (root / source).is_dir():
                        problems.append(f"{where}: source {source!r} is not a directory in this repo")

            # The marketplace entry pointing at this repo must name this plugin.
            if plugin is not None:
                local = [e for e in entries if isinstance(e, dict) and e.get("source") == "."]
                for entry in local:
                    if entry.get("name") != plugin.get("name"):
                        problems.append(
                            f"{rel}: plugins entry for source '.' is named "
                            f"{entry.get('name')!r} but plugin.json declares "
                            f"{plugin.get('name')!r}"
                        )

    return problems


def main():
    problems = lint()
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        print(f"\n{len(problems)} problem(s) in .claude-plugin/", file=sys.stderr)
        return 1
    print("ok: .claude-plugin manifests are valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
