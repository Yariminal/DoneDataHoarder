# Remote terminal sessions

Development candidate, 2026-09-29. The Omarchy laptop renders the same Textual
workspace; a Windows workstation owns the database, files, pipeline, Ollama,
and image preparation. The intended collection lives on an external SSD
connected to that workstation. No upload, synchronization, desktop streaming,
or shared SQLite file is involved.

The header's connection button (also F2) shows workstation status and session
settings. Model and worker changes execute on the workstation and apply to the
next new run. Existing unfinished plans retain their saved settings. The
Pipeline, Review, Collections, History, keyboard navigation, and local Omarchy
theme remain in the same interface.

## Nearby workstations (recommended)

On the Windows workstation, use an internal directory for DDH's database and
credentials and authorize only the collection folder on your SSD:

```powershell
python -m pip install -e ".[remote,nearby]"
ddh remote-serve --root "E:\Hoard" --db "$env:LOCALAPPDATA\DoneDataHoarder\index.db" --token-file "$env:LOCALAPPDATA\DoneDataHoarder\remote-token" --host 0.0.0.0 --name HOME-PC --discoverable --pair
```

This opts into a LAN HTTPS listener and mDNS discovery. DDH creates a persistent
TLS identity next to the index and prints a private, one-use invitation valid
for ten minutes. Transfer that invitation directly from your workstation to
your laptop. It authorizes a device to use the already allowed collection
folders; treat it like a password until used or expired. Subsequent starts can
omit `--pair`. DDH does not install a service or change firewall rules.

On Omarchy, in Python 3.12 or newer:

```sh
python -m pip install -e '.[tui,nearby]'
ddh tui --discover
```

Select HOME-PC, paste its invitation into the masked field, and choose Connect.
Choose a collection or saved session in the usual picker. Opening it does not
start processing. F2 → Nearby workstations also opens the picker from an existing
workspace; stop any local job before switching to a workstation.

The laptop remembers its own device credential and the workstation's verified
certificate. With **Reconnect automatically** enabled, it can rediscover the
workstation after its address changes. If one opted-in saved device is nearby,
`--discover` can select it automatically. Certificate or identity changes require
deliberate re-pairing; an mDNS announcement cannot replace a trusted certificate.
Forget removes the laptop's saved credential but retains command recovery state.

To issue a new invitation without stopping the server or its jobs, or inspect
and revoke devices, use another workstation terminal:

```powershell
ddh remote-pair --db "$env:LOCALAPPDATA\DoneDataHoarder\index.db"
ddh remote-devices --db "$env:LOCALAPPDATA\DoneDataHoarder\index.db"
ddh remote-devices --db "$env:LOCALAPPDATA\DoneDataHoarder\index.db" --revoke DEVICE_ID
```

Nearby discovery uses mDNS (UDP 5353); the remote API uses the configured TCP
port (8765 by default). Networks that isolate devices or filter multicast may
not show candidates. Use the picker's manual HTTPS address together with an
invitation or saved device, or use the manual SSH/TLS setup below. Discovery
announces only name, protocol, identity, hostname, and endpoint addresses.
Collection names, SSD paths, credentials, and file content are not broadcast.

## Manual connection: start the workstation

From this source checkout, in Windows PowerShell:

```powershell
python -m pip install -e ".[remote]"
ddh remote-serve --root "E:\Hoard" --db "$env:LOCALAPPDATA\DoneDataHoarder\index.db" --token-file "$env:LOCALAPPDATA\DoneDataHoarder\remote-token" --name HOME-PC
```

Replace `E:\Hoard` with the specific folder on your external SSD. Repeat
`--root` to authorize additional folders. Other roots and symlink/junction
escapes are refused. Existing sessions outside these folders are not exposed.
Keep the database, receipt sidecar, token, and execution journal on internal
storage; only collection files belong on the SSD. The default index is the
same location used by `ddh tui`; specify `--db` to use another existing index.

The command generates a private token file if absent, reuses it thereafter,
and prints its path, never its contents. Copy the token securely to the laptop,
for example using your existing SSH connection. Treat it as a credential: its
holder can review and apply changes inside the authorized folders. On Windows,
use a private user directory with its normal account ACLs.

