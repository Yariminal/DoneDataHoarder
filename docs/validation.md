# Real corpus validation

Run from the repository root with the project's virtual environment. The source ZIPs and existing folders in `D:\Test` are never modified. Each `prepare` creates a new, uniquely named `D:\Test\DDH-validation-*` directory. Reports, SQLite, config, logs, and the undo journal live inside that directory; only its `data` child is scanned.

On Windows, set `$env:PYTHONUTF8='1'` and `$env:PYTHONDONTWRITEBYTECODE='1'` before running the commands so non-Latin filenames and Rich progress render reliably.

```powershell
.\.venv\Scripts\python.exe scripts\validate_corpus.py inventory
.\.venv\Scripts\python.exe scripts\validate_corpus.py prepare --corpus Small.zip
```

Use the `run_dir` printed by `prepare` for the next commands. `prepare` validates ZIP paths, rejects links and encrypted entries, preserves each file's ZIP modified time, hashes every extracted file, and confirms the source ZIP hash is unchanged. It does not extract into existing corpus folders.

```powershell
$validationRun = 'D:\Test\DDH-validation-<printed-run-name>'
.\.venv\Scripts\python.exe scripts\validate_corpus.py pipeline --run-dir $validationRun --model gemma4:26b --workers 1 --full-ai --relate --organize
.\.venv\Scripts\python.exe scripts\validate_corpus.py review --run-dir $validationRun
```

The default `gemma4:26b` is the validation choice for this workstation; it does not change the application's default model.

`pipeline` uses only local Ollama and forces Hugging Face offline mode, so optional Whisper weights cannot download. If those weights are absent in the isolated cache, audio transcription is unavailable; the report records installed media tools and explicit skip reasons. It scans, enriches, runs exact/perceptual/text duplicate detection, analyzes, runs semantic duplicate detection and proposals, relates files, and proposes names and organization. The full mode attempts every eligible file. For a bounded AI smoke on a larger corpus, use `--ai-limit 5` instead of `--full-ai`; the run still scans, enriches, and deduplicates the whole copy, but submits at most five files to the analyzer. Relation grouping and organization are skipped in a bounded smoke unless `--relate` and `--organize` are passed. `reports/pipeline.json` records eligible, attempted, succeeded, failed, skipped, and unprocessed counts, actual attempted paths, outcomes, reasons, evidence sources, model tags/digests, and each stage's peak process RSS. `reports/analysis-evidence.json` records per-file provenance, including extractable character count. Model confidence is self-reported, not a calibrated risk score. Provider failures block downstream proposal stages.

`--ai-limit` bounds file analysis only. If `--relate` is included, relation grouping sends all indexed filenames to the local model in chunks; its time and call count are separate from the analysis limit. Relation and organizer log warnings are captured in the stage reports, and a logged LLM fallback fails validation rather than silently counting as full success.

This validation harness splits duplicate work around analysis: exact/perceptual/text checks run first, then semantic duplicate detection uses saved AI descriptions. Its stage order is distinct from both the `ddh pipeline` shortcut and the web Unattended Run described in the README.

If a relation call fails after completed file analysis, preserve `pipeline.json` and the app log, and write `reports/incident.json` with the matching `session_id` and `failed_stage: "relate"`. After fixing the cause, the narrowly scoped retry command resumes relation, naming, and optional organization in the same isolated database without repeating analysis:

```powershell
.\.venv\Scripts\python.exe scripts\validate_corpus.py retry-downstream --run-dir $validationRun --model gemma4:26b --organize
```

Each retry is numbered in `reports/retry-downstream-N.json` and leaves the original failed report and earlier retries intact. A further retry is allowed only after a recorded relation-stage failure, while analysis and duplicate stages remain complete, naming has not started, and no non-duplicate proposals exist. Review and execution are blocked until either the original pipeline or the latest retry completes.

An interrupted full analysis with a saved inference-failure sentinel can be continued on its unchanged copy after recording `reports/incident.json` and a read-only `reports/interrupted-analysis-snapshot.json`. `retry-analysis` verifies the original file hashes and DB statuses, changes only the captured legacy inference-failure row to explicit retryable `ERROR`, then runs the remaining `ENRICHED` files plus that error with one local worker. It keeps prior valid analyses and records both unique-file coverage and total attempt events, then runs the downstream stages only if all initially eligible files finish without provider failure. Its report is `reports/retry-analysis-1.json`.

```powershell
.\.venv\Scripts\python.exe scripts\validate_corpus.py retry-analysis --run-dir $validationRun --model gemma4:26b
```

For a proposal-side safety or naming fix after a completed full pipeline, preserve the initial reports and a consistent SQLite snapshot **before** refreshing. Do this once, before any approval or execution, while no validation process is writing that run's database. Keep the original snapshot; do not overwrite it on a later retry.

```powershell
Copy-Item -LiteralPath "$validationRun\reports\pipeline.json" -Destination "$validationRun\reports\initial-pipeline.json"
Copy-Item -LiteralPath "$validationRun\reports\review.json" -Destination "$validationRun\reports\initial-review.json"
.\.venv\Scripts\python.exe -c "import sqlite3,sys; source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2]); source.backup(target); target.close(); source.close()" "$validationRun\state\corpus.sqlite" "$validationRun\reports\initial-proposals.sqlite"
.\.venv\Scripts\python.exe scripts\validate_corpus.py refresh-proposals --run-dir $validationRun --model gemma4:26b
.\.venv\Scripts\python.exe scripts\validate_corpus.py review --run-dir $validationRun
```

`refresh-proposals` verifies that the disposable copy and source ZIP still match their baseline hashes, that the initial database has no reviewed or applied proposals, and that a refresh has not already run. It preserves existing duplicate proposals and AI evidence, removes only pending non-duplicate proposals, and reruns naming and organization without repeating file analysis or relation grouping. The original report/DB snapshot and numbered `reports/proposal-refresh-1.json` make the before/after decision reviewable. If the fix changes relation grouping or extraction itself, prepare a new isolated run instead.

Inspect `reports/review.json` and choose proposal IDs for a safe filesystem subset. The execution phase supports `RENAME`, `MOVE`, and `MARK_DUPLICATE`; it excludes `ADD_TAGS` because metadata writes are not covered by filesystem undo. It records each selected ID as individually reviewed, runs a dry run, commits only those IDs on the disposable copy, undoes that session's journal, and compares every file's relative path and SHA256 with the extraction baseline. It also checks database paths, directory topology, and the source ZIP hash after undo. Each run allows one execution cycle.

A selection may contain one `RENAME` followed by one `MOVE` for the same file when both proposals have the same original source, the rename proposal has the earlier ID, and the move destination basename matches the proposed new name. The ordered preview shows the cascaded destination. Other multiple selections for one file are rejected so the harness does not silently guess an execution order.

The pass gate requires directory topology to be restored, including empty directories. If review finds no safe filesystem operation, use `--no-safe-ops`; its report explicitly says that no mutation was exercised while still checking the untouched copy and source ZIP.

```powershell
.\.venv\Scripts\python.exe scripts\validate_corpus.py execute --run-dir $validationRun --proposal-ids 12 19
.\.venv\Scripts\python.exe scripts\validate_corpus.py execute --run-dir $validationRun --no-safe-ops
```

Outputs remain in `reports/` for review. A failed phase keeps the copy and report for diagnosis; prepare a new run directory for another complete attempt unless the explicit failed-relation retry above applies. ZIP modified times are preserved, while filesystem creation times reflect extraction on Windows.
