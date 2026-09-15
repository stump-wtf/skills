---
name: stumpcloud-secrets
description: >-
  OpenBao and vault operations for StumpCloud. Explains which identity a token carries, why a 403 is usually a dead or wrong token rather than a missing policy,
  where secrets live under the secret mount, how to read them without printing them, and
  what rotation really costs. Use when a hashi_vault lookup fails with Forbidden Permission
  Denied to secret, when vault or bao returns 403 or connection refused, when an Ansible
  play cannot read a secret, when converge or a deploy dies on credentials, when adding a
  secret for a new service, when rotating or deciding not to rotate a credential, and when
  someone asks which vault token am I, what can this token read, where does this service
  keep its password, or is this key safe to rotate. Covers the per-host deploy AppRole,
  human OIDC admin and personal AppRole split, vault-agent, vault-oidc-login, cas 0 semantics, and do_not_rotate metadata.
license: MIT
---

# StumpCloud secrets — OpenBao

**A 403 here is almost never a missing policy.** It is an unset `VAULT_ADDR`, an expired
token, or the wrong one of three identities. Walk the ladder in section 3 before touching a
policy body.

Secrets store: OpenBao at `https://vault.stump.rocks`, one KV v2 mount named `secret`.
`bao` and `vault` are interchangeable CLI surfaces here.

## 0. Before any vault command [mac]

```sh
export VAULT_ADDR=https://vault.stump.rocks
```

This shell is non-interactive and carries no credentials: `VAULT_ADDR` and `VAULT_TOKEN` are
both empty. `vault` falls back to `~/.vault-token`, which exists at mode 0600 — but only once
you export the address yourself.

Skip the export and the CLI dials `https://127.0.0.1:8200` and answers `connection refused`.
That reads as "OpenBao is down" and is not. Verified 2026-08-30 in this exact shell.

## 1. Never run `vault token lookup` unfiltered

**Both output forms print the live token.** `-format=json` returns it verbatim in `data.id`;
the plain table form prints it as the `id` row. Verified 2026-08-30 by comparing each against
`~/.vault-token` byte for byte — they matched. A token that reaches a transcript is burned.

Safe replacement [mac]:

```sh
vault token lookup -format=json | python3 -c \
  "import json,sys;d=json.load(sys.stdin)['data'];print({k:d.get(k) for k in ('display_name','policies','path','expire_time')})"
```

Two reasons the filter is mandatory rather than tidy: `vault token lookup` has **no `-field`
flag** (verified — it errors with `flag provided but not defined`), and `expire_time` is an
absolute RFC3339 stamp, so you never need `date -d`, which is GNU and fails on this Mac.

## 2. Which identity am I holding

ADR-0060 (`docs/adrs/ADR-0060-vault-identity-model-approle-scoping.md`, **status: proposed**)
defines three identities that must never collide:

| Identity | Auth | Reads | Token location |
|---|---|---|---|
| Host / deploy `deploy-<host>` | AppRole | `secret/{data,metadata}/<host>/*` read-write, `shared/*` read-only | `/run/vault-agent/token` |
| Human admin | OIDC via Pocket ID to LLDAP group | everything, by group policy | `~/.vault-token`, written by `vault-oidc-login` |
| Personal cred | AppRole, one per human | `secret/data/users/<user>/*` only | `~/.config/vault/agent-token` |

**"Admin in LLDAP" is not "admin token in your shell."** Admin comes only from
`vault-oidc-login`. On 2026-07-07 a personal AppRole token exported as `$VAULT_TOKEN`
outranked the operator OIDC identity and every admin read 403ed while the operator was, in
LLDAP, an admin. That is ADR-0060 section Context.

**Reality check, and it matters.** ADR-0060 is proposed, not accepted, and its own 2026-08-18
amendment records that no host runs vault-agent. Confirmed live 2026-08-30 on ie01: no
`/run/vault-agent/token`, unit `inactive`, and no inventory declares a `vault_agent` group.
So in practice two identities are live, and an operator run authenticates as the human.
Re-derive per host [host]: `ssh ie01.stump.rocks 'test -f /run/vault-agent/token && echo present || echo absent'`

## 3. The 403 ladder — stop at the first rung that explains it

1. **`VAULT_ADDR` unset.** Symptom is `connection refused` against 127.0.0.1:8200, not a 403.
   Fix: section 0.
