# Changing, moving and retiring a service

Converge is a **forward-only** mechanism: it ensures what a host's groups say it runs, and it
has no opinion about anything that used to be there. Four things are therefore never undone by
deleting an inventory block, and each has cost a real incident.

| Deleting the block leaves behind | Why | Evidence |
|---|---|---|
| The Route53 CNAME | The role converges the record to absent only while a play still runs for the service | `playbooks/services/dns.yaml:204-218` — 17 orphans from one PR |
| The running container | Converge never stops a service that left a host's groups | `playbooks/maintenance/manyfold-ie01-retire.yaml:1-8` — 1.2 GiB orphan on ie01 |
| The database and its volumes | Preserved on purpose (ADR-0040), removal is an explicit manual step | `roles/service/tasks/main.yaml:69-85` |
| The Kuma monitor and Homepage tile | Discovered from a container that no longer exists, so they go stale rather than absent | — |

## Retire in this order

1. **`enabled: false` on the block, merge, let it converge.** This is the only step that
   actually retires the DNS record: `tasks/main.yaml:404` is
   `state: "{{ service_enabled | ternary('present','absent') }}"`, so the same converge that
   removes the container also removes `<dns>.<domain>`. Compose goes to `state: absent` with
   `remove_orphans: true` (`tasks/main.yaml:245-247`).
2. **Verify.** `dig +short NAME.stump.rocks` must return nothing, and `docker ps` on the host
   must not list the container.
3. **Then** delete the block, the playbook, the runbook, the catalog tile and the
   `CLAUDE-OPS.md` row, in a second PR.
4. If step 1 was skipped and the block is already gone, the record is orphaned. Retire it in
   `playbooks/services/dns.yaml` with an explicit `state: absent` entry — see below.

Do it in the other order and nothing converges the record, ever again. That is exactly what
produced the 17 dead CNAMEs: `#282` deleted the playbooks outright, and the graveyard comment
in `dns.yaml` records the cleanup. Fourteen pointed at decommissioned PDX hardware and were
harmless dead ends; three pointed at live `ie01`, so they resolved, reached Caddy, and failed
there with no route behind them — the worse failure, because it looks like an app outage.

A second instance three weeks later: `manyfold.stump.rocks`, retired in commit `b5b8707a`
(2026-08-29, closes `stumpcloud/stumpcloud#351`). The fix was +10 lines in `dns.yaml`, not an
inventory edit — once the play is gone, inventory is no longer the lever.

## Retiring a record in `dns.yaml`

Read `playbooks/services/dns.yaml:12-34` before touching it. Three properties matter:

- **It re-asserts every record in BOTH zones** (`stump.wtf` and `stump.rocks`) with
  `overwrite: true`. It is not additive. Always `--check --diff` first and read the changed
  items. A record that exists in only one zone is a harmless no-op in the other.
- **Changing a record's type is a two-step operation.** Route53 rejects a CNAME where an A
  already exists at the same name (`InvalidChangeBatch: RRSet of type CNAME ... conflicts`).
  Delete the old type, run again to create the new one.
- **Touch one record without re-asserting the rest** by overriding the list on the command
  line — extra-vars beat play vars:

```bash
# [mac] in the ansible checkout — check first, always
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES pipenv run ansible-playbook -i dub.yaml \
  playbooks/services/dns.yaml --check --diff \
  -e '{"records":[{"name":"NAME","state":"absent","type":"CNAME","value":"OLD_TARGET"}]}'
```

**Retire against what DNS returns today, not what the deleted inventory claimed.** The two
disagreed for all 17 orphans, which is why the graveyard entries were verified with
`dig @8.8.8.8` before being written down. Pin the entry to the stale value you actually
observed, so a task that no longer matches the live zone fails loudly instead of deleting
something someone has since re-pointed at a live host.

```bash
# [mac] what is really there, in both zones
dig +short NAME.stump.rocks @8.8.8.8
dig +short NAME.stump.wtf   @8.8.8.8
```

Two records cannot live in the flat `records:` list at all, and both have a dedicated task at
`dns.yaml:308-352` explaining why: `grafana` is live in one zone and dead in the other, and
`manyfold` is the same shape. A name that is live in one zone and stale in the other must
never go in the list, because the loop would assert it in both and take the live one down.

## Moving a service between hosts

An inventory edit converges the whole group at once with no `--limit`, so a move cannot be one
edit. Stage it:

1. **Merge 1** — add the new host to the group and give it a block, with
   `dns_cname: {enabled: false}` on the **losing** host so the two cannot race for the record.
   Expect two Homepage tiles and some Kuma churn in the window; keep it short.
2. Verify the new instance answers, and that data landed where you expected.
3. **Merge 2** — `enabled: false` on the loser, which converges its container away and,
   because `dns_cname` is off there, leaves the winner's record alone.
4. **Merge 3** — delete the loser's block.

If the loser's container is already orphaned (the play no longer targets that host), inventory
cannot reach it. Write a one-shot maintenance play instead and give it the shape
`playbooks/maintenance/manyfold-ie01-retire.yaml` uses: guards first (the successor must be
answering; the data must still be the recorded empty state), dry run by default, mutation only
behind an explicit `-e <name>_apply=true`.

## Renaming, and changing the domain

- **Renaming** touches the container name, the compose project name (`service_dns`), the data
  directory and the CNAME. Treat it as a retire plus a deploy, in that order, not as an edit.
  A rename in one merge leaves the old record and the old container behind.
- **`domain:` on the block** moves the service into another zone. The role prunes the stale
  record it used to hold in the external zone (`tasks/main.yaml:421-446`) — but only for a
  per-service `domain:` override, and only when the old name is not also listed in `aliases:`.
- **A site-wide `dns.wtf` flip is NOT a `domain:` override**, and the prune does not fire for
  it. That is how `grafana.stump.wtf` sat on decommissioned `int01` hardware after DUB moved to
  `stump.rocks` (`dns.yaml:328-352`, commit `d6ec924c` 2026-08-25, `#336` / PR `#518`). After
  any site-level domain change, `dig` the old names yourself.

## What is guarded, and what is not

`tests/test_manyfold_ie01_retire.py` and `tests/test_route53_delegation.py` gate their own
narrow cases in `stumpcloud/ansible`'s CI. **Nothing gates the general rule** — no test asserts
that a service block removed in a PR has a matching `state: absent` entry in `dns.yaml`. That
guard belongs in `stumpcloud/ansible`'s `tests/`, where `make test` and CI would run it, not in
a skill where it would fire only when someone remembered to invoke it. It is worth an issue in
`stumpcloud/stumpcloud` the next time this bites.
