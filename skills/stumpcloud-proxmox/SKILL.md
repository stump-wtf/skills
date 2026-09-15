---
name: stumpcloud-proxmox
description: >
  Working reference for the StumpCloud Proxmox fleet - the DUB hypervisors, the
  PBS and PDM appliances at DTW, and allura in PDX. Load BEFORE touching any
  Proxmox node: running any pve-* or proxmox-* Ansible play, fixing an expired
  or failing-to-renew PVE certificate, wiring notifications or Pocket ID SSO,
  registering PBS storage, vzdump jobs or namespaces, sizing ZFS ARC on a
  hypervisor, upgrading a node from PVE 8 to 9, separating a node from a
  cluster, onboarding or relocating a hypervisor, or diagnosing a read-only
  /etc/pve. Also load when someone says the Proxmox web UI is throwing a cert
  warning, renewal is failing, a node will not take a config change, a new
  hypervisor has no backups, or the GPU box will not POST.
metadata:
  tier: "P2"
  domain: "hypervisors"
---

# StumpCloud Proxmox

**Everything Proxmox-specific converges through a playbook in `stumpcloud/ansible`; nothing on a node is hand-set state.** Every incident here has one shape: a value typed into the PVE UI once, that no play declares, that drifts, and that fails silently — an ACME credential, a pinned PBS fingerprint, a notification target with nowhere to send. When you are about to fix something by hand on a node, the fix is a task in a playbook, then a converge.

## The authority is in the repo, not here

`docs/playbooks/proxmox-fleet.md` is the fleet map and it **wins every conflict with this file**. Read it before acting; if this skill disagrees with it, correct this skill in the same task.

| Need | Read |
|---|---|
| Fleet map, what each play owns, the failure-mode essay, hal GPU bring-up | `docs/playbooks/proxmox-fleet.md` |
| Per-play reference | `docs/playbooks/pve-acme.md`, `pve-notifications.md` |
| Cluster separation, renumbering, the 8 to 9 ladder, in that order | `docs/playbooks/relocate-proxmox-node-between-sites.md` (427 lines, as-built) |
| Standing up a brand-new hypervisor | `docs/playbooks/onboard-dagda-proxmox-host.md` |
| Cert triage and the ACME fix runbook | `.claude-ops/checks/verify-pve-certificates.md`, `.claude-ops/playbooks/fix-pve-acme-renewal.md` |
| Read-only inspection commands | `.claude-ops/skills/proxmox-fleet-management.md` |
| Why hypervisors are in inventory at all | `docs/adrs/ADR-0045`, `ADR-0051`, `ADR-0021` |

All three governing ADRs are `status: proposed` (verified 2026-08-30). They are the operative convention and every `pve-*` play cites them, but say "proposed" when you quote a status. ADR-0021 built the `joestump.proxmox` collection; its "API-only, never SSH" premise is retired, the collection is not.

## Never memorize the fleet — derive it

Four written host tables disagree with the inventory right now. The inventory is the answer, and it needs no credentials:

```bash
# [mac] from /Users/joestump/src/ansible. VAULT_ADDR/VAULT_TOKEN may be empty.
.venv/bin/ansible-inventory -i dub.yaml --graph pve        # membership
.venv/bin/ansible-inventory -i dub.yaml --graph proxmox    # + PBS/PDM on dtw.yaml
.venv/bin/ansible-inventory -i dub.yaml --host <node>      # per-node vars
```

Verified 2026-08-30: returns in under a second with both `VAULT_*` empty. `dtw.yaml` carries `lotor`, `coran` and the appliances; `allura` is in DNS and **no** inventory on purpose — it is still a `stump-us-west1` cluster member in PDX, so converging standalone-node config onto it would be wrong.

Do the same for the play set rather than trusting a table — `ls playbooks/pve-*.yaml playbooks/proxmox-*.yaml`, then read each play's own `hosts:` line. `playbooks/pve-zfs-arc.yaml` exists, targets `hosts: pve`, and appears in **no** document in the repo.