2. **Token expired.** `deploy-<host>` mints `token_ttl: 20m` / `token_max_ttl: 1h`
   (`playbooks/services/openbao-deploy-identities.yaml:219-241`), so a long run dies at exactly
   T+3600s — converge run 6334 lost four of six shards that way (stumpcloud#245, #246).
   Ansible reports it as `Forbidden: Permission Denied to secret '<path>'`, which names the
   **secret** and never the credential; on 2026-08-02 that turned converge run 4365 into about
   40 misleading per-playbook 403s whose real cause was one six-week-old static token
   (`.gitea/scripts/vault-preflight.sh:1-11`). Check `expire_time` with the filter in section 1.
3. **Wrong identity.** The token is valid, just personal or another host. Compare `policies`
   against the table in section 2.
4. **Genuine policy gap.** Only now, and ask the live vault rather than reading a policy body
   [mac]:

```sh
vault token capabilities secret/data/ie01/lldap
```

For several paths at once, repeat the argument — the JSON-array form mangles its own output
keys into `["secret/data/ie01/lldap` and friends (verified 2026-08-30):

```sh
vault write -format=json sys/capabilities-self paths=secret/data/ie01/lldap paths=secret/data/shared/openai
```

Also worth knowing before blaming a policy: every `deploy-<host>` policy grants
`sys/internal/ui/mounts/*`, because the `kv` helpers 403 there before ever issuing the real
read (`openbao-deploy-identities.yaml:209-213`).

## 4. Path shapes, not a path list

Five stable shapes under the `secret` mount. The live inventory is `vault kv list`, never a
list in this file.

| Shape | Written by | Read by |
|---|---|---|
| `<inventory_hostname>/<service>` | that service playbook | `deploy-<host>`, converge, admin |
| `shared/<name>` | `playbooks/services/vault-metadata.yaml` or a service play | every host, read-only |
| `users/<user>/<bag>` | the human, or their personal agent | that user only |
| `ci/<consumer>` | `playbooks/services/ci-secrets.yaml`, the sole writer | converge, admin |
| `pages/<owner-slug>` | `playbooks/services/pages.yaml` | admin |

Derive the live tree [mac]: `vault kv list -mount=secret /` then `vault kv list -mount=secret ie01/`.

The host prefix is `inventory_hostname`, not a literal — a playbook that hardcodes one is how a
migrated service reads the old box's secret forever.

## 5. Reading without leaking

- **Key names only** [mac] — usually the whole question, and it prints nothing secret:
  `vault kv get -mount=secret -format=json ie01/lldap | python3 -c "import json,sys;print(sorted(json.load(sys.stdin)['data']['data'].keys()))"`
- **Metadata** is always safe to print — it carries no values:
  `vault kv metadata get -mount=secret ie01/lldap`
- **One value**, only when a command genuinely needs it, and never echoed:
  `vault kv get -mount=secret -field=db_password ie01/lldap`
- Comparing a vault value against what a container holds is a fingerprint comparison, never a
  print. The operator agent rules already ship that helper.

## 6. Adding a secret for a new service

The pattern is **two plays in one playbook file** (ADR-0027, accepted). Read the skeleton in
`docs/adrs/ADR-0027-two-play-vault-bootstrap-pattern.md` and diff it against the live
exemplar, `playbooks/services/paperlessngx.yaml:8-93`, which runs the whole thing end to end.

Four things that make a naive attempt fail:

1. **Play 1 must not reference `conf`.** Ansible evaluates the entire nested service dict when
   any task touches it, so the vault lookups fire before the secrets exist. That is the whole
   reason for two plays (ADR-0027 section Context).
2. **`cas: 0` plus `ignore_errors: true`.** First-write-only, so a re-run cannot clobber a live
   credential. The 412 it returns when the secret already exists is success, not failure
   (`paperlessngx.yaml:63-69`).
3. **The metadata write goes in the same play, immediately after the data write** — ADR-0012
   line 83 makes this a MUST, and metadata written anywhere else silently never lands.
   `vault-metadata.yaml` is only for orphaned `shared/*` paths with no service playbook.
4. **`become: false` is a play keyword and loses to a var.** The inventories set
   `ansible_become: true` in `all.vars`, so a `delegate_to: localhost` vault task escalates
   anyway, dies on `sudo: a password is required`, and reports it as an opaque MODULE FAILURE
   because the task carries `no_log`. Set `ansible_become: false` in the play vars as well —
   `playbooks/services/ci-secrets.yaml:56-65` explains it in place.

## 7. Rotation — the one rule that belongs in the body

Check `vault kv metadata get` first, and **absent metadata is not permission to rotate.**
Measured on the live vault 2026-08-30: of 47 paths under `secret/ie01`, 9 carry any
`custom_metadata` and 4 carry `do_not_rotate: "true"`. Re-derive with a loop over
`vault kv list`. Everything else — why writing to OpenBao does not reach a running container,
and the key-class heuristic for the unannotated majority — is in
`references/rotation-safety.md`. Read it before touching a live credential.

## 8. CI and converge

`.gitea/scripts/vault-preflight.sh` already owns CI authentication: AppRole login, `::add-mask::`
on the minted token, `lookup-self` validation, and re-invocation before every playbook so a
shard is never coupled to the 60-minute TTL. Call it; never hand-roll a CI login. It fails
outright in a credential-less operator shell by design — it wants `VAULT_ROLE_ID` and
`VAULT_SECRET_ID`, which this Mac does not have.

Converge authenticates as the `converge` AppRole, whose policy loops every host in the fleet
(`openbao-deploy-identities.yaml:296-322`) — which is why a playbook can 403 for you locally
and pass in CI.

## Never

- Print a token or a secret value, in any form, including a bare `vault token lookup`.
- Use the personal AppRole for admin or cross-host work.
- Hand-write a `secret/data/*` path that a service playbook owns — `cas: 0` means your write
  and its write disagree silently forever.
- Merge `sys/` capabilities into a `deploy-<host>` policy. It is written identically for the
  whole fleet in a loop, so one grant hands every host the ability to dump the backend. The
  raft-snapshot capability lives in its own `backup-<host>` AppRole for exactly this reason
  (ADR-0060, amendment 2026-08-18).
- Treat `.claude-ops/skills/vault-secrets.md` as current. Its Environment section claims
  `VAULT_ADDR` and a shared-scope `VAULT_TOKEN` are pre-injected; both are empty, and
  `~/.vault-token` carries `admin`.
