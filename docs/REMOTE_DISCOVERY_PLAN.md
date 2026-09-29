# Nearby workstations

Execution plan, 2026-09-29. This extends the remote-session transport with
LocalSend-style discovery. Implementation and local verification are complete.
After distribution verification, publish the branch as a pull request for review.

## Implementation checklist

- [x] Add bounded mDNS advertising and browsing, with manual connection fallback.
- [x] Add persistent workstation TLS identity and expiring one-use invitations.
- [x] Issue revocable per-device credentials and save trusted laptop profiles.
- [x] Keep pending command receipts bound to workstation identity across IP changes.
- [x] Add Nearby workstations, pairing, saved-device selection, and reconnect to the TUI.
- [x] Test discovery parsing/lifecycle, pairing abuse cases, real TLS, reconnect,
      command guards, and keyboard UI; run the complete regression suite.
- [x] Update setup documentation and verify the built wheel/source distribution.

Pairing uses standard certificate-verified TLS bootstrapped by an out-of-band
invitation. The workstation owner explicitly opens pairing with `--pair` and
shares a ten-minute, one-use invitation containing the workstation certificate,
identity, and a 256-bit random secret. The laptop verifies that certificate and
hostname before redeeming the invitation for its own revocable credential.
Discovery records never supply trust. This requires one paste on first use;
there is no custom short-code cryptography.

## User experience

1. Enable discoverability on the Windows workstation that owns the external
   SSD collection. Keep the main TUI layout unchanged.
2. On Omarchy, open the connection indicator/F2 and choose Nearby workstations.
   Show DDH workstation names and availability without requiring IP entry.
3. Pair once with the chosen workstation. The pairing flow must authenticate
   the workstation and receive explicit owner approval before issuing a client
   credential. A device name or discovery announcement is not proof of identity.
4. Open a saved session or an authorized SSD collection. Pairing does not expand
   the collection allowlist or start processing.
5. Offer Remember and reconnect for that workstation. Re-find its current
   address after network changes, verify its saved identity, then refresh session
   state and resolve pending command receipts. Never replay uncertain changes.

The existing header remains a compact status indicator, for example
`HOME-PC · Connected`. Connection details distinguish an unreachable workstation
from an attached-workstation session whose SSD is unavailable. GPU information
and collection names belong in authenticated details, when actually reported.

## Discovery transport

Use Python Zeroconf/mDNS with a DDH-specific service type such as
`_ddh._tcp.local.`. Advertise the display name, API major version, persistent
server identity, and HTTPS endpoint information. Exclude credentials, SSD paths,
file metadata, collection names, and model settings from announcements.

Advertisements are untrusted candidates. Validate and bound their properties,
track disappearance/expiry, deduplicate interfaces, and handle multiple network
adapters. Do not trust a certificate fingerprint merely because it arrived in
the same unauthenticated announcement. Manual address entry and existing SSH
tunnels remain fallback options when local discovery is blocked.

This follows the nearby-device experience, not the LocalSend wire protocol.
LocalSend documents its own UDP multicast discovery and HTTP fallback. DDH does
not need LocalSend interoperability, its ports, or its file-transfer protocol.
[LocalSend protocol](https://github.com/localsend/protocol#3-discovery)

Python Zeroconf provides service registration, browsing, address resolution, and
update/removal notifications. It avoids building our own multicast service
registry. [Python Zeroconf API](https://python-zeroconf.readthedocs.io/en/latest/api.html)

## Delivery sequence

**Discovery:** opt-in `remote-serve --discoverable` and `tui --discover`, with
bounded background browsing and manual fallback. Announcements start after
the HTTPS listener is ready and are withdrawn on shutdown. Multiple addresses
are tried with certificate verification before sending a pairing secret.

**Pairing and saved devices:** `--pair` or `ddh remote-pair` explicitly issues
a ten-minute owner-approved invitation. The certificate and 256-bit secret
travel out of band once; redemption uses standard verified TLS. Only a hash of
the secret and each issued device token is stored on the workstation. A device
token can be revoked with `ddh remote-devices --revoke`. Invitation redemption
is never automatically retried after an ambiguous network failure.

**Reconnect:** profiles and unresolved command receipts follow verified
workstation identity. Legacy URL guards migrate conservatively, preserving
unresolved outcomes. Forget removes a saved credential while retaining recovery
state. Rediscovery never repeats a mutation or starts a job.

## Acceptance checks

- Actual Omarchy and Windows discovery across Ethernet/Wi-Fi on the same LAN.
- Same-name workstations, multiple adapters, expired entries, and changing IPs.
- Forged announcements cannot obtain credentials or filesystem access.
- Pairing expiry, failed approval, rate limits, credential revocation, and a
  replaced certificate or workstation all preserve the trust boundary.
- Rediscovery cannot lose pending command receipts or repeat apply/undo.
- Blocked multicast, guest-network isolation, and private-network firewall
  restrictions yield a usable manual fallback without freezing the TUI.
- SSD collection authorization and missing-drive controls retain their existing
  behavior. Discovery does not imply strict physical-volume enforcement.

Network discovery and managed HTTPS are enabled only by the explicit CLI option.
No firewall, Windows service, SSH, or startup settings are changed automatically.
