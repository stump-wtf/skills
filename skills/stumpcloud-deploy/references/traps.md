# Traps

Symptom to cause to fix, for deploy failures whose message does not name what caused them.
Skim before pushing; come back when a converge fails in a way that does not fit your change.

Every trap below has a guard in `stumpcloud/ansible`'s `tests/`, a runtime `assert` in the
role, or both. That is what makes them cheap now — and it means **you must not reimplement
these checks in a new script**. A shadow copy runs only when someone remembers to invoke it,
and drifts from the guard that actually gates merge. If a trap here has no guard any more, that
is one grep to confirm and a PR against `stumpcloud/ansible` to fix.

## Docker rejects a container with an empty name, hundreds of lines in

**Symptom:** `services.app.container_name '' does not match pattern '[a-zA-Z0-9][...]'`, naming
neither the service nor the host. Reads like a Docker bug.

**Cause:** the playbook passed `service_config: "{{ some_key }}"` and the host being converged
does not define `some_key` — a host added to a service's group without a matching block, or a
key renamed on some hosts and not others. Every `service_*` default applies and `service_name`
becomes empty.

**Fix:** the guard at `roles/service/tasks/main.yaml:19-29` now fails first and names both the
host and the key. If you are seeing the raw Docker message, you are on an old checkout. The
original cost a full fleet-wide converge to trace back to two hosts sitting in the `caddy`
group with no `caddy_service` key.

## A Gluetun client is stranded and still reports healthy

**Symptom:** nothing. The container says `Up` and `healthy` right up until the next boot, when
it fails to start.

**Cause:** a VPN member shares Gluetun's network namespace. Docker resolves
`network_mode: container:gluetun` to Gluetun's **container ID** at creation, so recreating
Gluetun leaves every client in a *different* compose project pointing at an ID that no longer
exists. On 2026-08-21, recovering from the `lir` drive-loss incident, a `gluetun.yaml` run
stranded `qbittorrent`, `nzb` and `pinchflat`; all three reported healthy
(`tests/test_gluetun_vpn_member_index.py:17-21`).

**Fix:** membership is **two edits**, and they must agree.

1. `vpn: {enabled: true, container: gluetun}` on the service block.
2. The bare service name in that host's `vpn_members:` list (`dub.yaml:2880`).

`gluetun.yaml:125-132` filters claimants through `selectattr('value.vpn', 'mapping')`, so a
bare `vpn: true` is not a claim — it reads as unclaimed and the assert at `gluetun.yaml:174`
fires on the index side. `tests/test_gluetun_vpn_member_index.py` asserts **both** directions
statically, including the one `gluetun.yaml` structurally cannot check at runtime (a flagged
service that is not in the index) — finding those would mean a hostvars walk, which evaluates
every `hashi_vault` lookup on the host and dies on the first missing secret (ADR-0038, amended
2026-08-21).

## Every container monitor on one host reports DOWN

**Cause:** Uptime Kuma binds a docker-type monitor to a daemon by **numeric** id, never by
hostname, so `kuma_docker_host_id` is per-host inventory data. The old `| default('1')` aimed
every container check on `ie01`/`ie02`/`pie02` at `pie01`'s daemon: Kuma looked for the
container on the wrong host, never found it, and reported DOWN. That single fallback accounted
for **~140 of DUB's 142 red monitors** and hid for months, because `1` is a plausible id
(`roles/service/defaults/main.yaml:102-111`).

**Fix:** read the real id — `SELECT id, name FROM docker_host;` — and set it in the host's
inventory vars. `tasks/main.yaml:37-47` refuses to emit monitors when it is empty, but a
*wrong* id is still accepted, so note in the inventory comment whether the value is **measured
or predicted** (`dub.yaml:1610-1615` is a live example of a predicted one).
`tests/test_kuma_docker_host_id.py` covers three residual modes the runtime assert cannot:
a duplicate id, an id on a host outside `docker_hosts`, and `docker_hosts`/`autokuma` drift.

## The converge fails on a Homepage widget label

**Symptom:** `assert` fails naming `homepage.widget.password` (or `key`, `token`, `secret`,
`apiKey`, `passphrase`), with no value shown.

**Cause:** working as designed. Those labels are plaintext in `docker inspect` for anything
holding the Docker socket — on these hosts that is Homepage, Dozzle, the Beszel agent,
AutoKuma, Alloy and WUD, several of which ship data off-host.

**Fix:** move the value into that site's `homepage_widget_secrets` map and reference it from
`homepage_secrets:` on the service. The role emits Homepage's `{{HOMEPAGE_FILE_*}}`
indirection instead. Two sub-traps in `tasks/main.yaml:165-190`: an **empty** value is not a
credential and passes on purpose (bazarr ships `homepage.widget.key: ""` while disabled), and
every value is compared **as a string**, because an unquoted `key: 8675309` and an unquoted
`password: no` (YAML 1.1 boolean `False`) both reach the label as plaintext and a
`selectattr('value','string')` check would have waved both through.

