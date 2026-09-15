# Guard taxonomy — the eight shapes in use

Pick the shape before writing code. Each entry gives the failure class it defends against, the
assertion shape, the trap that makes that shape go wrong, and a worked exemplar from
`stumpcloud/ansible`.

Exemplars are keyed by **both** path and docstring first line, because a module gets renamed and a
docstring does not. If a path misses, re-find it: `grep -rl '<first line>' tests/`.

This is not an index of all 99 modules. A full index would be a dated snapshot and would be wrong
within a week — the suite grew from nothing to 99 modules between 2026-07-25 and 2026-08-30.

## 1. Drift guard between two sources of truth

**Failure class.** Two artifacts must agree, nothing makes them, and disagreement is invisible until
someone acts on the wrong one.

**Shape.** Read both artifacts. Derive the same value from each. Assert equality — never assert
either against a literal you typed, or you have created a third thing to drift.

**Trap.** Pinning the wrong side. Asserting `AGENTS.md` contains `"log-driver": "json-file"` ages the
moment the template changes. Asserting it contains whatever `daemon.json.j2` currently ships never
does.

**Exemplar.** `tests/test_ci_check_lanes.py` — *The syntax+lint sweep must cover every playbook
exactly once.* The `check` job in `ci.yaml` fans out over named lanes; the lane list in the workflow
matrix and the mapping in `.gitea/scripts/select-check-lane.sh` must partition the playbook tree.
The test asserts both directions plus a balance ceiling. A playbook matched by no lane is silently
never syntax-checked, and the sweep stays green while coverage shrinks.

## 2. Content-not-return-code guard

**Failure class.** The tool exits 0 on the broken input. Every check keyed on the exit code reports
success.

**Shape.** Parse the artifact in the test. Assert the parse succeeds **and** that it yields a
non-empty result. Two assertions, because parsing is necessary and not sufficient.

**Trap.** Calling the real tool and checking `returncode`. `ansible-inventory -i broken.yaml --list`
warns on stderr, emits an empty host set, and exits 0; so does `ansible-playbook`, which then
reports a clean green no-op against zero hosts.

**Exemplar.** `tests/test_inventory_parses.py` — *Guard every inventory against the fail-open
unparseable-inventory class.* A 2026-08-08 secret scrub rewrote six values to a bare
`***REDACTED***`; unquoted, a leading `*` opens a YAML alias, `pdx.yaml` stopped parsing, and PDX
resolved to zero hosts on main. Its loader also **records duplicate mapping keys**, which PyYAML
resolves last-wins in silence — `pdx.yaml` carried `ansible_ssh_user` twice.

## 3. Doc-currency guard

**Failure class.** A document an agent reads first describes something the repo no longer has. Stale
guidance is worse than absent guidance because it gets acted on.

**Shape.** Two halves. (a) Named-thing existence: extract every path the doc cites and assert it
exists on disk, with an explicit allowlist for names cited *because* they are gone. (b) Value
currency: derive the live value from the artifact and assert the doc states that one.

**Trap.** Placeholders. Worked examples legitimately name `playbooks/services/newservice.yaml`. Keep
the exemption set small, explicit, and commented, or the guard trains people to grow it.

**Exemplar.** `tests/test_agents_md_logging_is_current.py` — *AGENTS.md must not describe a logging
stack the repo no longer has.* It documented the retired `loki` Docker log-driver plugin as live
five and a half months after `daemon.json.j2` moved to `json-file`. The path half caught
`playbooks/caddy.yaml` (it lives under `services/`) and a deleted `playbooks/services/bin.yaml`,
both inside copy-pasteable snippets.

## 4. Safety and ordering guard on a destructive or one-shot play

**Failure class.** A play that is only safe because of its ordering or its gate, where an ordinary
refactor can reorder or ungate it without looking wrong.

**Shape.** Load the play as YAML, take `[t["name"] for t in tasks]`, and assert **index order**:
`names.index(GUARD) < names.index(TEARDOWN)`. Assert the apply flag defaults to false. Assert the
gate expression contains the flag and nothing else that could bypass it.

**Traps.**
- `ansible_check_mode` in a `when:` is not a dry run. `docker_container_exec` has no check mode, so
  a `when:` mentioning it *runs* the destructive task under a bare `--check`.
- A conditional built on a probe that cannot distinguish states. `zpool list <name>` exits non-zero
  for a pool that is exported, faulted, *or* merely not imported yet, so no conditional on it can
  tell absent from temporarily unavailable — which is why the safe answer was removing the creation
  path entirely, not guarding it.
- A var-level `lookup()` fatals before any task-level `when:` can save it; it needs
  `errors='ignore'` at the lookup.

**Exemplars.**
- `tests/test_manyfold_ie01_retire.py` — *The ie01 Manyfold retirement must stay safe to run and safe
  to forget.* Successor-verified-up must precede all three teardown tasks; a dead successor plus a
  completed teardown is total data loss.
- `tests/test_host_storage_pool_guard.py` — *roles/host must never create a ZFS pool.*
- `tests/test_ci_deploy_key_converge_safe.py` — *ci-deploy-key.yaml must be converge-safe
  fleet-wide.* The play was built for one host; a **comment-only** edit on 2026-08-19 mapped it to a
  converge target and run 7381 fatal'd on 19 hosts.

