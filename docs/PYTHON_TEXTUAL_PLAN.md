# Python + Textual delivery plan

Date: 2026-09-29. Decision: retain the Python engine and ship the Omarchy terminal
experience with Textual. This is the plan for the next implementation cycle.
Milestones below are pending unless explicitly described as existing work.

The next feature priority is remote sessions: an Omarchy laptop controls the
Windows workstation that owns processing and an attached external SSD. The
[remote session candidate](remote-sessions.md) implements the transport and
existing workspace integration. The [external storage policy](REMOTE_STORAGE_POLICY.md)
specifies the proposed volume boundary before claiming SSD-only support. These
add LAN, reconnect, and storage qualification to the release gates below.
The [nearby-workstation implementation](REMOTE_DISCOVERY_PLAN.md) adds automatic
discovery and one-time pairing to simplify the remote connection experience.

The [photo keeper requirement](PHOTO_KEEPER_POLICY.md) records the user's core
duplicate-photo intent: retain the best available resolution and meaningful EXIF,
with explicit review when those criteria favor different copies. The implemented
policy adds structured extraction, shared ranking, comparison evidence, and
local/remote keeper selection; existing indexes have an explicit refresh command.

The product promise is: open a messy folder, watch the real pipeline explain its
work, compare duplicate candidates visually, inspect the proposed organization,
and apply reviewed changes with a recoverable history. The full pipeline stays:
Scan → Enrich → Analyze → Dedup → Relate → Propose → Organize → Preview → Review/apply.

The approved Pipeline Observatory concept remains the visual reference: dense
monospace layout, thin borders, visible stages, collection tree, evidence pane,
activity, keyboard hints, and the user's active Omarchy palette. The main visual
payoff is seeing the proposed collection take shape and comparing actual photos.

**Starting point.** The local `codex/omarchy-tui` branch already contains `ddh tui`,
the shared service boundary, real jobs and sessions, metadata-only processing,
review/apply/undo, image comparison, bounded image caching, and theme reload.
The last full local test run passed 573 tests with one skipped; all 13 UI tests
passed again after the final compact-layout fix. These results are a baseline,
not native Linux graphics or release qualification. Foot and Kitty rendering
remain unverified on this Windows host; local WSL could not start.

| Milestone | Result | Completion evidence |
| --- | --- | --- |
| 1. Native image qualification | Reliable photos in the actual terminal | Recorded Foot and Kitty comparison walkthroughs and a versioned compatibility matrix |
| 2. Product workflow and visual finish | The approved design works with real data | Keyboard-complete folder-to-review walkthrough at wide and compact sizes |
| 3. Performance and architecture | Responsive UI on large collections | Reproducible latency, memory, query, and processing measurements |
| 4. Review quality and recovery | Decisions are understandable and recoverable | Labeled proposal review plus real-filesystem apply, interruption, and undo evidence |
| 5. Installation and integration | A usable Omarchy application | Fresh install, upgrade, relaunch, and uninstall checks from built artifacts |
| 6. Release candidate | A demonstrable, supportable first release | All required gates pass on the same candidate revision |

Milestone 1 starts first. Milestones 2 and 3 can then proceed together; recovery
regressions run throughout. Packaging can proceed alongside product work once
the dependency set is stable. Milestone 6 depends on all five preceding gates.

**1. Qualify native images before expanding the image UI.**

- Establish a real Omarchy machine or VM. Record Omarchy, terminal, Python,
  Textual, textual-image, font/cell size, display scaling, and tmux versions.
  Inspect the installed default terminal rather than assuming it is Foot.
- Qualify Foot/Sixel as the first image target and Kitty/Kitty graphics as the
  second. Publish support only for combinations actually exercised. Test the
  installed default terminal too; clearly explain an external-viewer fallback
  if it lacks the required graphics path. Ghostty and tmux are additional matrix
  entries, not automatic claims inherited from protocol support.
- Use a disposable corpus containing exact copies, edited/cropped lookalikes,
  portrait and landscape images, EXIF rotation, transparency, large images,
  malformed images, and files changed or removed after indexing. Explain the
  first-frame policy for animated formats rather than implying playback.
- Exercise thumbnail selection, both comparison panes, candidate switching,
  linked zoom/pan, resize, scrolling, light/dark theme changes, overlapping
  dialogs, close/reopen, and normal exit. Verify the correct image stays paired
  with its path and evidence during rapid navigation.
- Preserve the renderer adapter, early capability detection, bounded decoding,
  asynchronous preparation, stale-result rejection, and external viewer action.
  Avoid re-encoding unchanged images or repainting them for unrelated status
  updates. Keep protocol workarounds isolated and covered by regression tests.

