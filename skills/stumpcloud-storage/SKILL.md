---
name: stumpcloud-storage
description: Diagnose and operate StumpCloud storage — the ZFS pools on the DUB hosts and the Garage S3 cluster behind s3.stump.rocks and the pages.stump.rocks sites. Use it whenever drives drop, a pool goes DEGRADED or SUSPENDED, a guest freezes on I/O, disks read as all zeros, SMART or drive temperatures look wrong, a scrub or a dataset quota needs checking, or anyone is about to run zpool create, zpool import -f, or zpool clear. Use it too when a pages.stump.rocks site returns 502, S3 writes fail, a backup cannot reach its bucket, or someone asks whether the cluster stays writable while a node is taken down. Covers which layer actually failed — power, signalling, or data path — which machine holds each signal, how to translate between the five names one drive has, and why the Garage daemon container is not the one named garage.
---

Two domains, one rule: **read the layer that owns the thing you are asking about.** Almost none of
the three weeks and two OMGs this storage has cost went to hard problems — they went to reading the
wrong layer. Two facts every section below depends on:

- **The guest owns the pool; the hypervisor owns the hardware.** Pool state, vdev counters and ZED
  events live in the guest. SMART, link errors, PHY counters and bay maps live on the hypervisor.
  Neither machine can see the other half.
- **The Garage daemon container is not the one named `garage`.** `garage` is the web UI, and its
  healthcheck is green while the daemon is dead.

## Pools and who owns them

Dated snapshot, 2026-08-30. Re-derive before acting on it — one line each.

| Pool | Layout | Owned by | Hardware on | Declared in inventory |
|---|---|---|---|---|
| `voltron` | raidz2, 10 x 18 TB | ie02 guest | dagda | no |
| `critical` | mirror, 2 x 18 TB | ie02 guest | dagda | no |
| `tank` | raidz1, 3 x 16 TB | ie01 guest | lir | yes |

```sh
# [guest] ground truth — layout, health, members
ssh joestump@ie02.stump.rocks 'sudo zpool list; sudo zpool status'
# [mac] what Ansible knows about, in a stumpcloud/ansible checkout
grep -n -A6 'host_zfs_pool:' dub.yaml
```

**Only `tank` is declared.** `roles/host/tasks/storage.yaml` runs solely when `host_zfs_pool.name`
is defined, so `voltron` and `critical` get no ZED email alerting, no quota convergence and no
auto-import enablement from Ansible. The `ie02` block in dub.yaml records why. That gap is
deliberate, not a bug — closing it is a change, not a fix.

## Reaching the hosts

```sh
# [mac] hypervisors have no joestump account, so the bare form is denied; guests take
# the default login and need sudo for everything storage-related
ssh root@dagda.stump.rocks id -un   # -> root ; same on lir
ssh joestump@ie01.stump.rocks id -un   # -> joestump ; same on ie02
```

`ansible_user: root` in the inventory is Ansible's per-host connection identity, a separate thing
from what your own SSH lands as. Verified live 2026-08-30 with the commands above.

## Start here — the four-number census

Kernel-level drive events the guest never sees. Run on the **hypervisor**.

```sh
# [host] on dagda or lir
for p in "FAILED Result" "not ready" port_remove "hard resetting"; do
  printf '%s: %s\n' "$p" "$(journalctl -k -b | grep -ci "$p")"; done
```

**Zero on all four is the pass.** dagda read 25 `FAILED Result` at boot while its 12 V distribution
was failing, and reads 0 since the PSU replacement. Then run `sudo zpool status -v <pool>` on the
owning guest for state, per-vdev READ/WRITE/CKSUM, and the `errors:` line.

## Which layer actually broke

This table is the skill. A backplane, an HBA, and every cable were replaced during the voltron
incident before anyone applied it, because *drives are dropping* reads as a cable problem and the
kernel was saying something else entirely.

