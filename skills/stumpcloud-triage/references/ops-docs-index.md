# Routing map into the claude-ops corpus

`stumpcloud/ansible` carries a body of hand-written health checks and remediation runbooks under
`.claude-ops/`. This file is a **routing map into them**, not a copy of them. It holds filenames,
symptoms, and tiers — the material that changes slowly — and deliberately holds no host lists, no
service lists, and no command text, because that is the material that rots.

## Two ways to reach a document

| You have | Read it at |
|---|---|
| a checkout of `stumpcloud/ansible` | `.claude-ops/{checks,playbooks,skills}/<name>.md` — authoritative |
| no checkout | `https://stumpcloud.pages.stump.rocks/ansible/claude-ops/{checks,playbooks,skills}/<name>/` |

The published copy is generated from the same files by `docs-site/scripts/transform-claudeops.js`,
which strips the `.md` extension and requires the trailing slash. `CLAUDE-OPS.md` itself publishes at
`.../claude-ops/overview/`. `checks/README.md` is skipped by the generator and exists only in-repo.

## The reading protocol — apply it to every document here

These documents are trustworthy about **sequence and reasoning** and untrustworthy about **scope**.
Three habits, in this order:

1. **Re-derive every host list before acting on it.** `verify-caddy.md:7` and
   `fix-caddy-routing.md:14` both say Caddy runs on "all three hosts (ie01, pie01, pie02)"; the
   `caddy` group resolved on 2026-08-30 to four members, adding ie02. `.claude-ops/skills/
   service-inventory.md:15` repeats the same three. `CLAUDE-OPS.md` errs the other way, warning that
   `pdx.yaml` still lists five decommissioned Portland hosts when it holds exactly `nuc01` today.
   The inventory answers this correctly every time — see the derivation commands in `SKILL.md`.
2. **Add the location tag yourself.** The check format mandated by the repo's own check-authoring
   skill has no field for where a command runs, and its example patterns show `ss -tlnp`,
   `docker inspect`, `docker exec`, and `docker logs` with no `ssh` prefix. `checks/README.md` has an
   `## SSH Access` heading with no body under it. So a bare `docker …` line in any of these files
   means "on the host", never "here", and needs `sudo` as well.
3. **Screen for BSD hazards before pasting.** `fix-wedged-container-shim.md:35` and `:98` use
   `timeout`, which does not exist on the operator Mac; `verify-media-permissions.md:14-43` uses
   `stat -c`, which BSD rejects. Both fail in the direction that produces a confident wrong answer.

What is sound in them: the dependency orderings, the cooldown budgets, the escalation ladders, and
the failure-mode explanations. Those are the reason to read them at all.

## Symptom to document

Start here, not at the file listing. A symptom often routes somewhere non-obvious — an arr app
reporting "Download client is unavailable" is a Gluetun problem, not an arr problem, because Gluetun
is the single point of failure for the entire download stack.

