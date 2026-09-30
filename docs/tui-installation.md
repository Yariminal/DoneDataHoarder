# Installing the terminal candidate

For a source installation of the current preview branch, start with the
[installation guide](../INSTALL.md). The procedure below is for testing built
wheel and source artifacts before distributing a candidate.

This is the installation path for a locally built, unpublished candidate. Use
Python 3.12 or newer for the TUI; the existing CLI still supports Python 3.10+.
Installation is isolated from Arch's system Python and does not change terminal,
compositor, or Ollama settings.

The preferred application installer is [uv's isolated tool installation](https://docs.astral.sh/uv/guides/tools/).
Install uv through your existing package-management process. Given a candidate
wheel and its qualification constraints for your operating system/Python, run:

```bash
uv tool install --python 3.12 \
  --constraints /absolute/path/tui-distribution-report.constraints.txt \
  '/absolute/path/donedatahoarder-0.6.0-py3-none-any.whl[tui,docs]'
ddh --help
ddh tui-diagnostics --images auto --output terminal-report.json
ddh tui ~/Downloads
```

Replace the wheel path and version with the candidate you actually received.
The `docs` extra enables PDF and Office extraction. Video transcription and
cloud providers are separate optional extras. No model is downloaded by this
installation; metadata-only processing works without Ollama. Full analysis
requires the selected local Ollama model.

If uv reports that its executable directory is outside `PATH`, follow its shell
setup instructions before launching `ddh`. Normal launches do not need an
activated virtual environment or a source checkout. The native inline-image
acceptance procedure is in [terminal qualification](tui-qualification.md);
successful installation does not establish that a terminal renders photos.

For a different candidate, repeat the install command with its wheel and matching
constraints. If deliberately reinstalling the same version during qualification,
add `--force`. Close DDH processes first, preserve a backup of the index and
journal before a version migration, then reopen a saved session:

```bash
ddh tui --session SAVED_SESSION_ID
```

The default index is `$XDG_DATA_HOME/donedatahoarder/index.db`, or
`~/.local/share/donedatahoarder/index.db` when `XDG_DATA_HOME` is unset. If a session
used `--db`, resume with the same database. `uv tool uninstall donedatahoarder`
removes the isolated application and entrypoint; it does not remove your
collection, saved index, or journal. Same-version reinstall is exercised by the
artifact checker. Cross-version migration and a fresh Omarchy-account install
remain separate release checks.

## Build and verify a candidate

From a source checkout, use an isolated build environment:

```bash
python3.12 -m venv .build-venv
.build-venv/bin/python -m pip install 'build>=1.2,<2.0' 'uv>=0.8,<1.0'
.build-venv/bin/python -m build --outdir dist/tui-candidate
.build-venv/bin/python scripts/check_tui_distribution.py \
  --dist dist/tui-candidate \
  --uv .build-venv/bin/uv \
  --report dist/tui-candidate/tui-distribution-report.json
```

On Windows, the corresponding executables are under `.build-venv/Scripts/`.
Use an output folder containing exactly one wheel and one sdist. The build uses
the current source contents, including uncommitted changes; the verifier rejects
a wheel when packaged modules differ from the checkout at verification time.

The checker creates temporary environments outside the repository and:

- Verifies TUI modules, application assets, and Python version markers, including
  rejecting deleted modules retained by an old build directory.
- Installs the wheel with `tui,docs,remote,nearby`, checks dependencies, and calls the installed
  main, TUI, fixture, and diagnostics help commands.
- Generates a disposable qualification fixture through the installed CLI and
  writes an images-off diagnostics report that retains `qualification: not_run`.
- Runs the actual metadata-only pipeline on two disposable PNG copies, confirms
  the duplicate proposal, photo dimensions and unchanged source bytes, opens the headless Textual
  workspace, and resumes the persisted session in a new process.
- Rebuilds a wheel from the sdist, compares all application payload hashes and
  version/dependency/entrypoint metadata,
  reinstalls it, and repeats the checks outside the source checkout.
- Exercises the documented uv wheel/extras installation, forced reinstall,
  saved-session relaunch, and uninstall using temporary tool/cache directories.
- Exercises disposable workstation pairing and device revocation with the
  installed package. This does not qualify physical LAN discovery or a GPU.
- Writes artifact hashes, runtime and dependency versions, smoke results, and
  matching `.constraints.txt` into the chosen report directory.

The constraints record the versions actually exercised on that report's
platform/Python. Keep them alongside the candidate and report; Windows results
are not a Linux constraints set. A portable, hash-locked release environment is
not yet qualified. CI repeats these checks on Linux with Python 3.12 and 3.14;
3.14 is the current line on the [official Arch Python package page](https://archlinux.org/packages/core/x86_64/python/)
checked on 2026-09-29. A configured CI job is not evidence that its run passed.

No package is published by this workflow. AUR packaging, a fresh Omarchy install,
cross-version upgrade, and native terminal graphics are pending acceptance work.
