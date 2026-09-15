---
name: stumpcloud-triage
description: >
  Diagnose and remediate a sick StumpCloud fleet from the outside in. Use when a service is down,
  502, 503, timing out, or looks broken; when a host is unreachable; when a container is unhealthy,
  restart-looping, or wedged; when downloads stop, OIDC or Pocket ID logins fail, a database refuses
  connections, or a disk fills; and when someone asks what is broken, is StumpCloud down, why is
  jellyfin 502, check the fleet, or run a health sweep. Covers the DNS to edge to container to
  dependency to application ladder, blast-radius triage across the dub, dtw, pdx, and gva sites, the
  Tier 1 diagnose, Tier 2 restart, Tier 3 Ansible redeploy model and its stateless cooldowns,
  restart ordering for Gluetun and OIDC dependents, and the escalate-immediately list. Routes into
  the claude-ops checks and fix playbooks that live in stumpcloud/ansible.
---

# StumpCloud triage

Diagnose first, act narrowly, escalate early. This skill reads state, restarts containers, and at
most redeploys one service with `--limit`. It **never edits a file in `stumpcloud/ansible`** — a fix
that needs a config change is escalated as a concrete suggestion (the "Never Edit Code" section of
`CLAUDE-OPS.md` — grep the heading; its line number moves with every edit to that file). Alerting
cadence, transport, and message shape are not here; this is the infrastructure half.

## Where you are standing

Every command below is tagged `[mac]` (operator laptop) or `[host]` (over SSH on a fleet host). The
tag is load-bearing: a `[host]` command run on the Mac usually **returns a wrong answer instead of
an error**, and the wrong answer reads like a diagnosis.

- The Mac is BSD. `timeout`, `gtimeout`, `gdate`, `gstat` are absent; `date -d`, `stat -c`, and
  `head -n -1` fail. The Linux hosts have all of them.
- Two live examples in the material this skill routes to:
  `.claude-ops/playbooks/fix-wedged-container-shim.md:35` runs `timeout 5 docker inspect <c>` with
  no `ssh` prefix — on the Mac that exits 127, the `|| echo "WEDGED"` branch fires, and you have
  just diagnosed a wedged shim on a healthy host, whose remediation ladder ends at the blind
  `dockerd` restart blamed for stumpcloud#110 and the lake01 25-minute DTW outage.
  `.claude-ops/checks/verify-media-permissions.md:14-43` runs thirteen bare `stat -c … 2>/dev/null`
  lines; BSD rejects `-c`, the redirect eats the error, and empty output reads as "path missing".
- `docker`, `ss`, `journalctl`, `free`, `nproc`, `/proc/*` are `[host]`. `dig`, `curl`,
  `openssl s_client`, `aws`, `ansible-*` are `[mac]`.
- The shell is non-interactive with no credentials in the environment. `gh` uses its keyring, `tea`
  its config file, and `vault` needs `export VAULT_ADDR=https://vault.stump.rocks` first.

## Getting in — derive it, never recall it

```
[mac] cd ~/src/ansible                      # dub | dtw | pdx | gva at the repo root
      pipenv run ansible-inventory -i dub.yaml --list \
        | python3 -c 'import json,sys; print(*sorted(json.load(sys.stdin)["_meta"]["hostvars"]))'
      pipenv run ansible-inventory -i dub.yaml --graph        # group membership, ~250 lines
      pipenv run ansible-inventory -i dub.yaml --host lir     # per-host vars, incl. ansible_user
```

- **SSH lands as `joestump`.** `ssh -G <host> | awk '/^user /{print $2}'` `[mac]` confirms it.
- **Except on hypervisors and account-less guests**, where the inventory sets `ansible_user: root`
  and an unqualified login is refused outright. `ssh -G` still reports `joestump` there, so `-G`
  alone is not enough — read `ansible_user` out of the inventory before you connect.
- **`joestump` is not in the `docker` group.** Verified on ie01 2026-08-30: bare `docker ps` returns
  `permission denied while trying to connect to the docker API`. Every `[host]` docker command needs
  `sudo`. Piping it into `head` makes the exit status 0, so `|| echo` guards do not fire.
- **Hypervisors run no Docker at all** — `command -v docker` on lir returns nothing. `docker ps`
  there is a missing binary, not an empty fleet.
- Never read a host list out of prose. `.claude-ops/checks/verify-caddy.md:7` and
  `fix-caddy-routing.md:14` both say Caddy runs on "all three hosts (ie01, pie01, pie02)"; the
  `caddy` group resolved 2026-08-30 to **four** — ie01, ie02, pie01, pie02. ie02 is checked by
  nothing. `CLAUDE-OPS.md` errs the other way: its PDX table keeps `DECOMMISSIONED` rows for five
  hosts `pdx.yaml` no longer declares at all. Useful history, not fleet membership.

## The outside-in ladder

Climb from the outside. **Stop at the first red rung** — everything below a red rung is noise, and
chasing it is how a DNS problem becomes a database investigation.

| # | Rung | Settle it with | Red means |
|---|---|---|---|
| 0 | Your own network | `[mac] dig +short <svc>.stump.rocks` | DUB names resolve to `192.168.100.x`. Off-LAN and off-WireGuard, the whole site looks down and is not. GVA resolves public, so GVA-up plus DUB-down is your path, not the fleet |
| 1 | DNS | `[mac] dig +short <svc>.<zone>` | No CNAME, or a CNAME to the wrong host. A retired service keeps its record — nothing converges a record with no play |
| 2 | Edge / TLS | `[mac] curl -sS -o /dev/null -w 'code=%{http_code} tls=%{ssl_verify_result}\n' --max-time 15 https://<svc>...` | `000` = no listener or no route. `502`/`503` = Caddy is up and the backend is not → rung 3. Non-zero `tls` = certificate, not app |
| 3 | Container | `[host] sudo docker inspect <c> --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}} started={{.State.StartedAt}}'` | `exited`, `restarting`, or `unhealthy` → rung 4 for the reason |
| 4 | Dependency | `[host] sudo docker logs --tail 50 <c>` | Auth errors → Pocket ID / LLDAP. Connection refused → postgres / mariadb / redis. Fail-closed networking → Gluetun |
| 5 | Application | the service's own health endpoint or admin UI | Container healthy, dependencies healthy, feature still broken |