Gate: no stale/wrong image, persistent ghost image, corrupted borders, stuck
input, or image remaining after close/exit. Measure redraw latency and inspect
flicker on native recordings. Headless screenshots do not satisfy this gate.
If Sixel fails, investigate dirty-region updates, coalesced redraws, and the
adapter before adding image effects. An external viewer remains useful but does
not fulfill the promised inline comparison experience for the primary target.
The upstream library documents Sixel redraw limitations, making this a concrete
qualification task. [textual-image limitations](https://github.com/lnqs/textual-image#limitations)

**2. Finish the product workflow and the visual payoff.**

- Add an in-app start/resume flow: choose a folder or recent session, inspect
  model availability and terminal capability, then explicitly choose the full
  or metadata-only pipeline. Model downloads remain an explicit user action.
- Keep the stage rail visible and make each stage inspectable. Show real counts,
  duration, skipped/error states, and useful current activity. Show throughput
  or ETA only when enough measured data exists; represent unknowns honestly.
- Add a Current / Proposed collection tree built from persisted proposals.
  Mark pending, approved, rejected, protected, and needs-review items; changing
  approval updates the projected tree. Proposed paths remain visibly tentative
  until the execution journal confirms the operation. Preserve selection and
  expansion across updates.
- Make duplicate comparison the central review interaction: stable candidate
  and keeper labels, dimensions and sizes, exact versus similar evidence,
  linked view controls, and clear keep/reject decisions. Similarity scores are
  not deletion confidence. Optional blink comparison comes after the native
  rendering gate; animated sliders and image effects are outside the first
  release scope.
- Add file/proposal search, filters for pending/uncertain/errors/protected
  items, and a keyboard help overlay. Preserve identity when sorting or paging.
  Expose complete counts even when only a page or sample is displayed. Exercise
  large duplicate groups and long/Unicode paths independently of initial load.
- Match Omarchy colors and focus treatment with a Tokyo Night fallback. Verify
  a light palette, a dark palette, missing palette data, and live theme changes.
  Keep photos in their original colors. Use symbols/text as well as color for
  status, and maintain a readable monochrome mode.

Gate: a keyboard-only walkthrough can open a folder, run metadata processing,
compare copies, review a proposal, inspect the proposed tree, preview/apply,
and find its history. Repeat at 80×24, 120×40, and a wide terminal, including
resize while a dialog is open. A separate run exercises the complete Ollama
pipeline and records model failures as failures, not successful completion.

**3. Preserve a simple architecture and measure performance.**

| Layer | Responsibility |
| --- | --- |
| Textual app, screens, and widgets | Focus, navigation, rendering, and user intent |
| WorkspaceService | Typed snapshots, session-scoped commands, and operation validation |
| Existing Python engine | Jobs, extraction, inference, proposals, execution, and recovery |
| SQLite and journal | Durable state and execution/recovery evidence |
| Image adapter and cache | Capability detection, bounded preparation, rendering lifecycle |

- Split `tui/app.py` into screens/widgets as the relevant features change.
  Introduce typed service results and progress messages without duplicating the
  engine's rules. Keep the CLI's lazy imports and shared core review helpers.
- Keep filesystem, image preparation, database work, and inference off the UI
  thread. Send results back through Textual's supported message mechanisms.
  UI worker cancellation must not be mistaken for stopping an underlying
  filesystem operation. Retain explicit worker draining and safe shutdown.
  [Textual worker guidance](https://textual.textualize.io/guide/workers/)
- Make unchanged refreshes cheap: retain stable rows, bound queries and widget
  counts, and coalesce progress updates. Profile the current 1.5-second snapshot
  refresh before replacing it. Use a bounded event queue with snapshot
  reconciliation only where it demonstrably improves responsiveness.
- Profile SQL/ORM allocations, transaction frequency, thumbnail work, and
  candidate/naming algorithms. Keep SQLite writes coordinated, transactions
  short, and transaction lifetimes separate from model calls and UI callbacks.
  Use bounded process workers only for demonstrated Python CPU bottlenecks.

Initial UX budgets below are targets, not measured results. Establish a named
reference machine with local SSD storage in milestone 1 and freeze measurement
conditions before tuning. Any changed budget needs a recorded explanation.

| Measurement | Initial target |
| --- | --- |
| Installed-process start to interactive workspace | At most 2 seconds; exclude package installation and model loading, include capability probing |
| Keypress to visible selection/focus response during processing | p95 at most 100 ms |
| Cached image selection to visible preview | p95 at most 250 ms |
| Uncached representative 12 MP JPEG to visible preview | p95 at most 1 second |
| Idle Python process RSS with a 250-row page and warmed image cache | At most 200 MiB, excluding Ollama |
| Repeated comparison open/close and candidate switching | No sustained memory growth after warm-up; cache stays within its configured bounds |

Run at least 100 interaction samples for latency and record end-to-end visible
timing, not just image preparation. Capture median, p95, peak Python RSS, actual
bytes read, file counts, and stage wall times. Report Ollama/GPU use separately.
Exercise real 10,000-file and 100,000-file collections when available; otherwise
label the missing physical test as pending. Maintain separately labeled
synthetic database fixtures for large-count behavior.

Use the earlier 100,000-row naming result (208 seconds / approximately 1.07 GB)
as a reproduction target, not an acceptable default memory claim. Profile the
current source, reduce whole-session materialization, and demonstrate the
effect on both speed and memory. Changes must preserve proposal decisions or
document and validate any intentional quality change.
[Earlier benchmark evidence](ITERATION_3_VALIDATION_REPORT.md)

Gate: UX budgets met under the named workload, no unbounded widget/cache/queue
growth, and published stage measurements with any remaining scale limits.
The first release stays Python + Textual; native extensions, a separate UI
process, and a language migration are outside this implementation cycle.

**4. Verify useful review decisions and recovery.**

- Retain exact and non-exact distinctions, per-file evidence, protected project
  boundaries, stale-review rejection, and fresh execution previews. Preview
  tokens must bind the reviewed action set; new decisions invalidate old ones.
- Build a hand-labeled holdout containing true copies, adjacent frames, edits,
  related project files, and unrelated lookalikes. Report correct/incorrect/
  uncertain suggestions with denominators. Previously sampled non-exact
  proposals were often distinct or derivative files, so a polished presentation
  must not imply they are interchangeable.
- Report useful accepted renames/moves and unresolved files separately from
  indexed files, preserved projects, and rejected suggestions. A completed
  pipeline is not evidence that the collection became more useful.
- Exercise apply and undo on disposable real files, then verify paths, bytes,
  journal state, database paths, and session ownership. Cover rename/move,
  exact-duplicate trash, rejected near duplicates, changed/missing sources,
  destination collisions, and interruption/relaunch. Do not advertise universal
  rollback when external changes prevent recovery.
- Recheck concurrent CLI/web/TUI access to one database, provider loss, read-only
  folders, cancellation, and shutdown during a filesystem mutation. Error views
  must explain the failed action and the available recovery step.

Gate: meaningful core and UI regressions pass, the real-file cycle preserves
the expected content, and a reviewer can explain every suggested destructive
action from its evidence. Good UI performance does not waive this gate.

**5. Make installation feel like an Omarchy application.**

- Keep the TUI on Python 3.12+ and the existing core compatibility until an
  intentional compatibility decision changes it. Test the minimum TUI version
  and the Python version shipped by the tested Arch/Omarchy installation.
- Build and test a wheel and sdist, plus a reproducible development/release
  dependency lock or constraints set. Keep application dependency ranges
  separate from the exact versions used to qualify a release.
- Provide an isolated `uv tool install` path with the TUI and document extras;
  verify the exact command against the candidate artifact before documenting
  it. Users should not manage the source checkout or activate a virtual
  environment for every launch. uv supports isolated application installs.
  [uv tool installation](https://docs.astral.sh/uv/guides/tools/)
- Prepare and validate an Arch `PKGBUILD` for a subsequent AUR distribution,
  using declared package dependencies and a clean build environment. Treat
  recipe preparation, repository submission, and package publication as
  separate delivery steps; do not describe an unpublished package as available.
- Add an optional terminal desktop entry, shell completions, and actionable
  diagnostics for Ollama, image support, writable data locations, and disk
  capacity. Use XDG locations. Avoid changing terminal or compositor settings
  as an installation side effect.
- Verify upgrade/relaunch with existing sessions and document backup/recovery
  for any schema change. Test uninstall without removing the user's index or
  journal. Keep PDF/Office/video dependencies explicit and test their missing
  dependency messages.

Gate: on a fresh Omarchy user account, install a built candidate, run `ddh tui`,
resume after relaunch, upgrade, and uninstall successfully. Test the installed
artifact outside the repository so editable imports cannot hide missing files.

**6. Qualify and prepare the release candidate.**

- Run the core suite, headless Textual integration tests, package tests, and
  native terminal checks against the same recorded source/dependency revision.
  Add Linux minimum/current-Python jobs while preserving existing CLI coverage.
- Record the native support matrix, measured performance, known limits, and
  installation commands. Headless test success and prior benchmark evidence
  remain distinct from current native acceptance evidence.
- Prepare a deterministic sample collection with original or redistributable
  photos and known duplicate relationships. Record a short native demo showing
  pipeline progress, proposed organization, image comparison, review/apply,
  and recovery. Label sample data; use real processing counters.
- Prepare release notes and launch material around demonstrated behavior.
  Package publication and social posting are later explicit release actions.

Release acceptance requires inline comparison on the primary qualified terminal,
the complete keyboard workflow, the recovery gate, measured responsiveness,
and a clean installation path. A screenshot or simulated browser demo alone
cannot complete any of those checks.

The first implementation increment is milestone 1's small native comparison
fixture and terminal checklist. If an Omarchy environment is unavailable,
prepare those artifacts and continue independent packaging/profiling work while
keeping native qualification visibly pending.
