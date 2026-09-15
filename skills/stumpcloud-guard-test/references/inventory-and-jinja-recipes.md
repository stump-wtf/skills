# Reading inventories and Jinja from a guard test

Copy-paste code for guards written **inside `stumpcloud/ansible`**, in `tests/`, each paired
with the trap it exists for. Nothing here is a fact about the fleet — every line encodes a property
of PyYAML, Jinja2, or the Ansible inventory format. Neither library is stdlib; both are in that
repo's `Pipfile`, which is why `run-tests.sh` runs under `pipenv` and a bare `python3 -m pytest
tests` errors out on the Jinja-rendering modules.

## 1. The tag-tolerant loader — required, not optional

`yaml.safe_load` on any inventory raises `ConstructorError: could not determine a constructor for
the tag '!unsafe'`. `dub.yaml` carries 9 `!unsafe` scalars. 52 of the 99 test modules hand-roll this
shim under four names — `_InventoryLoader` (19), `_TagTolerantLoader` (16), `_Loader` (14),
`_LooseLoader` (2) — so grepping one finds a third of them. There is no shared helper; paste it:

```python
class _InventoryLoader(yaml.SafeLoader):
    """SafeLoader that tolerates the inventories' custom tags (e.g. !unsafe)."""

_InventoryLoader.add_multi_constructor(
    '!', lambda loader, suffix, node: loader.construct_scalar(node)
)

def _load_inventory(path):
    with open(path) as handle:
        return yaml.load(handle, Loader=_InventoryLoader)
```

Use `add_multi_constructor('!')`, not a constructor for `!unsafe` specifically — the next custom tag
someone adds must not turn your guard into a hard error at collection. **Playbooks and roles are
plain `yaml.safe_load`**; only the root inventories carry tags.

## 2. Recording duplicate keys instead of swallowing them

YAML says the last duplicate key wins and PyYAML honors that in silence, so a key written twice is
invisible to every other assertion in your module. `pdx.yaml` carried `ansible_ssh_user` twice and
nothing noticed.

```python
class _InventoryLoader(yaml.SafeLoader):
    def __init__(self, *args, **kwargs):
        super(_InventoryLoader, self).__init__(*args, **kwargs)
        self.duplicate_keys = []

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _value in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                self.duplicate_keys.append((key, key_node.start_mark.line + 1))
            seen.add(key)
        return super(_InventoryLoader, self).construct_mapping(node, deep=deep)
```

Reach the recorded list through the loader **instance** — `yaml.load()` discards it. Construct
`_InventoryLoader(open(path).read())`, call `get_single_data()`, `dispose()` in a `finally`, then
assert on `loader.duplicate_keys`. `start_mark.line + 1` is 1-based, so the failure message points
at a line an editor can jump to.

## 3. The host walk — and the shadowing that makes a guard vacuous

A hostname appears **twice**: once under `all.hosts` with its full var block, and again under every
`children` group it belongs to, where the value is `None` (membership only). If the empty entry
overwrites the real one, every host reads as `{}` and every check passes having examined nothing.

```python
def _hosts(data):
    """Every host mapping, keyed by hostname. A membership-only entry must
    never replace a real var block."""
    found = {}
    groups = (data['all'].get('children') or {}).values()
    sources = [g.get('hosts') if isinstance(g, dict) else None for g in groups]
    sources.append(data['all'].get('hosts'))
    for source in sources:
        for name, vars_ in (source or {}).items():
            if isinstance(vars_, dict) and vars_:
                found[name] = vars_         # a real block always wins
            else:
                found.setdefault(name, {})  # membership only
    return found
```

If you only need the **names**, recurse — groups nest, so one level misses hosts:

```python
def _walk(node, found):
    if isinstance(node, dict):
        found.update(node.get('hosts') or {})
        for child in (node.get('children') or {}).values():
            _walk(child, found)
