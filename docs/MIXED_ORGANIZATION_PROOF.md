# Disposable mixed-library organization proof

The labels in `tests/fixtures/mixed_organization_labels.json` were written before running the planner. They are **authored regression controls**, not an independent holdout. The fixture has 15 synthetic files: three project resources to preserve, three linked web files and two named-folder files to retain, six loose root/Downloads/Inbox files eligible for a move, and one unknown-format file needing review. The six expected destinations preserve filenames and use only file type, a shared filename topic, and one explicit EXIF year. A filesystem modification date does not create a year folder.

From the repository root, with its Python environment active:

```powershell
python scripts/prove_mixed_organization.py prepare --output-parent D:\Test
```

The command creates a new `DDH-mixed-organization-*` disposable directory and prints its path. Inspect `plan.json` there before selecting any operation. It records every file's expected and actual destination, boundary classification, suppression reason, file hashes and original directory topology. The planner scope is the deterministic standalone-file pass: no AI model or full organization pipeline is called. The expected proposal denominator is six; the expected no-move denominator is nine. The current authored fixture yields six exact proposed destinations and nine correct no-move decisions. A preserved project or unchanged named folder is a safety result, not newly organized output.

After reviewing one proposal, exercise its selected filesystem cycle on that same disposable directory:

```powershell
python scripts/prove_mixed_organization.py cycle --run-dir D:\Test\DDH-mixed-organization-EXAMPLE --selected-path Inbox/invoice_alpha.pdf
```

The cycle checks the frozen labels, complete source-file manifest, indexed file IDs/paths/names/hashes/statuses, and SQLite quick check and foreign keys. It approves only the selected proposal, runs a targeted dry run, commits the selected move, undoes it, and verifies all original paths, file hashes, directories and database file rows again. `cycle.json` retains the result. It isolates the undo journal under the disposable run directory. If a stage fails, it records the failure and stops without retrying or hiding the partial state.

The focused regression is `python -m pytest tests/test_mixed_organization_proof.py`. It invokes the runner from another working directory without `PYTHONPATH` to verify that the script imports the checkout beside it, even when the Python environment has another editable installation. The authored fixture cannot estimate useful-placement precision for an unseen mixed library, model-driven proposals, naming usefulness, or 100–500 GB capacity. Label a separate holdout before running its planner and review each proposed destination against that independent evidence.
