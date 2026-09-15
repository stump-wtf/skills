# The `service_config` key surface

One inventory block per service, under its host in `dub.yaml` / `dtw.yaml` / `pdx.yaml` /
`gva.yaml`. The role reads it as `service_config` and derives every Compose, Caddy, Route53,
Homepage and Kuma detail from it.

## The authority is the defaults file, and only the defaults file

`roles/service/defaults/main.yaml` is the only correct list. Everything else has drifted:

- Its **own header comment** (lines 9-15) names 36 keys. The file reads **43**. Missing from
  the comment: `domain`, `pid`, `pull`, `recreate`, `shm_size`, `timeout`, `ulimits`.
- `docs/playbooks/service-role.md:139,220` still documents a `watchtower` key and a
  `com.centurylinklabs.watchtower.enable` label; `grep -rn watchtower roles/service/` returns
  nothing. `docs/specs/SPEC-service-deployment-patterns.md` carries the same dead surface.

Re-derive the list instead of trusting the table below (snapshot 2026-08-30, 43 keys):

```bash
# [mac] in the ansible checkout
grep -rhoE 'service_config\.[a-zA-Z0-9_]+' roles/service/ | sed 's/service_config\.//' | sort -u
```

No test guards that table — this repo cannot run `stumpcloud/ansible`'s pytest suite. The four
worked blocks below are the durable part; a real inventory block ages better than a key table.

### Identity, placement and storage

| Key | Default | Notes |
|---|---|---|
| `name` | — | Container name and the base for everything else. Empty fails the guard at `tasks/main.yaml:19` |
| `dns` | `name` | Subdomain; produces `<dns>.<domain>` |
| `domain` | site external domain | Publish under a different zone. Triggers the stale-CNAME prune — see `lifecycle.md` |
| `image` | — | Full reference including tag |
| `port` | `8080` | **Container** port Caddy proxies to, never a host port |
| `enabled` | `true` | `false` soft-tombstones: container and CNAME absent, database and disk preserved |
| `data_dir` | `name` | Directory under `paths.local` |
| `dirs` | `[]` | Subdirectories created under the data dir, owned `puid:pgid` |
| `volumes` | `[]` | Compose mounts, written in full |
| `puid` / `pgid` | `3000` / `3001` | Set to `0` only when the image insists |
| `user` | — | `"uid:gid"` for images that ignore PUID/PGID |

### Networking and routing

| Key | Default | Notes |
|---|---|---|
| `networks` | `[caddy]` | `[]` on a host with no Caddy — the default references a network that would not exist (`dub.yaml:638-644`) |
| `ports` | `[]` | Host publishing. Rare; Caddy is the normal ingress |
| `caddy.enabled` / `dns_cname.enabled` | `true` | Turn Caddy off for anything with no web surface; turn the CNAME off on the losing host during a migration |
| `aliases` | `[]` | Extra FQDNs that get their own CNAME and redirect to the primary |
| `vpn` | `{enabled: false, container: gluetun}` | Must be a **mapping**; see `traps.md` |

### Runtime and integrations

| Key | Default | Notes |
|---|---|---|
| `environment` | `{}` | Compose interpolates `$var` — escape a literal `$` as `$$` |
| `labels` | `{}` | Extra Docker labels. Credential-shaped `homepage.widget.*` keys are refused |
| `command` / `restart` | `[]` / `unless-stopped` | Entrypoint override; sidecars inherit `restart` unless they set their own |
| `healthcheck` | `{}` | Empty leaves the image's own `HEALTHCHECK` in place |
| `pull` / `recreate` | `policy` / `auto` | `pull: always` for a floating tag; `recreate` is forced to `always` on a retired log driver |
| `timeout` | `120` | Shutdown grace, seconds |
| `resources` / `ulimits` / `shm_size` / `gpu` | — | Limits, fd caps, `/dev/shm` (the 64 MiB default breaks vLLM and PyTorch), and the Compose GPU reservation |
| `devices` / `privileged` / `pid` / `security` | — | Escalations. Justify each in a comment |
| `db` | disabled | `type: postgres` / `mariadb` / `mysql`; role creates database, user, grants |
| `redis` | disabled | Host, port, index |
| `oauth2_proxy` | disabled | Caddy `forward_auth` for apps with no native OIDC |
| `homepage` / `homepage_secrets` | enabled / `{}` | Dashboard tile, and its widget credentials by reference — never a literal |
| `secrets` / `secrets_file_env` | `{}` | File-based secrets from OpenBao |
| `sidecars` / `depends_on` | `{}` / `[]` | Extra containers in the same Compose project |

## Keys in a block that the role never reads

- **`oidc:`** — 18 blocks in `dub.yaml` carry one, and `grep -rn oidc roles/service/` returns
  nothing. It is consumed by the **playbook**, as `conf.oidc.callback_urls`
  (`playbooks/services/grafana.yaml:66`, `filebrowser.yaml:33`, `wud.yaml:92`). Adding
  `oidc:` without a play that reads it does nothing at all.