## The tile never appears on Homepage

Three independent preconditions, only the first obvious:

1. `homepage.enabled: true` in the block.
2. The host is in the `docker_hosts` group. Homepage discovers containers over the Docker
   socket, and a host outside that group is never polled. It is also the group that gates Kuma
   label emission (`defaults/main.yaml:113-118`).
3. `homepage.group` exists in `homepage_config.layout` on the Homepage host. **A group missing
   from the layout does not error — its tiles render on every tab**, which reads as a bug
   somewhere else entirely (`dub.yaml:1116-1120`).

A bad `icon` renders a blank tile, not an error. `sh-` is selfh.st, `mdi-` is Material Design
Icons; prefer the `-light` variant on this dark theme, and check it exists first:

```bash
# [mac]
curl -s -o /dev/null -w '%{http_code}\n' \
  https://cdn.jsdelivr.net/gh/selfhst/icons/svg/NAME-light.svg
```

## The `environment:` block provably does nothing

**Cause:** frontend builds (Vite, SvelteKit, Next static export) compile their public env vars
into the JS bundle at **build** time, and the upstream published image was built with them
unset. Setting them in inventory is decorative, and worse, reads to the next person as though
the service were configured.

**Fix:** check the project's `Dockerfile` for `ARG`/`ENV` pairs consumed during the build. If
the defaults are acceptable, omit `environment:` entirely and comment why it is absent —
`dub.yaml:1768-1795` (VERT) is the model. If they are not, the only route is building the image
with the build args you want and publishing it to `gitea.stump.rocks`.

## The container crash-loops with an exec format error

**Cause:** `pie01`/`pie02` are arm64 and the image publishes amd64 only. The deploy converges
cleanly first, so it reads as an application bug.

**Fix:** check the manifest during the preflight, before choosing the host. The role
deliberately never emits a `platform:` key — a multi-arch image is resolved by the host on its
own. An amd64-only image belongs on an x86 host.

## A private registry package fails to pull

DUB hosts pull `gitea.stump.rocks` images **anonymously**; there is no host-side registry auth
anywhere in the repo, and `.gitea/scripts/check-image-pullability.py` checks anonymously for
exactly that reason. A package flipped private fails the deploy with no obvious cause. Keep
packages for deployed services public, or treat adding registry auth as its own piece of work.

Separately: Gitea's built-in `GITHUB_TOKEN` is rejected by Gitea's own container registry, so a
workflow that *pushes* an image needs `vars.REGISTRY_USER` + `secrets.REGISTRY_TOKEN`.

## The merge went green and deployed nothing

`detect-deploy-targets.py` maps changed files to targets, and several outcomes are legitimately
empty. Read what it prints before assuming your change shipped:

```bash
# [mac] in the ansible checkout
python3 .gitea/scripts/detect-deploy-targets.py "$(git merge-base origin/main HEAD)" HEAD
```

- A change inside one service's per-host block converges **only** that service.
- A change to `roles/service/**` fans out to **every** service playbook.
- A change to an inventory *outside* a per-host service block (vars, `children:`) cannot be
  attributed, so it falls back to every enabled service for that site **plus the base phase** —
  host-level vars are applied only by `playbooks/host.yaml`. Before that fallback existed, a
  merged quota fix went green and never reached the host (`detect-deploy-targets.py:620-632`).
- Some plays never converge on merge at all. `.gitea/converge-skip.txt` lists them, one stem
  per line, with the reason above each.

## `:latest` does not redeploy itself

`pull: always` refreshes the image on the next converge **of that service**, and nothing in the
ansible repo notices that an upstream project published a new image — `converge-apps` fires
only on changes in the ansible repo. Pin a real tag when deploys should be explicit; accept
drift when they should not. Either way write which one you chose in the runbook.

## `.gitleaksignore` is empty, and re-pinning it is the wrong instinct

Older notes describe a procedure for re-pinning `.gitleaksignore` fingerprints after an
inventory insert shifted line numbers. **That procedure is dead.** The file is 14 lines of
comment with zero active pins as of commit `2708043f`, and its own text says so: entries are
`file:rule:LINE` fingerprints that rot whenever anything above them moves, it was re-pinned
thirteen times for two Outline keys before they were finally rotated, and *if you find yourself
re-pinning, rotate instead*. False positives go in `.gitleaks.toml`'s allowlist, never here.

Carry the lesson, not the procedure: a line-pinned ignore file is a tax due on every edit above
it, so remove the secret rather than maintain the pin.