**The `Health` template trap.** `{{.State.Health.Status}}` on a container with no healthcheck does
not print a blank — it aborts the whole template with `map has no entry for key "Health"` and you
get nothing at all. Always guard it with `{{if .State.Health}}`, as above.

**A green HTTP check is not a working service.** Outline's uploads were broken for eleven months
while hourly page checks passed, because nothing ever asked it to accept a file. When a user reports
a specific capability broken, exercise that capability at rung 5 rather than trusting rung 2.

## Blast radius — read it before you pick a rung

| Symptom | Implicates | Start at |
|---|---|---|
| One service, one host | that container | rung 3 |
| Every service on one host | Caddy on that host, or the host itself | rung 2 |
| Everything in one site at once, from the Mac | your network path first, then that site's edge | rung 0 |
| Every download client | Gluetun — clients are `network_mode: container:gluetun` and fail closed by design | rung 4, Gluetun only |
| Every login, many services | Pocket ID, then LLDAP | rung 3 on the identity host |
| Many services, connection refused | a shared database — do **not** restart the consumers | escalate |
| A hypervisor | no containers live there; it is a PVE-path problem | escalate |

## Tier 1 — diagnose

Always first, even when the fix looks obvious, and always before any Tier 2 action. Rungs 0-5 above,
plus `sudo docker ps -a --format '{{.Names}}: {{.Status}}'` `[host]` for the host-wide picture.
Record what you saw; the escalation is worthless without it.

## Tier 2 — restart, authorized without asking

```
[host] sudo docker restart <container>          # then wait ~20s and re-run the rung that was red
```

**Enforce the cooldown statelessly.** A scheduled run is a fresh session, so "max 2 per window"
cannot live in memory — read it off the container instead. Compare `StartedAt` (RFC3339, UTC, zero
padded, so a plain string compare is correct) against a cutoff:

```
[mac] python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc)-d.timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S"))'
```

If `StartedAt` sorts **after** that cutoff the container has already been restarted inside the
window — by you, by a previous run, or by Docker's restart policy. Do not spend the budget again;
escalate. (`date -d '4 hours ago'` is the obvious move and does not exist on this Mac.)

Documented budgets, verified at source: **2 Gluetun restarts per 4 hours**
(`fix-gluetun-stack.md:14`), **1 full redeploy per service per 24 hours**
(`redeploy-service.md:16`), **1 PVE converge per node per 24 hours when it orders a certificate**
(`fix-pve-acme-renewal.md:37`).

## Ordering constraints

- **Gluetun before its clients, always.** Restart Gluetun, verify the tunnel is up, then restart
  qBittorrent, SABnzbd, Pinchflat, slskd. Never the other way round.
- **A ProtonVPN auth or credential error is not restartable.** Escalate on sight.
- **Pocket ID, then LLDAP, then the OIDC consumers.** A consumer restarted against a down IdP just
  fails again with a fresher timestamp.
- **Never restart postgres, mariadb, or redis to fix a consumer.** Escalate.
- **Caddy is whole-host blast radius.** Restarting it takes every service on that host down for the
  restart. Treat it as an escalation candidate, not a first move.
- **`enabled: false` is deliberate.** Those services are off on purpose. Never "fix" one.

## Tier 3 — redeploy one service

```
[mac] cd ~/src/ansible
      OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES pipenv run ansible-playbook \
        -i <site>.yaml playbooks/services/<name>.yaml --limit <host>
```

`--limit` is mandatory. The fork-safety variable is required on macOS and appears in the repo's own
runbooks (`fix-garage-pages.md:86`). Confirm the service actually has a play for that host before
running: a service running with no inventory definition cannot be restored this way, and a converge
may instead recreate services the inventory still declares but nobody runs.

## Escalate immediately — skip the tiers

ProtonVPN auth failures · OpenBao seal or quorum loss · any database corruption or shared-database
outage · `/volumes` or `/media` full · more than one host down at once · anything security-shaped ·
a wedged container shim (follow `fix-wedged-container-shim.md` and **never restart `dockerd` blind**
— that caused stumpcloud#110 and the lake01 daemon-wedge OMG) · anything on a hypervisor, including
`pveproxy`/`pvedaemon` and certificate orders.

## Never

Edit any file in `stumpcloud/ansible` · delete under `/volumes` or `/media` · force-pull an image
(WUD watches, redeploys apply) · change DNS, Caddy config, WireGuard, or UniFi by hand · run
`pvesh get /cluster/acme/plugins` unredacted, which prints `AWS_SECRET_ACCESS_KEY` in plaintext ·
rename a hypervisor · restart a VPN-routed client without checking Gluetun first.

## Handing off

An escalation carries: the highest red rung, the exact command **and its tag**, the output, what was
restarted and the `StartedAt` you read, the cooldown state, and — for a config fix — the file, the
current value, and the proposed value.

`references/ops-docs-index.md` routes a symptom to the specific check or fix playbook in
`stumpcloud/ansible`, and states how to read those documents safely. For inventory semantics and
running Ansible against this fleet, load the `stumpcloud-ansible` skill.
