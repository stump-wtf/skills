# The ZFS traps, with the evidence behind them

Read the section matching the situation you are in. Each is drawn from a real StumpCloud incident
and each names the observation that identified it, so you can confirm you are reading your own
situation rather than pattern-matching onto a similar-looking one. The two procedures at the end —
verifying after hardware work, and reading temperatures — are here because both were invented
during the voltron incident and both encode a mistake made twice.

## The frozen guest that reports as running

**Incident:** ie01, 2026-08-20. Frozen 10:24Z, discovered around 18:15Z by a human noticing the site
was down. **7h55m undetected.** ie02 had the same failure on 2026-08-08 at 1h54m — the detection gap
got *worse*, because the fix from the first one was never applied to the second host.

**Mechanism.** QEMU's default error policy for a disk is to pause the VM when I/O fails. A paused
guest is not crashed, it is suspended mid-instruction: its journal stops mid-sentence with no panic,
no shutdown record, and no further entries. `qm list` still reports it `running`, because from the
hypervisor's point of view the VM object exists and has not exited.

What it looks like: the guest journal ends abruptly with its last entries entirely normal, and there
is no kernel panic, no shutdown, and no reboot record. Two corroborating signals sit on either side
of the boundary — ZFS logged large I/O delays immediately before the stop (ie01's `zed` recorded
`class=delay` at **31,755 ms** and **32,571 ms** on two `tank` members), and drive link errors ran
in the *hypervisor's* log for tens of minutes prior (ie01's `ata5` threw
`PHYRdyChg / 10B8B / LinkSeq / DevExch` **69 minutes** before the freeze).

```sh
# [host] on the hypervisor — status: running with qmpstatus: io-error is the frozen case
qm status <vmid> --verbose | grep -E '^(status|qmpstatus)'
```

Once the guest is rebooted or the hypervisor restarted, that runtime state is gone and the diagnosis
becomes circumstantial. The journal-stops-mid-sentence signature plus preceding I/O delays is still
strong evidence.

**The fix, and its live status.** Every passthrough disk should carry `werror=report,rerror=report`,
which makes the guest see an I/O error and carry on instead of the hypervisor freezing the whole VM.
As of 2026-08-30 both hosts have it — `grep -c werror /etc/pve/qemu-server/100.conf` reads 12 on
dagda and 5 on lir. **Nothing in Ansible converges this** (`grep -rn werror` returns nothing), so it
is hand-set on the hypervisor and a re-provision drops it silently. Re-check after any VM rebuild.

## Disks that read as all zeros

**Incident:** ie01, 2026-08-20, around 18:18Z. Looked exactly like total data loss. Nothing was lost.

**Mechanism.** The guest was rebooted but the **QEMU process never died** — it had been running
since Aug 17 and still held open file descriptors on block devices the host kernel had detached six
hours earlier. The guest OS booted normally inside a container of dead handles and saw five
correctly-sized 14.6 T disks that read as nothing but zeros.

What it looks like, and why it is so alarming: `zpool status` returns `no pools available`; `lsblk`
shows the disks at the right size with **zero partitions**; sector 0 reads all zeros on every disk;
`sgdisk -p` reports no GPT and will cheerfully invent a fresh one in memory; and containers exit 127
because their bind mounts point at an unmounted path.

```sh
# [host] is QEMU older than the guest's uptime?
ps -o pid,lstart,etime -p "$(cat /var/run/qemu-server/<vmid>.pid)"
# [host] are the passthrough disks actually present on the hypervisor?
awk -F'[:,]' '/^scsi[1-9]/{print $2}' /etc/pve/qemu-server/<vmid>.conf \
  | xargs -I{} sh -c 'ls -l {} 2>&1'
```

QEMU start time earlier than the guest's boot, plus absent `by-id` links on the host, means **dead
file descriptors, not dead platters**.

**What to do:** stop Docker in the guest before it writes divergent state into the empty bind-mount
paths, then `qm shutdown <vmid>` to release the stale descriptors. **Write nothing** — no
`zpool create`, no `zpool import -f`, no `sgdisk`; with the disks reading as zeros, any write is
unrecoverable. On ie01 the total damage was 60 K of empty directories on the root filesystem.

## Split-brain: two hosts, one pool

**Incident:** dagda/ie02, 2026-08-21. Caught before damage.

Every voltron member disk is passed through to the ie02 guest **and** remains visible to dagda, so
dagda can see, and import, a pool it does not own. On 2026-08-21 it did, while ie02 was running with
the same disks attached. Nothing was corrupted only because ie02 had not *also* imported it. Two
independent ZFS instances writing one set of disks is not a degraded state you recover from.

**The guard is the cachefile.** After exporting from a hypervisor, confirm the pool is gone from it,
or the next boot auto-imports and you are back here.

```sh
# [host] on the hypervisor — must list ONLY rpool
zdb -C -U /etc/zfs/zpool.cache | grep -oE '^[a-z]+:'
```

Moving a pool back to its guest:

```sh
# [host] release it
zpool scrub -s <pool>                                 # cancel any scrub first
zpool export <pool>
zdb -C -U /etc/zfs/zpool.cache | grep -oE '^[a-z]+:'  # confirm it is gone
# [guest] take ownership by stable identity, not by whatever sdX landed where
sudo zpool import -d /dev/disk/by-id <pool>
```

**Then restart every container that bind-mounts the pool.** Docker resolves bind mounts at container
start, so anything that started while the pool was absent holds an empty directory forever and
silently serves nothing — the service looks healthy and returns nothing. Derive the set rather than
trusting a list: `sudo docker ps -q | xargs sudo docker inspect -f '{{.Name}} {{range .Mounts}}{{.Source}} {{end}}' | grep /voltron`.

