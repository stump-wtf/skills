# Finding the physical drive

Every procedure here answers one question: **the guest is complaining about a device — which drive
do I put my hand on?** Getting it wrong means pulling a healthy member out of a degraded array,
which is one of the few genuinely unrecoverable acts available in this domain.

The rule the body states and this file operationalizes: **an `sdX` name is meaningless across the
host/guest boundary.** The two kernels enumerate independently and both reshuffle across reboots.
Serial and WWN are the only names that mean the same thing on both sides.

## Guest device name to serial, in three hops

ZFS inside the guest names a faulted member `scsi-0QEMU_QEMU_HARDDISK_drive-scsiN`, sometimes with a
`-part1` suffix. That `scsiN` is the QEMU slot, and the hypervisor's VM config maps slots to real
`by-id` paths, which carry the serial.

```sh
# [guest] what ZFS is complaining about
sudo zpool status -v <pool>
#   -> scsi-0QEMU_QEMU_HARDDISK_drive-scsi5-part1   FAULTED

# [host] which VM, then which slot maps where
qm list
grep -E '^scsi[0-9]+: /dev/disk' /etc/pve/qemu-server/<vmid>.conf
#   -> scsi5: /dev/disk/by-id/ata-ST18000NM002J-2TV133_ZR5A275R,...
```

The trailing token of the `by-id` path is the serial. Worked example, verified 2026-08-30: guest
device `drive-scsi5` on ie02 resolves to serial `ZR5A275R`, which sits in voltron bay 7.

Confirm the serial against the drive itself before touching anything, because the VM config is a
mapping that a human wrote and hardware moves:

```sh
# [host] serial for every attached disk, from the drive not the config
lsblk -d -ndo NAME,SIZE,SERIAL
smartctl -i /dev/sdX | grep -i 'Serial Number'
```

## Bay numbers, where an enclosure exists

Not every host has one. A host behind an HBA that presents a VirtualSES exposes
`/sys/class/enclosure/`; a host on direct AHCI does not, and there are no bay numbers to read.
Check first, and expect a per-host answer:

```sh
# [host] does this machine have an enclosure at all?
ls -d /sys/class/enclosure/*/ 2>/dev/null || echo "no SES on this host"
```

Where one exists, the bay number equals the phy number, and walking the enclosure gives you the
bay-to-serial map directly:

```sh
# [host] bay -> serial, sorted numerically
for e in /sys/class/enclosure/*/; do for s in "$e"*/; do
  dev=$(ls "$s"/device/block 2>/dev/null | head -1); [ -z "$dev" ] && continue
  echo "bay $(basename "$s"): $(smartctl -i /dev/$dev | awk -F: '/Serial Number/{gsub(/ /,"",$2);print $2}')"
done; done | sort -V
```

## Blinking the locate LED

```sh
# [host] start, then stop
echo 1 > /sys/class/enclosure/*/7/locate
echo 0 > /sys/class/enclosure/*/7/locate
```

**The write is always accepted.** Whether a light actually comes on depends on whether the chassis
has the LED lines wired, and a silent success is indistinguishable from a working blink at the
shell. Confirm once per chassis by blinking a drive you can already identify, and record the answer
in the chassis runbook — do not re-derive it under incident pressure.

## Why SMART is unreadable from inside the guest

The guest does not see the drive. It sees a QEMU SCSI device whose vendor string is
`QEMU HARDDISK` and whose serial is the synthetic slot name, `drive-scsiN`. `smartctl -d sat`
returns `unsupported scsi opcode` because QEMU implements a basic SCSI command set with no real
drive behind it to forward ATA PASS-THROUGH to. There is no flag that fixes this: raw passthrough
gives the guest the *blocks*, not the *device*.

Consequences worth internalizing:

- **Temperature, reallocated sectors, CRC counts, power-cycle history and self-test results are
  hypervisor-only.** Any check that wants them has to run there.
- **The guest cannot tell a failing drive from a failing link.** It sees I/O errors either way.
  Kernel-layer discrimination is a hypervisor read; see the layer table in the skill body.
- Conversely, **pool state, vdev error counters and scrub progress are guest-only.** The hypervisor
  has no idea a pool exists on the disks it is passing through.

## The inventory is a declaration, not the running config

`passthrough_disks` in the site inventory records what *should* be attached. The hypervisor records
what *is*. For anything you are about to unplug, the hypervisor wins — always read
`/etc/pve/qemu-server/<vmid>.conf`, never the YAML.

**And do not grep the inventory for a slot number.** QEMU slot names are per-VM, so the same slot
appears under multiple hosts in one file and resolves to entirely different physical drives:

```sh
# [mac] in a stumpcloud/ansible checkout — this returns more than one drive
grep -n 'scsi4:' dub.yaml
```

As of 2026-08-30 that returns two matches, thousands of lines apart, on two different hypervisors —
and they are not even the same manufacturer. A grep that reads only the first hit points at the
wrong machine's drive. Always scope the search to the host block:

```sh
# [mac] slot map for one host only
grep -n -A20 'passthrough_disks:' dub.yaml
```

One more property of the declaration, stated in the inventory's own comment on the twelve-drive
host: **slot order is arbitrary.** ZFS imports by on-disk label, so `scsi1` is not the first member
of anything and the numeric order carries no meaning. Do not infer a vdev position from a slot
number.

## Before you pull a drive

1. Serial from the hypervisor, not from the guest and not from the inventory.
2. Serial confirmed twice, from two independent reads — the VM config and `smartctl -i`.
3. Pool state read from the guest, so you know the array can survive losing this member. A raidz2
   with one FAULTED member tolerates one more; a raidz1 with one FAULTED member tolerates none.
4. Bay confirmed by a blink you actually saw, or by the chassis runbook if the LEDs are not wired.
5. The [dagda Drive Array runbook](https://outline.stump.rocks/doc/dagda-drive-array-xdjefXPChg)
   carries the live bay map and per-drive serials; when it and a live read disagree, the live read
   wins and the runbook gets fixed.
