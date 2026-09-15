# Garage, operationally

Garage is the S3 layer behind `s3.stump.rocks`, every `*.pages.stump.rocks` site, restic backups,
and OpenTofu state locking. This file covers the parts the ADRs do not: which container is actually
the daemon, how to talk to a distroless binary, how cluster membership is really determined, and
what a wedge looks like.

**Do not use `docs/playbooks/garage.md` as the source of truth.** As of 2026-08-30 it disagrees with
the inventory in five places at once — it names `ie01` as the only host, `dns: s3-admin`, port 3903
as the primary Caddy target, `replication_factor: 1`, and "There is no built-in web UI". Every one
of those is wrong now. It is a good illustration of the general rule and a bad reference.

## Which container is the daemon

The service role names a container after its `dns` key, falling back to `name`:

- `roles/service/defaults/main.yaml:36` — `service_dns: "{{ service_config.dns | default(service_name) }}"`
- `roles/service/tasks/main.yaml:373` — `container_name: "{{ service_dns }}"`

So a service block whose `name` is `garage` but whose `dns` is `s3` produces a container called
`s3`, and a *different* service named `garage-webui` with `dns: garage` produces the container
called `garage`. That is exactly the arrangement on ie01.

```sh
# [mac] derive it from the inventory, in a stumpcloud/ansible checkout
grep -n -E '^\s+(name|dns):' dub.yaml | grep -A1 -E 'name: garage'
# [guest] confirm it on the host
ssh joestump@ie01.stump.rocks 'sudo docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}"' | grep -iE 'garage|s3'
```

Verified live 2026-08-30: `s3` runs `dxflrs/garage`, `garage` runs `stumpcloud/garage-webui`.

**The web UI reports healthy while the daemon is dead**, because its healthcheck probes only its own
port. `docker ps` showing `garage ... (healthy)` says nothing at all about S3. Diagnosing the wrong
container is called out in `.claude-ops/playbooks/fix-garage-pages.md` as wasting a whole session.

## Talking to a distroless image

`dxflrs/garage` ships no shell. There is no `sh`, no `bash`, no `curl` inside it. The CLI binary is
`/garage` at the image root:

```sh
# [guest] on ie01; use s3-dtw on lake01
sudo docker exec s3 /garage status
sudo docker exec s3 /garage layout show
```

`docker exec s3 sh -c ...` fails, and so does anything that assumes a shell — including a healthcheck
written as a shell string. That is why the inventory bind-mounts a **static curl** into the container
purely so `healthcheck.test` has something to execute.

**The published ports are not the S3 ports.** Only the RPC port is published to the host; the S3,
web and admin listeners are reachable only on the container network. Probe the container IP:

```sh
# [guest] the daemon's own address, then each listener
IP=$(sudo docker inspect s3 --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
for p in 3900 3902 3903; do
  curl -s -o /dev/null -w "$p -> %{http_code}\n" --max-time 3 "http://$IP:$p/"; done
```

An anonymous `GET /` on the S3 port returns a fast 403 — that is *healthy*. `000` on all three means
the daemon is not accepting connections at all.

## Membership is by shared rpc_secret, not by the name

Nodes join a cluster when they share `GARAGE_RPC_SECRET` and can reach each other on the RPC port.
Two services both called `garage` in the inventory are **not** necessarily in the same cluster, and
counting service blocks to get a node count gives the wrong answer.

```sh
# [mac] partition by secret path, not by service name
grep -n 'GARAGE_RPC_SECRET' dub.yaml dtw.yaml gva.yaml pdx.yaml
```

As of 2026-08-30 that returns three definitions across two distinct secret paths: DUB and DTW share
one path and form the live cluster, while a third block on the GVA side carries its own secret and
is `enabled: false`. Read `enabled:` as well as the secret before counting anything — a disabled
service block is inventory, not a node.

The authoritative live answer comes from the daemon itself, and it is one command:

```sh
# [guest] HEALTHY NODES, with zone, capacity, and free space per node
sudo docker exec s3 /garage status
```

## Quorum, as arithmetic rather than a snapshot

Garage's quorum depends only on the replication factor:

| RF | Copies | Read quorum | Write quorum |
|---|---|---|---|
| 2 | 2 | 1 | **2** |
| 3 | 3 | 2 | **2** |

**At RF=2 with two nodes, both must be online for any write.** Reads survive a node loss; writes do
not. That is not an incident-only property — taking either node down deliberately halts every S3
write cluster-wide, including Pages publishes, OpenTofu state locking, and any backup targeting the
bucket from a host that is still up.

The cluster has lost writes twice this way, on 2026-06-28 and 2026-07-11, and again for most of a
day on 2026-08-20 when `tank` became unimportable and took the DUB node with it.

`docs/openspec/specs/garage-three-copy-replication/design.md` designs the fix — a third node at
RF=3, which keeps the cluster writable while any one node is offline. **It is `status: proposed`
along with ADR-0057.** Check the current value before quoting a quorum:

```sh
# [mac] the declared replication factor per site
grep -n 'replication_factor' dub.yaml dtw.yaml gva.yaml pdx.yaml
```

## The two wedge signatures

**File-descriptor exhaustion.** Docker's default soft `nofile` is 1024, which the daemon exhausts
under multi-site RPC socket churn; `accept()` then halts on *every* port at once. The signature in
the daemon log is `No file descriptors available (os error 24)` on `listener.accept`. It took Pages
down on 2026-05-30 and recurred on roughly a two-week cycle until the inventory raised `nofile` to
65536 soft / 524288 hard on both nodes.

```sh
# [guest] quantitative confirmation
PID=$(sudo docker inspect s3 --format '{{.State.Pid}}')
sudo cat /proc/$PID/limits | grep 'open files'   # 1024 soft is the bug; 65536 is the fix
sudo ls /proc/$PID/fd | wc -l                    # at or near the soft limit means EMFILE
```

**A half-wedged listener.** On 2026-08-19 the admin port kept answering while the S3 port hung —
so a probe against the admin API reported a healthy daemon that could serve nothing. The inventory's
healthcheck was moved onto the S3 port for exactly this reason. Never conclude *the daemon is fine*
from the admin port alone.

**One more asymmetry worth knowing:** Caddy on the DUB node proxies only to its *local* daemon. If
that node is down, `*.pages.stump.rocks` returns 502 even though the DTW replica is healthy. The
data is safe and unreachable at the same time — that is a routing fact, not a durability one.

## Remediation lives in the ansible repo

Do not reinvent the ladder. `stumpcloud/ansible` carries a diagnosed, tiered playbook at
`.claude-ops/playbooks/fix-garage-pages.md` (Tier 1 diagnose, Tier 2 restart, Tier 3 raise the limit
and redeploy) with its verification check beside it at `.claude-ops/checks/verify-garage-pages.md`.
Two cautions it states that are easy to get wrong from the outside:

- **A 404 on a Pages path for a repo that never deployed is content, not infrastructure.** Do not
  escalate it.
- **Never re-run `layout assign` or `layout apply` on a live cluster.** If `/garage status` shows a
  node missing or the layout wrong after a restart, escalate to a human instead.

One live defect to know about before you trust the playbook's layout step to have ever run:
`playbooks/services/garage.yaml` execs into `{{ conf.name }}` at lines 197, 208, 219 and 226, which
resolves to `garage` — the web UI on ie01, and nothing at all on lake01. `failed_when: false` on the
first task swallows the error, the registered stdout comes back empty, and the
`cluster layout version: 0` condition gating the whole init block is therefore never true. The
container name it should use is the `dns` key. Verify before assuming it has been fixed:

```sh
# [mac] expect these to name the container by dns, not by conf.name
grep -n 'container:' playbooks/services/garage.yaml
```

## Fate sharing

A backup destination is only as good as the failure domain it does not share. Garage is genuinely
offsite because the cluster replicates across two sites, but it is still **one system**, and a
cluster-wide fault takes every copy with it — which is why the DUB host now writes to two unrelated
backends rather than one. Derive the current answer with `grep -n -A4 'backend:' dub.yaml` rather
than trusting any prose about it, including this paragraph.