## 5. Index or aggregation guard — the direction the runtime cannot assert

**Failure class.** An index exists so the runtime can avoid an expensive scan. The runtime can then
check index-to-flag, but checking flag-to-index would require exactly the scan the index avoids. The
unchecked direction rots.

**Shape.** Assert the expensive direction statically, where parsing YAML is free and no lookups
fire. Say in the docstring which direction the runtime already holds, so nobody duplicates it.

**Trap.** The host walk. A hostname appears under `all.hosts` with its var block **and** again under
every `children` group with the value `None`. Let the empty entry win and the guard silently sees no
hosts and passes vacuously — see `references/inventory-and-jinja-recipes.md`.

**Exemplar.** `tests/test_gluetun_vpn_member_index.py` — *Guard the Gluetun VPN member index against
the stranded-client class.* Docker resolves `network_mode: container:gluetun` to a container **ID**
at creation, so recreating Gluetun strands every member in a different compose project. On
2026-08-21 that stranded `qbittorrent`, `nzb`, and `pinchflat`; all three reported `Up` and
`healthy` at the time and would have failed on next boot.

## 6. Rendered-expression guard

**Failure class.** A Jinja filter chain no longer produces what its comment claims. Asserting on the
expression source text cannot catch that — the text is exactly what did not change.

**Shape.** Build a `jinja2.Environment`, shim the Ansible filters the expression uses, pull the
expression out of the role or play by variable name, render it against a synthetic context, and
`ast.literal_eval` the result. Then assert on the resulting **data structure**.

**Trap.** Type-filtering the inputs. A guard that only inspects `str` values skips precisely the
dangerous ones: an unquoted numeric key is an `int`, and YAML 1.1 turns `password: no` into `False`.
Both reach `docker inspect` as plaintext.

**Exemplar.** `tests/test_homepage_widget_secret_indirection.py` — *Keep Homepage widget credentials
out of Docker labels (stumpcloud#215).* A container label is plaintext to anything holding the
Docker socket, which on these hosts is six containers. Its parametrize deliberately includes
`8675309`, `False`, `True`, and `3.14` as must-flag cases.

## 7. Script-or-shell-under-test guard, hermetically

**Failure class.** A script in `.gitea/scripts/` whose wrong answer is a *plausible* one — zero
targets, or a green skip — so the pipeline reports success while nothing happens.

**Shape.** Two variants, both hermetic. For a Python script, import it by path with
`importlib.util.spec_from_file_location` and `monkeypatch` its module-level constants. For a shell
script, stand up a stdlib `http.server` as the remote API and point the script at it.

**Trap.** Testing one merge topology. Build a real git repository per topology in `tmp_path` —
merge-commit, squash, rebase — and assert prior-main resolves in each.

**Exemplars.**
- `tests/test_deploy_diff_base.py` — *Unit tests for the deploy lane's diff-base resolution.* The
  RomM `DB_HOST` fix (`stumpcloud/stumpcloud#180`) merged fully green and never shipped: the branch
  was rebase-merged, so `HEAD^1` was the branch's own commit and the lane emitted zero targets.
- `tests/test_vault_preflight.py` — *Unit tests for .gitea/scripts/vault-preflight.sh.* A stub HTTP
  server stands in for OpenBao, so a shell script is tested from pytest without touching
  `vault.stump.rocks`. The behavior under test is as much the message and exit code as the happy
  path — the preflight exists to replace about forty misleading permission-denied failures with one
  actionable error.

## 8. Schema or completeness guard

**Failure class.** Two declarations that must be co-present, where absence resolves to a default and
produces a downstream error naming neither the host nor the service.

**Shape.** Enumerate the pairs from the artifacts themselves — every play's `hosts:` group against
the inventory key its `service_config` names — and assert every member of the set defines the key.

**Trap.** Guessing at the cases you cannot resolve. Keep the guard deliberately narrow: skip
computed values (`set_fact`, play-level `vars`, leading-underscore locals) and inventory-wide
defaults rather than flagging them. A false positive here trains people to add exemptions, and an
exemption list is how the guard dies.

**Exemplar.** `tests/test_service_config_defined.py` — *Every host in a service's group must actually
define that service's config.* `caddy.yaml` migrated from `{{ caddy }}` to `{{ caddy_service }}` and
only two of four hosts got the new key; `service_config` resolved to nothing, the role built a
compose file with an empty `container_name`, and Docker rejected it with a pattern-match error that
reads like a Docker bug. It stayed invisible for weeks because a normal merge converges one or two
services.

## Cross-cutting: assert the wiring, not only the logic

Whenever the real check lives in a CI step rather than in pytest, the guard has two jobs: pin the
script's pure logic offline, **and** assert the step is still wired into `ci.yaml`. A perfectly
correct script that no job invokes is indistinguishable from no script.

`tests/test_image_pullability_gate.py` — *Inventory images must exist in the registry before they
can merge* — does exactly this. `stumpcloud/ansible#384` merged the binnacle service while its image
had never been pushed; CI validated syntax, lint, tests, and secret references, and nothing asserted
the referenced images existed, so the first converge after merge failed at the compose pull
(`stumpcloud/stumpcloud#247`). The module imports `check-image-pullability.py` by path, pins its ref
parsing and first-party scoping offline, and separately asserts the workflow still calls it. The
live registry lookup stays in the CI step, where the network is.