| Symptom | Layer | Do this |
|---|---|---|
| `Logical unit not ready, cause not reportable` | **Power** — the drive is browning out | Look at 12 V distribution, not the cable |
| `SError: { UnrecovData 10B8B BadCRC }` plus `interface fatal error`, drive answers `DRDY` | **Signalling** | Swap that drive's data cable |
| `PHYRdyChg`, `LinkSeq`, `DevExch`, `SATA link down` | **Signalling** — link will not train or hold | Cable, connector, backplane |
| PHY `invalid_dword` / `loss_of_dword_sync` climbing | **Signalling** | Reseat, then suspect the controller |
| Every port `SStatus 0` **and** clean AHCI/PCI status bits | **Data path** — below the OS | Backplane or shared cabling. Rebooting will never fix it. Clean means `lspci -vvv -s <ctrl>` reads `-` on every error bit |
| Drive absent, **no kernel messages at all** | Not connected | Reseat power and data. This is not a failure |
| Failures on physically **adjacent** bays | **Power** — shared chain | Rebalance the 12 V feed |
| Failures confined to specific **lanes** | **Signalling** — shared cable or port | Swap that cable |

Three discriminators that collapse the search fast:

- **Adjacency.** Affected drives sitting next to each other physically but spanning *both* data
  cables exonerates every cable in one observation. This is what finally cracked voltron.
- **Vendor count.** Five drives from three manufacturers failing in the same second is not media
  failure. Count vendors before ordering replacements.
- **Lights on, no link.** Power reaching the drives eliminates the PSU and the power leads at a
  stroke, promoting the backplane from possible to leading.

## Where each signal lives

| Signal | Read it on |
|---|---|
| SMART attributes, drive temperature, power-cycle history | hypervisor |
| Kernel census, SATA/SAS link errors, HBA PHY counters, SES bay map | hypervisor |
| `qmpstatus` — frozen versus running | hypervisor |
| Pool state, vdev error counts, scrub progress, `zed` events | guest |

**SMART is unreadable inside the guest even with full raw passthrough.** The guest sees
`QEMU HARDDISK` with a synthetic serial; `smartctl -d sat` returns `unsupported scsi opcode`,
because there is no real drive behind the emulation to forward ATA PASS-THROUGH to. Any monitor
polling one side is blind to half the failure modes — part of why both August 2026 OMGs went
undetected for hours.

## One drive, five names

| Namespace | Example | Stable |
|---|---|---|
| Serial | `ZR5A275R` | **yes** — the only real physical identity |
| WWN | `wwn-0x5000c500e4af3762` | **yes** — what ZFS uses on the hypervisor |
| Bay / phy | `7` | **yes**, while cabling is unchanged |
| QEMU serial | `drive-scsi5` | guest-only; ZFS names it `scsi-0QEMU_QEMU_HARDDISK_drive-scsi5` |
| `/dev/sdX` | `sde` | **no** — reshuffles every boot |

**Never carry an `sdX` name across the host/guest boundary.** `/dev/sdb` on dagda and `/dev/sdb` on
ie02 are different physical drives; the two kernels enumerate independently. Translation, the SES
bay walk, the locate LED and the inventory slot-collision trap are in `references/drive-identity.md`.

## The ZFS traps

Recognition cues only. `references/zfs-incidents.md` carries each incident with the observation that
identified it, the post-hardware verification procedure, and how to read drive temperatures without
calling a false runaway.

- **A frozen guest reports as `running`.** QEMU pauses a VM on disk I/O error and `qm list` still
  says `running`. ie01 sat frozen and unnoticed for 7h55m. On the hypervisor,
  `qm status <vmid> --verbose | grep -E '^(status|qmpstatus)'`; `qmpstatus: io-error` is the case.
- **Disks reading as all zeros are usually dead file descriptors, not dead platters.** A guest
  rebooted inside a surviving QEMU process sees correctly-sized disks full of zeros. It looks
  exactly like total loss and is not. **Write nothing** until you have checked.
