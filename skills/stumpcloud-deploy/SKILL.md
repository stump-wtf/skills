---
name: stumpcloud-deploy
description: >
  Deploy a new service onto StumpCloud, or change, move, or retire an existing one: a Docker
  container behind Caddy, driven by an Ansible service-role playbook plus a declarative
  inventory block, with a runbook and a Homepage tile. Use it whenever someone says deploy
  this, put this on StumpCloud, self-host this, spin this up on ie01 or pie02, get this behind
  Caddy, or hands over a repo or Docker image and expects it live at something.stump.rocks.
  Also the maintenance half: bump an image, add a database or OIDC, move a service between
  hosts, rename it, change its domain, or retire it and its DNS record. Covers host selection,
  the image preflight, the service_config key surface, the local gauntlet, and the traps that
  fail late: Gluetun membership, Kuma monitor binding, Homepage widget credentials, and
  orphaned Route53 CNAMEs. Reach for it even for just write me an Ansible playbook for X.
---

# Deploying and retiring services on StumpCloud

**Repo:** `stumpcloud/ansible` on Gitea. **Issues:** `stumpcloud/stumpcloud`.

**Merging the PR is the deploy.** The `converge-apps` lane diffs the merge, maps changed files
to playbooks, and runs each against every host in its group over SSH — with no `--limit`
(`.gitea/workflows/ci.yaml:883`). Exceptions are one playbook stem per line in
`.gitea/converge-skip.txt`: operator-only plays CI deliberately holds no credentials for.

## Step 0 — classify what you are looking at

A path under `playbooks/services/` does not mean a service. Snapshot at `b5f7c492`, 140 files:

| Shape | Count | Marker | Copy it? |
|---|---|---|---|
| Service role only | 73 | `include_role: name: service` + `service_config:` | **Yes.** The modern pattern. |
| Direct compose only | 25 | `docker_compose_v2` and no `service_config` | No. Predates the role. |
| Both | 3 | role play plus a hand-rolled compose task | Only if you know why. |
| Not a service | 39 | DNS records, CI secrets, OpenBao identities, host tasks, and the `import_playbook` aggregators (`apps.yaml`, `core.yaml`, `db.yaml`, `ai.yaml`, `office.yaml`) | No. |

```bash
# [mac] in the ansible checkout — re-derive, do not trust the table above
ls playbooks/services/*.yaml | wc -l
grep -l service_config playbooks/services/*.yaml | wc -l
grep -l docker_compose_v2 playbooks/services/*.yaml | wc -l
```

## The shape of a change

| File | What goes there | Enforced? |
|---|---|---|
| `playbooks/services/<name>.yaml` | ~12 lines: a play that includes the `service` role | yes, by ansible-lint in CI |
| `<site>.yaml` at the repo root | The `service_config` block plus a `children:` group entry | yes, by `tests/test_service_config_defined.py` |
| `docs/playbooks/<name>.md` | The runbook. Docusaurus autogenerates the sidebar (`docs-site/sidebars.ts:22-25`), so no sidebar edit | **no** — 130 runbooks against 140 playbooks today |
| `docs/playbooks/README.md` | One catalog tile, alphabetical | no |
| `CLAUDE-OPS.md` | One row under that host in `## Service Inventory` | no |

Homepage needs no edit: tiles come from Docker labels the role emits. `AGENTS.md` is a different
document, not a twin of `CLAUDE-OPS.md` — over twice the length — so never edit one and call the
other done. More than five files and you have drifted into a second concern.

## Read the neighbours, never a prose host list

Every prose inventory here is stale. The checkout's own in-tree authoring notes still describe
PDX as `ext01, int01, gpu01, media01, nuc01, nuc02`; `pdx.yaml` has exactly one host, `nuc01`.
Derive instead, then copy the conventions of two or three neighbours of the same shape:

```bash
# [mac] hosts and groups; and how loaded a host is = how many groups claim it
# (2026-08-30: ie01 57, ie02 26, pie01 17, pie02 14)
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES pipenv run ansible-inventory -i dub.yaml --graph
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES pipenv run ansible-inventory -i dub.yaml --graph \
  | grep -c -- '--ie01$'
```

## Preflight the image before writing any config

Four questions, each of which silently produces a broken deploy if guessed:

```bash
# [mac] with Docker running
docker run -d --name smoke-NAME -p 18080:GUESSED_PORT IMAGE
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:18080/
docker inspect -f '{{.State.Health.Status}}' smoke-NAME
docker image inspect IMAGE -f '{{.Os}}/{{.Architecture}}'
docker rm -f smoke-NAME
```

- **Container port, not host port.** README examples show the host side of `-p 3000:80`.
- **Architecture.** `pie01`/`pie02` are arm64. An amd64-only image converges clean, then
  crash-loops with an exec format error that reads as an application bug.
- **Does its baked `HEALTHCHECK` work?** Many images call `curl` in a base that has none, so
  the container sits `unhealthy` forever and reddens Homepage and Kuma. Leave `healthcheck:`
  empty when the baked one works; supply one when it does not.
- **Is config read at runtime at all?** Vite/SvelteKit/Next bake `PUB_*` vars in at build time
  (`dub.yaml:1768-1795`, the VERT block), so an `environment:` block would be decorative.