```

## 4. A service definition may live under `all.vars` **or** under a host

Both are legal and both are used — the service role is handed a var by name and does not care about
its scope. Pinning either layout makes the guard fail on an unrelated move.

```python
def _service_definitions(data):
    all_block = data['all']
    scopes = [all_block.get('vars') or {}]
    scopes += [host or {} for host in (all_block.get('hosts') or {}).values()]
    definitions = {}
    for scope in scopes:
        for key, value in scope.items():
            # A service definition is a mapping with an image to run.
            if isinstance(value, dict) and 'image' in value:
                definitions.setdefault(key, value)
    return definitions
```

Identify a service **structurally** (it has an `image`), never by a name list.

## 5. Discover the corpus by glob, not by a hardcoded list

22 modules hardcode `INVENTORIES = [...]`; 6 glob. Prefer the glob, and say why in a comment: an
inventory this suite does not scan is one whose converge breaks in production instead of in CI.

```python
INVENTORIES = sorted(
    os.path.basename(p) for p in glob.glob(os.path.join(REPO_ROOT, '*.yaml'))
)
```

A glob returns non-inventories, so check the shape before indexing `data['all']`. As of 2026-08-30
the root glob returns 7 files: four `all`-rooted inventories, two top-level **lists**, and one that
parses to `None` — re-derive that split rather than trusting it. Skip the non-inventories with an
explicit `pytest.skip('%s declares no all block' % name)`, so `-rs` shows a sentence a human wrote
rather than the bare "got empty parameter set".

## 6. Render the real Jinja; do not regex its source

A filter chain that no longer does what its comment claims is invisible to a text assertion — the
text is exactly what did not change. Build an environment close enough to Ansible's and render.

```python
def _combine(*dicts):
    # ansible.builtin.combine, non-recursive — later wins, as the plays use it.
    merged = {}
    for item in dicts:
        merged.update(item or {})
    return merged

def _jinja_env():
    env = jinja2.Environment(keep_trailing_newline=True)
    env.filters['dict2items'] = lambda d: [{'key': k, 'value': v} for k, v in d.items()]
    env.filters['regex_replace'] = lambda s, p, r: re.sub(p, r, str(s))
    env.filters['combine'] = _combine
    env.tests['match'] = lambda v, p: re.match(p, str(v)) is not None
    return env

def _render(expression, **context):
    return _jinja_env().from_string(expression).render(**context).strip()

def _render_literal(expression, **context):
    return literal_eval(_render(expression, **context))   # from ast import literal_eval

def _role_var(name):
    # The Jinja expression the service role binds `name` to.
    for task in _load(ROLE_TASKS):
        for value in (task or {}).values():
            if isinstance(value, dict) and name in value:
                return value[name]
    raise AssertionError('%s is not defined in %s' % (name, ROLE_TASKS))
```

Pull the expression out by name, as `_role_var` does — pasting it into the test makes a copy that
drifts. Assert on the resulting **data structure**, not a rendered string. Where ordering *inside* one expression is the invariant, `.index()` on the source is the honest
tool: `expr.index('combine(service_labels)') < expr.index('combine(_generated_labels)')`.

## 7. The scalar-type trap

Not every value comes out of YAML as a `str`, and the ones that do not are precisely the ones a
type-filtered guard waves through:

| Written in YAML | Python type | Why it matters |
|---|---|---|
| `key: 8675309` | `int` | An unquoted numeric credential |
| `password: no` | `False` | YAML 1.1 booleans: `no`, `off`, `y`, `n` |
| `version: 3.14` | `float` | A version that silently loses a trailing zero |
| `key:` | `None` | Declared and deliberately empty — not the same as missing |

Parametrize the must-flag cases explicitly, including the non-string ones. A guard that skips them
reports success having matched nothing. And keep `None` distinct from a missing key: they usually
mean different things, and `if not value` collapses them.

## 8. When to fall back to a text scan

Sometimes the assertion is about a line, not a value — that a null service key is gone from an
`all.vars` registry, say. A load normalizes away the thing you care about. Scanning text is
legitimate; say so in the docstring, and anchor on exact indentation with `==`, not `in`:

```python
for line in DUB.read_text().splitlines():
    if line == "    manyfold:":          # 4-space all.vars indent
        raise AssertionError("dub.yaml still declares a null `manyfold` key")
```

A substring match on `manyfold:` fires on a comment or a nested key and produces a failure nobody
can act on.
