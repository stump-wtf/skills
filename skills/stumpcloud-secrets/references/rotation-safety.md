# Rotation safety

Rotating a StumpCloud credential is a **two-step act with a silent failure mode in between**, and
for a minority of keys it is not a rotation at all — it is data destruction. Read this before
writing a new value over a live one.

## 1. Writing to OpenBao does not reach the running container

ADR-0006 (`docs/adrs/ADR-0006-ansible-provisioned-secrets-openbao-docker-file-delivery.md`,
status accepted) chose deploy-time file delivery: Ansible reads the secret at **deploy** time,
writes a host file, and Docker Compose mounts it at `/run/secrets/<name>` on tmpfs. Containers
never talk to OpenBao. Line 45 of that ADR states the consequence plainly — rotation still
requires a redeploy to re-materialize the files.

So the sequence is:

1. Write the new value into OpenBao.
2. **Re-run the service playbook.** Until you do, the container is still using the old value.

Between those two steps OpenBao and the fleet disagree, and nothing reports it. The failure
surfaces later, out of context, as an authentication error against a credential you believe you
already rotated. If you cannot complete step 2 in the same session, do not start step 1.

The corollary: rotation is never a `vault kv put` on its own. Anything that only performs the
write leaves the system in the worse of the two states.

## 2. No playbook will ever rotate for you

Every service playbook writes with `cas: 0` — check-and-set against version 0, meaning
first-write-only. On a re-run OpenBao answers 412, the task carries `ignore_errors: true`, and
the existing value is untouched. See `playbooks/services/paperlessngx.yaml:63-69`, where the
comment reads `cas=0: 412 = secret already exists, non-fatal`.

Two things follow:

- **A 412 from a provisioning play is success.** It means the secret was already there. It is not
  a rotation failure and not something to work around.
- **Rotation is always deliberate and always manual.** There is no automated rotator to blame and
  no scheduled job that will pick it up. If a value changed, a human or an agent changed it.

Do not "fix" a 412 by removing `cas: 0`. Without it, every converge run overwrites live
credentials with freshly generated ones and the fleet drifts apart one service at a time.

## 3. Ask the metadata first — and absent metadata means stop

ADR-0012 (`docs/adrs/ADR-0012-openbao-secret-metadata-and-rotation-safety.md`, status accepted)
put rotation constraints into KV v2 `custom_metadata`, co-located with the secret. Metadata never
contains secret values, so it is always safe to print.

```sh
# [mac] — after export VAULT_ADDR=https://vault.stump.rocks
vault kv metadata get -mount=secret ie01/lldap
```

The `custom_metadata` map carries `service`, `host`, `managed_by`, `playbook`, `description`, and
optionally `do_not_rotate`. The flag is **path-level**, not key-level, because KV v2 metadata is
per-path; when it is set, `description` names which key inside the bundle must not change and why.

Three possible answers, and only one of them is a green light:

| What you find | What it means |
|---|---|
| `do_not_rotate: "true"` | Stop. Read `description`. Rotating this destroys data. |
| `do_not_rotate: "false"` | Someone assessed this path and said rotation is safe. Proceed to section 1. |
| no `custom_metadata`, or the key absent | **Unknown. Treat as stop**, then apply section 4. |

**Absent metadata is not permission.** Coverage is thin and the gap is measured, not guessed.

> **Dated snapshot, 2026-08-30.** Of 47 paths under `secret/ie01`, 9 carry any `custom_metadata`,
> 4 carry `do_not_rotate: "true"`, 3 carry it explicitly `false`, and 2 annotated paths omit the
> key entirely. The 38 unannotated paths include ordinary services whose safety nobody has
> assessed.

Re-derive it rather than trusting that number [mac]:

```sh
for p in $(vault kv list -format=json -mount=secret ie01/ | python3 -c \
    "import json,sys;[print(x) for x in json.load(sys.stdin)]"); do
  vault kv metadata get -format=json -mount=secret "ie01/$p" \
    | python3 -c "import json,sys;m=json.load(sys.stdin)['data'].get('custom_metadata') or {};print('$p', m.get('do_not_rotate','UNANNOTATED'))"
done
```

ADR-0012's own premise — that rotation tooling can read the flag before acting — is therefore only
partly true in practice. It works where it was applied; it proves nothing where it was not.

## 4. The key-class heuristic for unannotated paths

When the metadata is silent, classify the **key** rather than the path. There are two kinds of
secret here and they behave completely differently under rotation:

- **A credential** authenticates a caller. Rotating it invalidates a login. The blast radius is a
  redeploy and, at worst, a brief outage. Examples: `db_password`, `admin_token`, `api_key`,
  `client_secret`, `user_pass`, `jwt_secret`.
- **An encryption seed** derives keys that already encrypted data at rest. Rotating it does not
  invalidate a login — it makes existing data undecryptable. There is no rollback once the old
  value is gone, because the old value was the only copy of it.

**Presume any seed-shaped key is unrotatable until proven otherwise.** Names seen in this fleet on
paths that do carry the flag: `key_seed`, `secret_key`, `encryption_key`, `restic_password`. Treat
`*_seed`, `*_secret_key`, `*_encryption_key`, and anything a `description` calls a seed the same
way. Inspect the key names without printing values [mac]:

```sh
vault kv get -mount=secret -format=json ie01/lldap \
  | python3 -c "import json,sys;print(sorted(json.load(sys.stdin)['data']['data'].keys()))"
```

### What getting it wrong costs

`dub.yaml` carries the warning inline at the LLDAP service block, immediately above
`LLDAP_KEY_SEED` (find it with `grep -n LLDAP_KEY_SEED dub.yaml`): vault is the only source of
that seed, there is no `config.toml` on the host any more since OMG-2026-07-15, and every LLDAP
password is an opaque record bound to it. Rotating it
invalidates **every user password in the directory** — which is every OIDC login in StumpCloud, so
the recovery cannot be done through the services it just broke.

The recovery procedure exists, and its length is the honest measure of the cost:
`.claude-ops/playbooks/fix-lldap-opaque-auth.md`, 166 lines.

The same shape appears elsewhere with different data behind it. Paperless annotates
`secret_key` as breaking document encryption; Spotter annotates `encryption_key`; autorestic
annotates `restic_password`, which is the key to every backup repository — rotate that and the
backups you would restore from become unreadable at the same moment you need them.

## 5. If you decide to rotate

1. Confirm the metadata says `false`, or classify the key and satisfy yourself it is a credential.
2. Write the new value, then **immediately re-run the owning service playbook**. The `playbook`
   field in `custom_metadata` names it; that is what the field is for.
3. If the path had no metadata, write it now, in the owning playbook, immediately after the data
   write — ADR-0012 line 83 makes same-playbook co-location a MUST, and metadata written from
   anywhere else drifts away from the secret it describes. `playbooks/services/vault-metadata.yaml`
   is the sole exception and covers only orphaned `shared/*` paths with no service playbook of
   their own (ADR-0012 line 113).
4. Verify by fingerprint, not by printing: compare the value the container now holds against the
   one in OpenBao. The operator agent rules already ship that helper.

## 6. When you cannot tell

Stop and ask the operator. An unrotated credential is a known, bounded risk that has usually been
sitting there for months. A wrongly rotated seed is unbounded and, for encrypted data, permanent.
The asymmetry is the whole point of this file.