Do not write your own registry checker. CI owns `.gitea/scripts/check-image-pullability.py`
(gated by `tests/test_image_pullability_gate.py`); run it. It checks **anonymously** on
purpose, because the fleet pulls anonymously — a Gitea package flipped private fails the same way.

## Choose the host deliberately

Put a service where its database already is; put cheap tenants (one nginx process, no mounts)
on the Pis and keep them arm64-clean; read the inventory comments, which record the current
state of play. Then **say why in a comment on the block** — `dub.yaml:1756-1761` is the model:
the reason, the load on the alternative, and why there is no `platform:` key.

## The playbook is thin by design

```yaml
---
# Governing: ADR-0003 (service patterns), ADR-0042 (external/internal domain split)
# <Name> — one line on what it is and why it is here.
- name: <Name>
  hosts: <group>
  tags: [<name>, apps]
  tasks:
    - name: Deploy <Name>
      ansible.builtin.include_role:
        name: service
      vars:
        service_config: "{{ <inventory_key> }}"
```

`hosts:` is a **group**, added under `children:` — never a bare hostname — so the service can
run on two hosts during a migration without editing the play. One playbook per service; the
Gluetun stack is the single sanctioned exception, its members sharing a network namespace.

Anything needing a secret or an OIDC client provisioned first is a **two-play** playbook
(ADR-0027): play 1 mints and writes to OpenBao with `cas: 0`, play 2 deploys. Copy
`playbooks/services/freshrss.yaml` wholesale rather than reinventing the ordering.
`references/service-config.md` has the key surface and four worked inventory blocks.

## Five things that gate the happy path

Each fails in a way that does not name its cause. Diagnosis in `references/traps.md`.

1. **Gluetun is a two-edit change.** `vpn: {enabled: true, container: gluetun}` on the service
   block **and** the bare name in that host's `vpn_members:` list (`dub.yaml:2880`). A bare
   `vpn: true` fails `selectattr('value.vpn','mapping')` in `gluetun.yaml:128` and reads as
   unclaimed. Guard: `tests/test_gluetun_vpn_member_index.py`.
2. **Never put a literal on `homepage.widget.{key,password,token,secret,apiKey,passphrase}`.**
   The role asserts and fails the converge (`roles/service/tasks/main.yaml:165-175`). Use
   `homepage_secrets:` plus that site's `homepage_widget_secrets` map.
3. **`kuma_docker_host_id` has no safe default.** Read the real id out of Kuma
   (`SELECT id, name FROM docker_host;`). The old `| default('1')` accounted for ~140 of DUB's
   142 red monitors (`roles/service/defaults/main.yaml:102-111`).
4. **`oidc:` in an inventory block is not a `service` role key.** `grep -rn oidc roles/service/`
   returns nothing; the playbook consumes it as `conf.oidc.callback_urls`.
5. **An inventory edit deploys the whole group at once.** No `--limit`, ever. Plan migrations
   as separate merges.

## Run the gauntlet locally

```bash
# [mac] in the ansible checkout — the same targets CI runs
make lint
make test
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES pipenv run ansible-playbook \
  -i dub.yaml playbooks/services/NAME.yaml --syntax-check
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES pipenv run ansible-playbook \
  -i dub.yaml playbooks/services/NAME.yaml --list-hosts
python3 .gitea/scripts/detect-deploy-targets.py "$(git merge-base origin/main HEAD)" HEAD
```

`--list-hosts` catches the group you forgot to add: it reports zero hosts, and a play with no
hosts is a silent no-op that looks like a successful deploy. `detect-deploy-targets.py` prints
the blast radius as `{"base": [...], "apps": [...]}`; more than your own playbook means the
change fanned out. Every trap above already has a guard in `tests/`, so a green local
`make test` is meaningful — do not reimplement any of those checks in a new script.

## PR, watch CI, verify live

Normal PR flow, label `feature`. Because the merge is the deploy, watching CI to green is not
optional here — and then check the **service**, not the check: a converge succeeds happily
while the container crash-loops. Finish on the Homepage tile and the Kuma monitor.

```bash
# [mac] after the merge converges
dig +short NAME.stump.rocks
curl -sI https://NAME.stump.rocks | head -3
```

## Changing, moving, retiring

`references/lifecycle.md` carries this in full. The one fact that belongs here because it is
silently destructive: **deleting a service block does not delete its Route53 CNAME.** The role
converges the record to absent only while a play still runs for it
(`roles/service/tasks/main.yaml:403-404`); delete the block and the name dangles forever.
`playbooks/services/dns.yaml:204-218` is the graveyard of 17 such orphans from one PR. Retire
with `enabled: false` first, verify, then remove — and `enabled: false` **preserves the
database** (`tasks/main.yaml:69-85`, ADR-0040), which is the opposite of what older notes claim.

## Reference material

- `references/service-config.md` — the 43-key surface plus four worked blocks. Open while writing one.
- `references/traps.md` — symptom to cause to fix. Open before pushing, and when a converge
  fails in a way that does not fit the change you made.
- `references/lifecycle.md` — move, rename, re-domain, retire, and the Route53 half.
