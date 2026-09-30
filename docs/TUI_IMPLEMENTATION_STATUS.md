# Python + Textual implementation checkpoint

## Preview readiness review — 2026-09-30

The current candidate includes the full Textual organization workspace, nearby
workstation pairing, and photo keeper decisions based on resolution and meaningful
capture metadata. The README leads with file and folder organization through
relationship inference and contextual evidence, includes real headless TUI
captures, and links to source installation, platform limits, and a preview demo
guide. The candidate remains an alpha preview; no package has been published.

The readiness audit corrected stale review evidence after rescans or workspace
changes, content-verification failures in execution/recovery, disabled-cache
analysis bypassing stale-file checks, media-date parsing, and remote transport
and uncertain-command handling. Regression tests use disposable synthetic files.

Current results belong to [PR #10](https://github.com/Yariminal/DoneDataHoarder/pull/10)
and its checks for the latest commit. The checkpoint counts below describe
earlier increments, not the latest suite. Package qualification builds a wheel
and sdist, installs them outside the checkout, and exercises metadata processing,
saved-session resume, pairing/revocation, and isolated uv tool lifecycle.

Real Omarchy/Foot and Kitty image rendering, Windows-to-Omarchy discovery,
workstation GPU processing, external-drive hotplug, and real photo libraries
still require hardware checks. Remote access is folder-allowlisted; automatic
external-SSD-only enforcement and EXIF merging are not implemented.

## Nearby workstation increment — 2026-09-29

The same TUI now includes mDNS discovery, a Nearby workstations picker, managed
workstation HTTPS, one-use owner-issued invitations, saved device profiles,
revocation, and optional reconnect across IP changes. Invitations can be renewed
without stopping jobs. Command guards follow verified workstation identity;
an OS lease prevents two local connections from overwriting pending outcomes.
Closing the picker or connection cleans up late workers without releasing stale
results into a new connection. See the [execution plan](REMOTE_DISCOVERY_PLAN.md)
and [setup guide](remote-sessions.md).

Final local suite: **888 passed, 2 skipped**, in 109.37 seconds. One existing
Gemini SDK deprecation warning remains. The source tests include real HTTPS
pairing, verified certificates/hostnames, session access and revocation; mDNS
record/lifecycle and multi-adapter behavior use deterministic backends. Six web
state regressions, JavaScript syntax, compilation, dependency consistency, and
Git whitespace checks passed. Actual Windows-to-Omarchy discovery, native
terminal graphics, GPU processing, and physical SSD identity remain hardware
qualification work; strict external-SSD enforcement is still proposed.

The discovery candidate's wheel/sdist and isolated installation evidence are
retained under `dist/discovery-candidate/`. The installer exercises the TUI,
docs, remote, and nearby extras, metadata processing, and pairing/revocation
outside the source checkout. Earlier validation checkpoints follow.

## Earlier remote-session checkpoint

Date: 2026-09-29. Working branch: `codex/omarchy-tui`, based on `91892ec`.
This records an earlier increment of the [delivery plan](PYTHON_TEXTUAL_PLAN.md);
it is not a completed release.

**Earlier remote-session increment.** The workstation server and TUI client are
implemented locally. The same workspace now has a connection indicator/F2 panel,
remote session settings, scoped image previews, disconnect/reconnect handling,
durable command receipts, and read-only session access while storage is absent.
Quitting a remote client leaves workstation processing running. The server uses
an authenticated API with verified HTTPS or loopback through an SSH tunnel;
authorized collection folders are explicit. Database, credentials, and recovery
state are refused inside the scanned folders.

The latest full local suite passed **735 tests, with 1 skipped**, in 96.27 seconds.
This includes real remote approve/preview/rename/undo, dropped replies after
commit, reconnect without replay, path/session boundaries, missing storage,
off-page preview revisions, settings, and UI controls. Compilation, dependency
checks, and Git whitespace checks also passed. A new wheel and sdist were built
under `dist/remote-candidate/`; the earlier artifacts below predate remote mode.
The new artifacts passed installation and TUI checks outside the checkout,
including rebuilding the sdist to the same package payload and metadata, in
94.28 seconds. That installer run exercised the TUI/docs extras; remote API
integration was validated by the source test suite. See
[remote setup and limitations](remote-sessions.md).

A separate cold-process Uvicorn/TCP smoke used the normal HTTP client and a
temporary 258-file collection. The metadata pipeline was running at disconnect
and completed on reconnect in 9.562 seconds (12.484 seconds for the whole smoke).
Authentication, settings, real preview dimensions/crops/pixels, and graceful
server cleanup passed. A six-file run passed too. This is loopback transport
evidence, not LAN/SSH/TLS, external-SSD, GPU, or native terminal qualification.
Initial smoke timeouts were traced to the test helper's blocked stdin reader
during NumPy import; replacing only that helper with sentinel-file shutdown
resolved both fixtures. Runtime source hashes remained unchanged. The thread's
artifact directory retains the script and all smoke reports.

The user's intended storage is an external SSD attached to Windows. Current
authorization is folder-based: strict SSD classification, persistent volume
identity, changed-letter migration, and physical hot-unplug checks remain the
proposed [external storage policy](REMOTE_STORAGE_POLICY.md). Actual Omarchy/LAN,
GPU inference, native graphics, and guided uncertain-command recovery remain
release gates. Nothing has been published or enabled on the user's network.

The following table and artifact hashes record the earlier local-TUI increment.

| Area | Delivered in this increment | Remaining acceptance work |
| --- | --- | --- |
| Native image qualification | `ddh tui-fixture` generates 13 diagnostic images and a real isolated metadata session; `ddh tui-diagnostics` creates an environment report and pending checklist | Actual Foot, Kitty, and installed-default-terminal walkthroughs on Omarchy |
| Image lifecycle | Unchanged previews retain pixels/zoom; changed sources invalidate; resize/zoom keep valid pixels until replacements are ready; cancelled results release resources; animations disclose first-frame preview | Native positioning, cleanup, flicker, and visible latency measurements |
| Startup and navigation | Folder/recent-session picker, saved-setting-aware Ollama readiness, idle session switching, and keyboard help | Current/Proposed tree, search/filtering, and the remaining visual workflow |
| Worker lifecycle | Completed jobs remain tracked until their threads actually exit; automatic continuation remains supported; failed fixture preparation cancels/drains owned work | Broader crash/recovery and interruption qualification against real collections |
| Packaging | Wheel/sdist verification outside the checkout, dependency evidence, isolated uv install/reinstall/uninstall, Linux artifact CI configuration | Linux CI execution, fresh Omarchy install, cross-version migration, desktop integration, and AUR packaging |
| Performance and quality | Removed redundant image redraw work and added meaningful lifecycle regressions | Named-machine latency/RSS measurements, large real-file collections, and independently labeled proposal-quality checks |

**Validation completed.**

- Full local suite: **619 passed, 1 skipped**, in 84.25 seconds. One existing
  optional Gemini SDK deprecation warning remains. Development environment:
  Windows 11, Python 3.12.3, Textual 8.2.8, textual-image 0.14.1, Pillow 12.2.0.
- Real fixture preparation ran Scan → Enrich → Dedup → Preview on all 13
  generated samples without AI initialization or applied proposals. Its
  deliberately missing/replaced files retain the old indexed baseline for
  native error/staleness checks. These are synthetic diagnostic drawings.
- Regressions exercise same-path image replacement, unchanged-preview reuse,
  cancellation, modal focus, session opening, coalesced model checks, correct
  saved-plan server/models, final worker notifications, and automatic advancement.
- Headless snapshots of the new picker and help were visually checked at
  80×24 and the picker at 120×40. This establishes layout evidence only.
- Final wheel and rebuilt-sdist checks passed outside the checkout, including
  dependency checks, actual metadata processing, headless TUI, session relaunch,
  fixture/diagnostics commands, and isolated uv installation, same-candidate
  reinstall, and uninstall while preserving data. Verification took 118.53
  seconds and rechecked packaged source hashes before recording success.
- Package qualification used Windows/Python 3.12.3, Textual 8.2.8,
  textual-image 0.14.1, newly resolved Pillow 12.3.0, and uv 0.12.20. The
  generated constraints describe that platform's tested environment.
- Python compilation, `pip check`, and Git whitespace checks passed.

Candidate artifacts are local under `dist/tui-candidate/`:

| Artifact | SHA-256 |
| --- | --- |
| `donedatahoarder-0.6.0-py3-none-any.whl` | `c8250c91dd1c9be3e19a8bc27be57b15e6abdfd395dfa6e715a84e53d572a648` |
| `donedatahoarder-0.6.0.tar.gz` | `8c80349a2a04be3f04f757f894b1d83b3396e22762c69c1ab99bc849fa4aee0d` |

The adjacent `tui-distribution-report.json` and
`tui-distribution-report.constraints.txt` contain the full installation evidence.
These artifacts are unpublished. The Linux Python 3.12/3.14 jobs are configured;
they have not been run on GitHub as part of this local work.

**Next acceptance step.** Run the [native qualification walkthrough](tui-qualification.md)
on Omarchy, starting with direct Foot/Sixel and Kitty sessions. This host is
Windows and its WSL environment cannot start, so native graphics remain
`not_run`. The test kit deliberately does not turn a detected graphics protocol
into a passed result. Continue the remaining workflow/performance work against
the measured findings; the six release gates in the plan remain authoritative.
