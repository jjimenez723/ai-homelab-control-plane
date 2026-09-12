# 01 — Hardware Inventory

## Known inventory

### `mac-m4pro`
- Apple M4 Pro
- 48 GB unified memory
- macOS
- Current best candidate for local LLM/VLM inference and control-plane development

### `dell-xeon`
- Dell Precision 7520-class (exact model to verify)
- Xeon CPU
- 32 GB RAM
- Candidate headless Linux worker
- Engineering-workflow heritage; GPU details unknown

### `dell-i7`
- Dell laptop
- Intel i7
- 32 GB RAM
- Candidate headless Linux worker

### `dell-lite`
- Older Dell laptop
- 8 GB RAM
- Broken display
- Candidate lightweight infrastructure node

### `t7` (volume `angel`)
- Samsung T7 external SSD, 2.0 TB (2,000,398,893,056 bytes)
- Filesystem: **APFS (Encrypted)**, volume `Angel` at `/Volumes/Angel`, container `disk5` on physical store `disk4s1` (USB) — erased and recreated 2026-09-11 (D-019)
- Identity of record: the macOS Volume UUID, held only in the gitignored `config/ingest.yaml` (`t7.volume_uuid`). It is deliberately not duplicated into this tracked file; read it with `diskutil info /Volumes/Angel`.
- Verified 2026-09-11 on `mac-m4pro`: mounted, unlocked, writable, SMART status Verified
- Still to verify: serial number and measured throughput (`dd`/`fio`)

## Inventory commands to capture later

For Linux:
```bash
hostnamectl
lscpu
free -h
lsblk -o NAME,SIZE,FSTYPE,MODEL,SERIAL,MOUNTPOINTS
lspci
ip -br addr
sudo dmidecode -t system -t memory
```

For macOS:
```bash
system_profiler SPHardwareDataType SPStorageDataType
sysctl -n machdep.cpu.brand_string
df -h
diskutil list
```

## Missing facts that affect design

- exact CPU model/core count for both 32 GB Dells;
- any discrete AMD/NVIDIA GPU present in the Precision;
- Ethernet capabilities of each node;
- Wi-Fi generation;
- available internal-disk space on each worker;
- whether workers can use wired Ethernet;
- thermal condition/battery health;
- whether Wake-on-LAN is supported;
- whether the cluster must remain portable or can become stationary.

Do not make expensive hardware decisions until this inventory is complete.