| Symptom | Go to | Tier |
|---|---|---|
| One or more services returning 502, Caddy not picking up new labels | `playbooks/fix-caddy-routing.md`, `checks/verify-caddy.md` | 2, then 3 |
| Arr reports "Download client is unavailable"; qBittorrent, SABnzbd, or Pinchflat unreachable | `playbooks/fix-gluetun-stack.md` first, `checks/verify-gluetun.md` | 2 |
| Downloads running but egress may be leaking the host IP | `checks/verify-vpn.md` | 1 |
| Arr stack misbehaving with a healthy VPN | `checks/verify-arr-stack.md`, `skills/arr-stack-management.md` | 1, 2 |
| Arr API keys rejected after a container recreate | `playbooks/setup-arr-api-keys.md` | 2 |
| Cannot log in anywhere; OIDC callback or "invalid client" errors | `checks/verify-oidc.md`, `playbooks/fix-oidc-auth.md` | 1, 2 |
| A single OIDC client misbehaving in Pocket ID | `skills/pocket-id-client-check.md` | 2 |
| Password resets appear not to take effect | `playbooks/fix-lldap-opaque-auth.md` | — |
| "Too many connections" or connection refused to Postgres or MariaDB | `checks/verify-database-health.md`, `playbooks/fix-database-connections.md` | 1, 2 |
| Routine database upkeep, sizes, locks, long queries | `skills/database-maintenance.md`, `skills/postgres-inspection.md`, `playbooks/manage-postgres.md` | 2, 3 |
| Grafana database dashboards empty | `checks/verify-database-metrics.md` | 1 |
| Disk above 85 percent, or "no space left on device" | `checks/verify-disk-space.md`, `playbooks/fix-disk-space.md` | 1, 2 |
| Stale `buildx_buildkit_*` or `GITEA-ACTIONS-TASK-*` containers piling up | `playbooks/cleanup-buildx-containers.md` | 2 |
| General Docker reclaim | `skills/prune-docker.md` | 2 |
| "Permission denied" in a container log; wrong ownership under `/volumes` or `/media` | `checks/verify-media-permissions.md`, `playbooks/fix-media-permissions.md` | 1, 2 |
| A service DNS name does not resolve, or points at the wrong host | `checks/verify-dns-resolution.md` | 1 |
| `*.pages.stump.rocks` 502 while the web UI container still reports healthy | `checks/verify-garage-pages.md`, `playbooks/fix-garage-pages.md` | 1, 2, 3 |
| A UI "looks broken" but HTTP checks return 200 | `checks/verify-ui-services.md` | 1 |
| Duplicate monitors multiplying in Uptime Kuma | `checks/verify-kuma-duplicates.md` | 1 |
| A service is up but slow | `playbooks/troubleshoot-slow-service.md` | 1 |
| `docker inspect`, `restart`, or `kill` hangs on one container while the daemon is fine | `playbooks/fix-wedged-container-shim.md` | 3 |
| Proxmox web UI throwing certificate warnings; renewal failing with `InvalidClientTokenId` | `checks/verify-pve-certificates.md`, `playbooks/fix-pve-acme-renewal.md` | 1, 3 |
| Anything else on a hypervisor | `skills/proxmox-fleet-management.md` | 1 |
| rack-hud showing a host as unknown | `checks/verify-rack-hud-collector.md` | 1 |
| YouTube channels in Jellyfin missing posters or hero art | `checks/verify-youtube-artwork.md`, `playbooks/fix-youtube-series-images.md` | 1, 2 |
| Pinchflat sources, metadata refreshes, cookie state | `skills/pinchflat-management.md` | 2 |
| A restart did not fix it and the service needs a converge | `playbooks/redeploy-service.md` | 3 |
| Moving a service between hosts | `skills/service-migration.md` | 3 |
| A fleet-wide "what is running" report | `skills/service-inventory.md` | 1 |
| Reading a secret to diagnose a misconfiguration | `skills/vault-secrets.md` | — |

## Tier, as these documents use it

`**Tier**: N` in a playbook header is the most invasive action the document authorizes, matching the
tier model in `SKILL.md`: 1 diagnose, 2 restart, 3 Ansible converge. A header reading
`2 (container restart), Tier 3 (redeployment)` means the document walks both, in that order — do not
jump to its Tier 3 section without running its Tier 2 section first. Four documents carry no tier
header (`fix-lldap-opaque-auth.md`, `vault-secrets.md`, and the `checks/` files, which are Tier 1 by
construction); treat an untiered playbook as at least Tier 2 and read its own preconditions.

## Snapshot and how to refresh it

As of **2026-08-30** the corpus is 16 checks plus a README, 16 playbooks, and 10 skills — 3,719
lines. Re-derive:

```
[mac] cd ~/src/ansible
      ls .claude-ops/checks/*.md .claude-ops/playbooks/*.md .claude-ops/skills/*.md
      wc -l .claude-ops/*/*.md | tail -1
```

If that listing shows a filename this table does not, the table is behind — read the new file's
first paragraph and its `**Tier**` line, then add the row. New files are announced in
`.claude-ops/checks/README.md` by the check-authoring workflow that lives in the ansible repo, which
is why filenames are a safer thing to index than contents.

## What is deliberately not here

- **The documents themselves.** They are actively authored in `stumpcloud/ansible` by workflows that
  live there and update `checks/README.md` as a side effect. A copy in this repo would be written by
  nobody, updated by nobody, and — since the originals are already stale in the ways listed above —
  would double the staleness rather than fix it. Fix `verify-caddy.md` where the inventory is.
- **Host and service inventories.** Derive them; `SKILL.md` carries the commands.
- **Endpoint lists.** They rot fastest of all. The existing hourly monitor prints
  `https://beszel.stump.wtf` in every alert it sends; that name did not resolve on 2026-08-30, while
  `beszel.stump.rocks` returned 200. Resolve a service name from the inventory that declares it, or
  ask DNS, rather than reading one out of a document.