- **`kuma_docker_host_id`** and **`vpn_members`** — host vars, not service keys. See `traps.md`.
- **`homepage_widget_secrets`** — a site var consumed by `playbooks/services/homepage.yaml`.

## Shape 1 — static site

No database, no secrets, no volumes. The image is the state; the container is disposable.

```yaml
      rack_planner:
        name: rack-planner
        image: gitea.stump.rocks/stump.wtf/rack-planner:latest
        enabled: true
        port: 8080                       # nginx-unprivileged listens high; Caddy fronts it
        dns: rack-planner
        pull: always                     # floating :latest tag, force refresh
        # Lives here rather than on ie01 because it is the cheapest possible tenant --
        # one nginx process, no mounts. No platform key: the role never emits one and
        # the image publishes a linux/arm64 manifest this host resolves on its own.
        homepage:
          enabled: true
          group: Applications            # must already exist in homepage_config.layout
          name: Rack Planner
          icon: mdi-server-network
          weight: 210
```

Comment what is **absent** and why. A block explaining why there is no `environment:` is worth
more than one listing only what is on.

## Shape 2 — database-backed

The role provisions database, user and grants from `db:`; the credential is a vault lookup,
never a literal. Put the service where its database already is.

```yaml
        db:
          enabled: true
          type: postgres
          name: freshrss
          user: freshrss
          pass: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/ie01/freshrss:db_password', url=vault_conn.url, auth_method=vault_conn.auth_method, token=vault_conn.token) }}"
          host: "{{ inventory_hostname }}"
        dirs: [data]
        volumes:
          - "{{ paths.local }}/freshrss/data:/var/www/FreshRSS/data"
        environment:
          TZ: "{{ tz }}"
          DB_HOST: "{{ inventory_hostname }}.{{ dns.wtf }}"
        healthcheck:                     # only because the image ships none that works
          test: ["CMD", "curl", "-fs", "--max-time", "2", "http://localhost:80/api/fever.php"]
          interval: 30s
          retries: 5
```

The database `login_host` is built from `service_dns_internal`, not the public domain
(`tasks/main.yaml:81`) — on a split-horizon edge host those differ.

## Shape 3 — behind forward-auth

For apps with no usable native OIDC. Caddy checks the shared `oauth2-proxy` container before
proxying and passes the identity upstream.

```yaml
      cairn:
        name: cairn
        image: gitea.stump.rocks/joestump-agent/cairn:0.0.2-dev.2
        enabled: true
        port: 8080
        oauth2_proxy:
          enabled: true                  # defaults: container auth, port 4180,
                                         # header X-Auth-Request-Email
```

Apps that **do** speak OIDC get a Pocket ID client instead, minted in play 1 of a two-play
playbook (ADR-0027) with the client secret written to OpenBao at `cas: 0`. Copy
`playbooks/services/freshrss.yaml` wholesale — the ordering between client creation, secret
write and container start is fiddly, and a half-provisioned client 404s.

## Shape 4 — sidecars and VPN-routed

Extra containers in the same Compose project go in `sidecars:`, each a full Compose service
definition; they inherit `restart` unless they set their own.

```yaml
        sidecars:
          pgadmin:
            image: dpage/pgadmin4
            container_name: pgadmin_container
            labels:                      # a sidecar gets its own Caddy vhost this way
              caddy: "{{ inventory_hostname }}-pgsql.{{ dns.wtf }}"
              caddy.reverse_proxy: "{{ '{{' }}upstreams 80{{ '}}' }}"
            networks: [caddy]
        depends_on: [pgadmin]
```

VPN-routed is the one real exception to one-playbook-per-service: members share Gluetun's
network namespace, so `gluetun.yaml` owns the whole Compose project and recreates them in
lockstep. Two edits, both required:

```yaml
      qbittorrent:
        enabled: true
        port: 8666
        vpn:                             # a mapping; a bare `vpn: true` is not a claim
          enabled: true
          container: gluetun
      vpn_members:                       # host var, dub.yaml:2880
        - qbittorrent
        - sabnzbd
```

## Secrets

Credentials come from OpenBao at deploy time; nothing sensitive is committed.

- `secrets:` writes each value to `<paths.local>/<data_dir>/secrets/<key>` mode 0600 and mounts
  it at `/run/secrets/<name>_<key>`; `secrets_file_env:` points the app's `*_FILE` variables at
  it. Prefer this to a credential in `environment:`.
- `homepage_secrets:` maps a widget field to a name in that site's `homepage_widget_secrets`.
  The role emits Homepage's `{{HOMEPAGE_FILE_*}}` indirection as the label value and **refuses
  a literal** on `key`, `password`, `token`, `secret`, `apiKey` or `passphrase`
  (`defaults/main.yaml:249-255`). A name missing from the site map renders the placeholder
  literally and the widget call 401s.
