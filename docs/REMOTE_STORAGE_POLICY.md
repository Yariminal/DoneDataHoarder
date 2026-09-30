# Remote collections on an external SSD

The intended setup is an Omarchy laptop running the TUI, a Windows workstation
running the pipeline and Ollama, and an external SSD attached to that workstation.
Originals stay on the SSD. The laptop receives session state and bounded image
previews. The workstation's internal storage should hold the index, command
receipts, connection configuration, and recovery journal.

## Implemented boundary

The remote server requires explicit allowed collection folders through `--root`.
It checks session membership, folder containment, and symlink/junction ancestry.
Image previews accept indexed file IDs rather than arbitrary paths and validate
the source identity across decoding.

This is a **folder allowlist**. It does not identify SSD hardware, pin a physical
device or volume, or guarantee safe operation across unplugging a drive and
reusing its letter. Configure a specific collection folder on the external SSD;
do not interpret that configuration as a completed external-SSD policy.

## Proposed device boundary

Register an explicitly selected SSD and collection folder on the workstation.
Allow remote access only to those registered collections. Device discovery can
show a suggested external-drive list, but it must not silently grant access to
all drives that match a hardware category.

Windows `GetDriveTypeW` distinguishes fixed and removable media; its fixed-media
examples include flash drives. It does not establish that a drive is both
external and an SSD. USB bus information and media characteristics can assist
selection, but an unknown result must remain unknown. Strict automatic
external-SSD classification needs its own hardware qualification before it can
be advertised or enforced. [Microsoft: GetDriveTypeW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getdrivetypew)

For the first volume-aware implementation, the authorization decision should be
the user's registered volume and folder, with the following persisted binding:

- Workstation/server identity and collection ID.
- Windows volume GUID path.
- Filesystem volume serial number.
- Authorized folder relative to that volume, plus its display path and label.

Obtain the enclosing mount point with `GetVolumePathNameW`, then its volume GUID
with `GetVolumeNameForVolumeMountPointW`. Query `GetVolumeInformationW` against
that GUID path for the filesystem serial and capabilities. Recheck that the
original path still maps to the same GUID before accepting the binding. Use
absolute validated paths: `GetVolumePathNameW` can otherwise resolve an invalid
or relative path to the boot volume. Preserve the existing link/junction checks.
[Microsoft: GetVolumePathNameW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getvolumepathnamew),
[GetVolumeNameForVolumeMountPointW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getvolumenameforvolumemountpointw)

Drive letters and labels are display information, not identity: letters can
change and labels can repeat. A volume can also have multiple GUID paths, so a
changed GUID result should block access pending an explicit check, rather than
silently replacing the saved binding. The filesystem serial is assigned at
format time; it is not the manufacturer's hardware serial number. This binding
protects against accidental replacement and reformatting, and is not device
authentication against deliberately cloned identifiers.
[Microsoft: Naming a Volume](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-volume),
[GetVolumeInformationW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getvolumeinformationw)

Persist the binding on the workstation's internal storage and retain it across
daemon restarts. Never learn a replacement identity automatically on startup or
reconnect. Missing, inaccessible, unrecognized, or mismatching storage must block
new file access. A non-Windows `st_dev` check may support test fixtures or a
separate platform implementation; it is not a substitute for this persistent
Windows binding.

## Drive changes and ongoing work

Connection state and storage state must remain separate. A connected workstation
with a missing SSD is still reachable. The TUI can keep its current layout and
show the storage condition in the existing status/details area.

| Storage condition | Required behavior |
| --- | --- |
| Registered volume and folder available | Normal pipeline and review controls. |
| SSD absent or inaccessible | Show persisted session progress/history; permit pause/cancel; block new file access and commits. |
| Different volume at the same drive letter | Show identity mismatch; do not scan, preview, apply, or undo against it. |
| Original SSD returns | Verify the saved binding; require explicit resume and a fresh apply/undo preview. |
| Same SSD returns at another letter | Detect it without silently rewriting stored paths; require an explicit, validated rebind/migration. |

Current file records, proposals, and journal entries contain absolute paths.
Automatically replacing a drive letter in only the session root would leave
those other records pointing at the old letter. Initial volume-aware support
should block this case; a later migration must update and validate the complete
set together. Moving filesystem operations to volume-GUID-based paths is another
option, but requires testing every scanner, extractor, executor, and recovery
path that consumes them.

An API-entry check alone is insufficient: background jobs continue after a
request ends, and apply/undo can perform multiple operations. Required hooks are:

1. Carry the persisted volume binding into the session and durable run plan.
   Revalidate on start, resume, and before advancing each pipeline stage.
2. Check storage before scheduling each file read and at producer checkpoints.
   A detach or mismatch must stop further work and record a recoverable storage
   error. Do not let an empty or interrupted directory walk report completion.
3. Revalidate immediately before each apply/undo filesystem mutation, including
   destination-directory creation and trash operations. Stop the remaining batch
   on storage failure; preserve completed transitions and recovery records.
4. Include the storage binding in preview/confirmation state. Reconnection or
   reattachment must not replay an uncertain apply/undo request automatically.
5. Keep metadata inspection and pause/cancel independent of filesystem readiness.
   Cancellation must still wait for actual worker exit before releasing leases.

Periodic checks and checks before an operation cannot make unplugging hardware
mid-operation atomic. An implementation claiming protection against rapid drive
letter reuse also needs volume-bound I/O paths or validated handles, and tests of
the resulting failure and recovery behavior. Existing journaling helps recovery;
it does not supply the missing volume boundary.

## Acceptance gate before calling this SSD-bound

- Register an actual external SSD, including one Windows reports as fixed media.
  Verify that unrelated internal disks and unregistered external drives remain
  outside the allowed collections. Unknown hardware classification is disclosed.
- Restart the daemon with the same SSD; the persisted binding remains unchanged.
  Start with it absent; no replacement binding or collection directory is created.
- Replace the SSD with a different volume at the same letter and folder name.
  Reject scan, source reads, previews, apply, and undo, including queued work.
- Reformat the registered SSD or alter its reported identity. Require explicit
  registration instead of accepting the old session against the new volume.
- Detach during scan, metadata extraction, inference preparation, and between
  pipeline stages. Keep the workstation connection usable; avoid a false
  completed scan; permit cancellation and later explicit resume.
- Detach between two apply/undo operations and during a filesystem operation.
  Stop the batch, retain accurate partial results, and verify recovery on the
  original SSD. Never continue on a replacement disk or replay a lost request.
- Reattach the original SSD at the same and at a different drive letter. Require
  identity verification in both cases and migration for changed stored paths.
- Test symlink/junction escapes, mounted folders, inaccessible volumes, duplicate
  labels, API failures, and attempts to change the binding through the remote API.
- Exercise the Windows volume API helper with mocked failures and identity
  changes, plus native integration tests. A read-only probe of the three APIs has
  succeeded on this development machine's internal NTFS volume; external SSD
  hot-plug behavior has not yet been qualified.

This policy is proposed work. The current remote implementation remains governed
by the explicit folder allowlist described above.