## Reaching a node

Hypervisors and appliances are the fleet's exception: they are reached **as root**, and the bare hostname fails.

```bash
ssh root@lir.stump.rocks 'pveversion'    # works
ssh lir.stump.rocks                      # Permission denied (publickey,password)
```

Verified 2026-08-30. `~/.ssh/config` sets no `User` for `*.stump.rocks`, so a bare host falls through to the local username `joestump`, which does not exist on a PVE node. Docker hosts land as `joestump`; these do not.

In inventory that is four settings per host, not one. `all.vars` sets the legacy alias `ansible_ssh_user: joestump`, which **shadows** a host-level `ansible_user` — both must be set (`dub.yaml:214` comments this inline). Also set `ansible_become: false` (you are already root) and `ansible_python_interpreter: /usr/bin/python3` (Debian 13 ships 3.13; `all.vars` pins 3.12 for the guest fleet). Do not pin `ansible_ssh_private_key_file`.

For the macOS run environment — the fork-safety variable, `VAULT_ADDR`, the venv-versus-pipenv question — load the `stumpcloud-ansible` skill rather than guessing.

## Running the plays

`--check --diff` first, then one node, then the fleet. Read the play's `hosts:` line before trusting a `--limit`, because the two families behave differently:

- **`pve-acme`, `pve-notifications`, `pve-storage`, `pve-nic-tuning`, `pve-zfs-arc`** all target `hosts: pve` and open with the same `assert` on `pve_role == 'hypervisor'`. A node without that var is refused, which is the guard that keeps the guest `host` role and its Docker install off a hypervisor.
- **`proxmox-oidc-sso`, `proxmox-homepage-tokens`** target `hosts: proxmox` (PVE + PBS + PDM) and branch on `proxmox_kind` instead. No `pve_role` assert.
- `pve-storage.yaml` also carries two `hosts: pbs` plays at the bottom (lines 419 and 563) that only ever run against `dtw.yaml`.

Three traps:

1. **Converge both inventories.** The nodes are standalone, so per-node config (ACME, notifications, OIDC realm) converged on `dub.yaml` leaves DTW untouched. Worst on `-e proxmox_oidc_rotate_secret=true`: rotate one site and the other stops authenticating.
2. **`proxmox-homepage-tokens.yaml` destroys the token it mints if `--limit` excludes localhost.** PVE reveals a token secret once, at creation; the play that stores it in OpenBao runs on `localhost` at the end of the file. `--limit hal` minted a live credential and skipped the store with `no hosts matched`. There is now an assert at line 117 that refuses — scope it `--limit '<node>,localhost'`. Recovery is to delete the token and re-run.
3. **`uri` tasks skip under `--check`.** A dry run never validates API status codes, and combined with `no_log: true` that hides real failures. Verify end state with a read on the node.

## Certificates — the recurring failure mode

Let's Encrypt via Route53 DNS-01, per node, no cluster. On 2026-08-13 the AWS access key rotated; OpenBao and every Ansible consumer picked it up, but the credentials **embedded in each node's ACME plugin** did not. Every renewal failed with `InvalidClientTokenId` next to `_acme-challenge.<node>`, silently, until lir was 19 days from a browser-breaking expiry.

Read-only triage, all safe:

```bash
ssh root@<node> 'openssl x509 -in /etc/pve/local/pveproxy-ssl.pem -noout -enddate -issuer'
ssh root@<node> 'openssl x509 -in /etc/pve/local/pveproxy-ssl.pem -noout -checkend 2592000'   # exit 0 = good for 30d
ssh root@<node> 'journalctl --since -30d | grep -iE "InvalidClientTokenId|_acme-challenge" | tail -20'
```

`-checkend` is an exit status, so it needs no date arithmetic — which also sidesteps the BSD shell's missing `date -d`. The play uses the same primitive (`pve-acme.yaml:346`).