Two expected non-errors on a voltron import: `voltron/Backups` has `readonly=on`, so ZFS cannot
create the child mountpoints beneath it, and the `voltron/.system/*` datasets are TrueNAS-era
`legacy` mountpoints not meant to auto-mount. Neither means the import failed.

## Why nothing in Ansible creates a pool

`roles/host/tasks/storage.yaml` used to guard `zpool create` on `zpool list` returning non-zero.
**A pool that is merely unimportable is indistinguishable from a pool that never existed**, from
`zpool list`'s point of view — disks detached, controller wedged, a bad boot all read as `rc != 0`.
Any host-role converge against ie01 during the 2026-08-20 outage window would have run
`zpool create` straight over the live raidz1 members. It did not fire only because the devices were
absent entirely, so `create` would have errored on missing paths.

That path was removed on 2026-08-24 when CI began converging the role fleet-wide on merge. The role
now fails loudly instead, and `tests/test_host_storage_pool_guard.py::test_role_never_creates_a_zfs_pool`
asserts the string `zpool create` appears nowhere in the role. Verify the guard rather than trusting
this paragraph: from a `stumpcloud/ansible` checkout on the Mac, `grep -rn 'zpool create' roles/host/`
must return no matches.

The reasoning outlives the fix: **an `rc`-based existence check cannot distinguish absent from
unavailable**, so never build one, and never converge a storage host whose pool is not imported.

## Power versus signalling: the voltron story

**Incident:** dagda/ie02, 2026-08-16 to 2026-08-21. Three weeks, three wrong hypotheses, three
hardware swaps.

1. **Bad backplane or drive cage** — three physical hand tests, all negative.
2. **Bad SFF-8643 cable** — HBA replaced, all three cables replaced, backplane removed. Persisted.
3. **Bad drive** — drives swapped between slots. Errors followed the *slot*, not the drive.

**What settled it.** Under load, three *physically adjacent* drives returned `Logical unit not
ready, cause not reportable` at the same instant, while PHY error counters stayed flat, zero bus
drops occurred, transport reported `DID_OK/DRIVER_OK`, and SMART was clean on all three. Adjacent
slots spanning *both* data cables share exactly one thing: the power chain. That single observation
exonerated every cable at once.

**The arithmetic.** A SATA power connector carries about 4.5 A on 12 V and is specified for one
drive. An Exos X18 draws roughly 2.0 A spinning up, 0.8 A seeking, 0.45 A idle. Six drives
daisy-chained off one feed is about 12 A at spin-up and 4.8 A merely seeking — over the connector's
rating while just busy. Contact resistance compounds it: at 10 to 20 mOhm per junction, six
junctions carrying 12 A drop over a volt, leaving the far drive near 10.9 V against an 11.4 V
minimum.

**Rail capacity and rail distribution are separate limits.** The old 600 W flex PSU had plenty of
watts and only two peripheral outputs, which is what forced six drives onto one chain. The fix was a
1000 W ATX with three independent 12 V leads plus a full re-cable onto a 9400-16i. `FAILED Result`
went 25 to 0 on a verified cold boot.

**A rejected fix worth not re-proposing.** Staggered spin-up via the HBA option ROM was recommended
during the incident and is **not available here** — direct-attached SATA drives spin up the moment
12 V is applied, before the controller initializes, and nothing in the HBA can sequence them without
PUIS or a sequencing backplane. Prefer a **star fanout** over a daisy chain: in a chain every
upstream connector carries the sum of everything downstream, in a star only the input does.

Two lessons about method rather than hardware. The failure mode was **not confined to spin-up** —
the pool suspended under sustained read load with every drive already spinning, and an early call
that only inrush breaks it was wrong and cost days. And **errors following the slot rather than the
drive** is the cheap decisive test for drive-versus-position; it takes one swap.

## Verifying after hardware work

**It must be a cold boot with every drive connected and powered.** A hot-attach staggers spin-up by
hand and produces a false pass — made twice during the voltron incident, both results worthless.

```sh
# [host] did they really power-cycle? SMART min/max resets on a true cycle, so
# yesterday's peak still showing means they never spun down
smartctl -l scttempsts /dev/sdX | grep 'Power Cycle Min/Max'
# [host] then the four-number census (expect zeros), the drive count, and attach timing
lsblk -d -ndo NAME,SIZE,SERIAL
journalctl -k -b -o short-monotonic | grep "Attached SCSI disk"
```

All members should attach within about 10 s of boot; a straggler is a power or link problem that has
not yet escalated. For a peak-draw test, read every member raw and concurrently with ARC bypassed —
the heaviest load the array sees short of spin-up. Run it under `systemd-run --unit=`, **cap it**,
and abort on temperature: under this load temps climb about 0.5 C per minute with no plateau, so an
unbounded run just cooks the drives long after the answer is known. Bound it on the Linux host,
where `timeout` exists; the operator Mac is BSD and has no such binary.

## Reading temperatures and counters honestly

**Temperature lags load by tens of minutes in both directions.** When a load test stops, temps
*rise* for minutes on chassis heat-soak before falling — the observed voltron curve ran 57 C under
load, peaked at 62 C, and reached 40 C after 41 minutes.

**Never call a thermal trend on less than about 15 minutes of samples.** A two-minute window at the
top of that curve reads as a runaway and is not one — this produced a wrong stop-the-backup
recommendation during the incident.

**Alert on the delta, not the absolute.** Some drives carry permanent scars from faults already
fixed: voltron bay 7 reads nonzero reallocated-sector, CRC and command-timeout counters, all earned
on a cable since replaced, and unmoved through a full-rate load test and a full scrub. An absolute
threshold makes that drive alert forever and trains everyone to ignore it, which is worse than not
alerting. What matters is whether a counter is increasing *now*.