Keep Ollama running on the workstation at its default local endpoint. Its
model must already be installed; DDH checks readiness without downloading
models. `--model`, `--workers`, and `--ollama-host` on `remote-serve` set the
workstation defaults. GPU use is determined by Ollama and its driver/runtime;
it has not been measured on this user's RTX 3090 setup. See
[Ollama hardware support](https://docs.ollama.com/gpu).

The server remains a foreground process. It is not installed as an automatic
Windows service. Closing the laptop TUI leaves workstation processing running;
stopping the workstation process interrupts it. Reopen the session and explicitly
resume after a workstation restart. One daemon per database is enforced by a
separate lifetime lock in `remote-serve`.

## Connect from Omarchy

The default server listens on workstation loopback only. With Windows OpenSSH
already configured, create an encrypted tunnel from the laptop:

```sh
ssh -N -L 8765:127.0.0.1:8765 windowsuser@workstation-ip
```

For Windows SSH setup, use Microsoft's
[OpenSSH installation guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse).
DDH does not install SSH or change firewall rules automatically.

In another laptop terminal, using the matching checkout and Python 3.12+:

```sh
python -m pip install -e '.[tui]'
chmod 600 ~/.config/donedatahoarder/home-token
ddh tui --connect http://127.0.0.1:8765 --token-file ~/.config/donedatahoarder/home-token
```

The picker lists workstation sessions and takes workstation paths. For example:

```sh
ddh tui 'E:\Hoard' --connect http://127.0.0.1:8765 --token-file ~/.config/donedatahoarder/home-token
ddh tui --session SESSION_ID --connect http://127.0.0.1:8765 --token-file ~/.config/donedatahoarder/home-token
```

The laptop does not initialize a DDH database in remote mode. Omit `--db` and
unset `DDH_DB` for a remote client. Use F2 to edit the remote session's model and
worker settings. The [nearby-workstation implementation plan](REMOTE_DISCOVERY_PLAN.md)
describes discovery, pairing, and the identity-bound reconnect behavior.

For direct LAN connections, configure a certificate whose subject alternative
name matches the workstation hostname or IP. The server requires both
`--cert-file server-cert.pem --key-file server-key.pem` when binding outside
loopback, for example with `--host 0.0.0.0`. Connect using
`--connect https://workstation-name:8765 --ca-file trusted-ca.pem`. Normal system
CA verification is used if `--ca-file` is omitted. Invalid certificates,
redirects, and plain HTTP LAN endpoints are refused. The existing unauthenticated
`ddh serve` browser application is a separate application, not the remote
session endpoint.

## Connection and drive behavior

- A failed connection retains the last received workspace, marks it stale,
  blocks changes, and retries with a bounded delay. F2 offers an explicit retry.
  No offline commands are queued. Restored connections fetch authoritative state.
- Each command has a durable workstation receipt. A lost response triggers a
  receipt lookup; the command is not resent. The laptop persists its pending ID
  and workstation identity under `XDG_STATE_HOME/donedatahoarder/remote/`
  (default `~/.local/state/donedatahoarder/remote/`). Command guards contain no
  credential. Separate `device-*.json` profiles contain each paired credential
  and trusted certificate, saved with owner-only permissions on Linux. Keep
  this directory private; on Windows use a private account directory and ACLs.
- If a workstation crash leaves an outcome uncertain, further changes are
  blocked. Session state/history remain available. There is deliberately no
  automatic dismissal or replay. A guided operator reconciliation flow remains
  a release gate; preserve the database, receipt sidecar, client state, and
  filesystem journal for diagnosis. Do not delete these files to bypass a block.
- If an authorized collection folder disappears, the workstation remains
  connected and reports storage unavailable. Cached session records and
  cancellation remain accessible; starting, applying, undoing, and image reads
  require the folder to return.
- Image comparison fetches bounded, original-color PNG previews prepared on the
  workstation. Linked zoom/pan, native terminal rendering, and stale-image
  disposal are retained. Full-original downloads and external-viewer opening
  are not provided in this candidate; the remote viewer labels that limitation.

## External SSD policy and remaining qualification

**Implemented:** explicit authorized folder boundaries, authenticated access,
scoped file IDs, no arbitrary file-read endpoint, and no remote root changes
through session settings.

**Proposed, not implemented:** restricting enrollment to approved external SSD
volumes, durable volume GUID/serial bindings, and hot-unplug identity checks
through every background stage and filesystem operation. A folder allowlist
does not prove that a drive is external or an SSD and cannot protect against
Windows reusing its drive letter for a different volume. Keep the collection
mounted with the same identity during this candidate's processing. See the
[external storage policy](REMOTE_STORAGE_POLICY.md) for the proposed boundary
and acceptance tests before enabling strict SSD-only mode.

Local automated tests cover authentication, root/session scope, command receipts,
settings, the real metadata pipeline, preview pixels, TUI controls, Windows path
display, and filesystem execution/recovery. Actual Omarchy-to-Windows LAN,
native terminal graphics, 3090 inference, real LAN discovery, and physical
SSD detach/reconnect testing are still required. This is not a qualified release.