**Never order a certificate by hand** — Let's Encrypt rate-limits duplicates to five per week and an unguarded fleet converge burns that in one run. The fix is converging `pve-acme.yaml`, whose `-checkend` gate is precisely that protection, and which asserts `-checkend 0` on the result so a green run over a still-expired cert cannot happen.

## Two commands here leak a live credential

| Command | Prints |
|---|---|
| `pvesh get /cluster/acme/plugins` | the AWS secret access key, inline in `data` — this is how it reached a terminal in 2026-08 |
| `cat /etc/pve/priv/notifications.cfg` | the SMTP2Go password |

Address the plugin by id (`/cluster/acme/plugins/route53`) and grep `AWS_ACCESS_KEY_ID=` only; the key id is an identifier, not a secret, and comparing it is enough to catch a missed rotation. Never index `plugins[0]` — PVE always carries a builtin `standalone` plugin with no `data` key, usually first. Better still, let `pve-acme.yaml --check --diff` report `ok` / `DRIFT` / `MISSING` under `no_log`.

## Cluster-shaped symptoms

- **A write to `/etc/pve` failing while you are root means lost quorum, not permissions.** `/etc/pve` is a FUSE filesystem that refuses writes without quorum. `pvecm expected 1` is the recovery lever and **does not survive a reboot**; it is not the fix.
- **On a standalone DUB node, `pvecm status` failing with `Cannot initialize CMAP service` is what success looks like.** There is no corosync to ask. Do not "fix" it.
- The other route to a read-only `/etc/pve` is a renamed node: identity is keyed on the **short** name at `/etc/pve/nodes/<name>`, so a rename makes a node a stranger to itself. Revert the short hostname.
- Separation, renumbering and the 8 to 9 upgrade are surgery with a written as-built order (separate, then renumber, then upgrade). Use `relocate-proxmox-node-between-sites.md`; do not improvise.

## A new hypervisor has no PBS namespace, and the play cannot make one for it

`dub@pbs` holds `DatastoreBackup`, which lacks `Datastore.Modify`, so a vzdump job into a missing namespace fails for every guest. `pve-storage.yaml`'s namespace-creation play targets `hosts: pbs` and builds its list from `groups['pve']` **in the same run** — but buoy is only in `dtw.yaml`, whose `pve` group is `lotor` and `coran`. So that play can never create a `dub/<node>` namespace. For a DUB node, do it on buoy as a one-off before the first backup window:

```bash
ssh root@buoy.stump.wtf 'proxmox-backup-debug api create /admin/datastore/dub/namespace --name <node>'
```

All six DUB namespaces exist today (`dub.yaml:1001`, verified 2026-08-23). The next node is the one to worry about.

## Hard nevers

- Never rename a node, and never let `common`'s hostname task run against one (ADR-0051 exempts it).
- Never apply the guest `host` role to a hypervisor — Docker plus system-Python pins, on the interpreter PVE's own tooling uses.
- Never remove a `pve_role` guard to make a play run somewhere.
- Never hand-set state a play owns; never `docker` anything on a hypervisor — they run none.
- Never restart `pveproxy` or `pvedaemon` blind to make a symptom go away.

## Fleet documents that are stale right now

Fix these in `stumpcloud/ansible` in the same session rather than working around them — a wrong fleet fact costs the next agent a rediscovery:

- `.claude-ops/checks/verify-pve-certificates.md:21,30` and `.claude-ops/playbooks/fix-pve-acme-renewal.md:68` hardcode `for h in lir dagda ogma nyma pidge` — the newest node is silently skipped by the exact check that exists to catch silent cert drift. Derive the loop from `ansible-inventory --graph pve`.
- `.claude-ops/skills/proxmox-fleet-management.md:32-41` still lists five nodes, and `:146-156` still leads with an "OPEN BLOCKER" on PBS namespaces that `dub.yaml:1001` closed on 2026-08-23. Do not act on it.
- `docs/playbooks/proxmox-fleet.md:9` says "Five hypervisors" and `:78` says "the four `pve-*` plays" while its own table lists six nodes and five plays exist. `pve-zfs-arc.yaml` has no docs page at all.
