# Deriving fleet facts

Every host, service, group, and variable in StumpCloud is derivable from the four site
inventories at the root of `stumpcloud/ansible`. Derive it. Do not write it down.

The reason is not tidiness. Several documents in and around this repo state a fleet fact,
and they disagree with each other and with the inventory —
`.claude-ops/checks/verify-caddy.md:7` says Caddy runs on "all three hosts" where the
`caddy` group has four, the `roles/service` defaults header names 36 config keys where the
body reads 43, and `AGENTS.md` says there are four site inventories at line 159 and lists
three at line 1415. Each was true when written, and they get corrected unevenly: PR #578
(2026-08-30) rewrote 148 lines of `AGENTS.md` and `CLAUDE-OPS.md` in one pass and left that
contradiction standing. A further table changes nothing except how long the next agent
spends deciding which to believe.

All commands below run **on the operator Mac, from the repo root**, with the repo venv on
`PATH` — see `operator-shell.md` for why the bare `ansible-inventory` is a different
program. Nothing here needs a credential or reaches a host.

## The assertion that has to wrap all of it

`ansible-inventory` and `ansible-playbook` both exit **0** on an unparseable inventory.
They print a `[WARNING]: Failed to parse inventory with auto plugin` to stderr, emit an
empty host set, and report success. Verified 2026-08-30 against a deliberately broken file.

So a derivation is only trustworthy if it asserts on **content**:

```sh
ansible-inventory -i dub.yaml --list \
  | python3 -c 'import json,sys; h=json.load(sys.stdin)["_meta"]["hostvars"]; \
assert h, "empty host set - inventory did not parse"; print(len(h), "hosts")'
```

`tests/test_inventory_parses.py` makes exactly these two assertions (parses at all, yields
a non-empty host set) for all four inventories, and `make test` runs it. If you want the
guarantee rather than the number, run `make test` and read that module.

## Hosts and groups for a site

```sh
ansible-inventory -i dub.yaml --list      # full JSON: groups + _meta.hostvars
ansible-inventory -i dub.yaml --graph     # the group tree, readable
```

Snapshot 2026-08-30: dub 17 hosts, dtw 6, pdx 1, gva 1. Group counts move faster than host
counts — they had already drifted from a manifest written six days earlier — so derive them
rather than quoting them.

`pdx.yaml` resolving to a single host is not a parse failure. PDX is nearly decommissioned
(ADR-0024); only `nuc01` survives. A tool that treats "one host" as suspicious will keep
raising a false alarm about it.

## Who is in a group

```sh
ansible-inventory -i dub.yaml --graph caddy
```

This is the direct answer to the whole `verify-caddy.md` class of question — a prose file
naming the members of a group. The group is the record; the prose is a copy someone made.

## What runs on a host

```sh
ansible-inventory -i gva.yaml --host cloud01
```

`--host` merges **both** service-definition surfaces, which is why it beats any hand-rolled
walk. A service is a dict carrying an `image` key, and it may be declared under the host
block **or** under `all.vars`:

| Site | host-block services | `all.vars` services |
|---|---|---|
| dub.yaml | 91 | 11 |
| dtw.yaml | 8 | 8 |
| pdx.yaml | 5 | 4 |
| gva.yaml | **0** | 7 |

Measured 2026-08-30. The gva row is the trap: a host-block-only walk reports `cloud01` as
running nothing, and being invisible in exactly that way is the most likely reason gva sat
outside the on-merge deploy until 2026-08-09 (see the `ALL_INVENTORIES` comment in
`.gitea/scripts/detect-deploy-targets.py`).

To list just the services on a host:

```sh
ansible-inventory -i gva.yaml --host cloud01 \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
print(sorted(k for k,v in d.items() if isinstance(v,dict) and "image" in v))'
```

Add `and v.get("enabled") is not False` to exclude tombstones, or invert it to find them —
`enabled: false` is a deliberate soft-tombstone, not a service to switch back on.

## Which hosts a playbook targets

Nothing else answers this. `ansible-inventory` knows the inventory; the playbook knows its
own `hosts:` pattern, which may name a **group or a bare host**.

```sh
ansible-playbook -i dub.yaml playbooks/services/jellyfin.yaml --list-hosts
```

`--list-hosts` resolves the pattern without connecting to anything. Two outputs to read
correctly:

- `hosts (1): ie01` — the answer.
- `Could not match supplied host pattern` followed by `hosts (0):` and **exit 0** — the play
  targets nobody. That is a real answer about the fleet (the service is not deployed
  anywhere on that site), not a broken command. As of 2026-08-30, five of the 77
  service-role playbooks resolve to no host on any of the four sites.

Resolving only *groups* under-reports, because a `hosts:` line naming a bare host never
appears in the group tree at all.

## One variable, with precedence applied

```sh
ansible-inventory -i dub.yaml --host ie01 \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("paths"))'
```

`--host` gives the merged view, so a host-level key that overrides `all.vars` shows its
winning value. Two precedence facts worth knowing before you trust a raw grep:

- A host-level block **replaces** a group default wholesale rather than merging into it
  (`hash_behaviour=replace`). `dub.yaml` carries an inline comment about this on the
  `beszel` block, where per-site wiring once hid the Beszel hub from the hosts that needed
  it most.
- `all.vars` sets the legacy alias `ansible_ssh_user: joestump`, which shadows a host-level
  `ansible_user` (`dub.yaml:214`). Hosts reached as root set **both** keys. So the connection
  identity is not readable from `ansible_user` alone.

## Counting playbooks and roles

```sh
ls playbooks/services/*.yaml | wc -l                            # 140
grep -lE '^\s+-?\s*(role|name):\s*service\s*$' playbooks/services/*.yaml | wc -l   # 76
grep -l docker_compose_v2 playbooks/services/*.yaml | wc -l     # 28
```

Snapshot 2026-08-30, at `b5f7c492`. These numbers move on ordinary merges — `5643c895`
dropped two of them the same week by deleting the dead Infisical plays — which is why the
commands, not the counts, are the content here. The residue is not a rounding error: roughly a third of
`playbooks/services/` is not a service deployment at all — DNS records, CI secrets, OpenBao
identities, and the aggregate imports (`media.yaml`, `office.yaml`, `downloads.yaml`, ...).
Do not infer "number of services" from "number of files in that directory".

## What not to derive this way

- **Anything that needs a live host.** These commands read files. Container state, disk
  usage, and health belong to the fleet-triage and monitoring skills, over SSH.
- **The changed-file to deploy-target mapping.** `.gitea/scripts/detect-deploy-targets.py`
  owns it, including the fallback that resolves `speedtest` to `speedtest-tracker.yaml`.
  Call the script.
- **A snapshot you intend to reuse.** If a number lands in a file, date it and put the
  command that produced it on the next line, so the next reader can re-derive it in one
  step instead of trusting it.