- **Two hosts must never import one pool.** Every voltron member is passed through to ie02 *and*
  still visible to dagda, so a hypervisor can import a pool it does not own — dagda did on
  2026-08-21, while ie02 was running. Two ZFS instances on one set of disks is unrecoverable.
- **Nothing in Ansible creates a pool, on purpose.** `roles/host/tasks/storage.yaml` fails loudly
  when a declared pool is absent, and `tests/test_host_storage_pool_guard.py` pins that. If you find
  yourself typing `zpool create` against a pool that merely will not import, stop.
- **A quota wall is not a full pool.** `tank/media` hit its quota at 0 B available with 8.03 T free
  in the pool, silently failing every write from the download clients, the *arr import path and
  Jellyfin. `zfs get -r quota,available <pool>` separates the two.

## Garage

- **The daemon is `s3` on ie01 and `s3-dtw` on lake01.** The container named `garage` is
  `garage-webui`, and `docker ps` reports it healthy because its healthcheck probes only its own
  port. That says nothing about the daemon. The service role names a container after its `dns` key,
  falling back to `name` — `roles/service/defaults/main.yaml:36`, `roles/service/tasks/main.yaml:373`.
- **The image is distroless.** No shell; the CLI binary is `/garage`, so `sudo docker exec s3 /garage
  status`. Ports are not published — probe the container IP, never `localhost`.
- **Write quorum is `floor(RF/2)+1`, and RF is 2 today**, so *both* nodes must be up for any write.
  Deliberately taking one down halts writes cluster-wide — it has happened twice, on 2026-06-28 and
  2026-07-11.
- **Membership is by shared `rpc_secret`, not by the service being called garage.** Another `garage`
  block in the inventory carries a different secret path, so counting blocks miscounts the cluster.

`references/garage-cluster.md` carries the derivations, the two wedge signatures, and why the
published runbook must not be used as the source of truth. For a live Pages 502 the remediation
ladder already exists at `.claude-ops/playbooks/fix-garage-pages.md` in `stumpcloud/ansible` —
follow it rather than reinventing it.

## Long-running work

Scrubs and load tests outlive an SSH session. **Reconnect on every poll** — a monitor holding one
long-lived connection gets closed by the `ControlPersist` master mid-run and then reports stale
numbers forever, which looks like a healthy plateau. **Run the work under `systemd-run --unit=`**,
not a bare background job, so it survives disconnection and stops by name; `pkill -f` matches your
own command line. A ZFS scrub ETA extrapolates from the scan phase before any reads issue, so a
wildly pessimistic first estimate is normal.

## When it was an incident

Real outages here get an OMG. Why the action-item discipline matters in this domain specifically:
the `werror=report,rerror=report` passthrough fix was diagnosed on ie02, validated under fire, and
filed — then not applied to ie01, which froze for eight hours from the identical cause twelve days
later. **A filed action item is not a mitigation until it ships.** Both hosts carry it now, but
nothing in Ansible converges it (`grep -rn werror` returns nothing), so it is hand-set in
`/etc/pve/qemu-server/<vmid>.conf` and a re-provision drops it. Verify after one.

## Further reading

In `stumpcloud/ansible`: `docs/adrs/ADR-0020` (passthrough), `ADR-0008` and `ADR-0025` (Garage, both
accepted), `ADR-0057` and `ADR-0059` (**both `status: proposed`** — designs, not deployments). In
Outline: the [dagda Drive Array runbook](https://outline.stump.rocks/doc/dagda-drive-array-xdjefXPChg)
for the live bay map and serials, and the two August 2026 OMGs —
[ie01/lir](https://outline.stump.rocks/doc/2026-08-20-ie01-froze-then-all-five-of-lirs-passthrough-disks-lost-sata-link-huge-omg-zOy9Pa71gi)
and [ie02/dagda](https://outline.stump.rocks/doc/2026-08-08-ie02-froze-on-io-error-dagda-slot-10-kept-dropping-a-voltron-disk-big-omg-bTgdeYGInl).
