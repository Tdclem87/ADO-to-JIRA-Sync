# Changelog

Notable user-visible changes are recorded here for each release.

## [Unreleased]

### Changed

- Windows launchers create a local Python environment, check for pip updates, and install requirements automatically.
- First-run setup collects organization prefixes and URLs until `F4` is pressed instead of asking for an organization count.
- Jira issue summaries and ADO work-item matches display automatically, followed by the preflight and a clear post-to-Jira confirmation.
- Generated Jira comments use clear headings, regular-weight details, and a muted sync-reference footer; older bracketed markers remain supported.
- Attachment uploads preserve original ADO filenames when available and infer missing extensions from the response content type.
- Sync rechecks recorded Jira comment IDs and restores deleted field or status comments when their ADO revision snapshot is available; missing ADO comments are reimported from ADO.
- Jira comment failures now include Jira's validation details in the run log and audit export.
- Jira sync-reference footers use the ADF `textColor` mark, avoiding Jira `INVALID_INPUT` errors when posting comments.

## [1.1.0] - 2026-10-09

### Added

- Replay-safe SQLite ledger records with source snapshots, Jira target IDs, and incremental recovery of pending field revisions.
- Bounded retries for transient Azure DevOps and Jira API failures, with reconciliation before retrying uncertain Jira writes.
- Per-run audit exports with sync decisions, run summaries, attachment URL mappings, and partial progress on failures.
- Detailed preflight listing planned comments, state and field changes, attachment outcomes, and large-payload warnings before requiring explicit `YES` confirmation.
- Run-correlated activity logs and configurable continue-on-error or stop-on-error reporting.
- Offline regression tests for retries, ledger exports, and revision recovery.

## [1.0.0] - 2026-10-09

### Added

- Automatic Git fast-forward updates from the Windows launcher.
- F1 README and F2 organization registration shortcuts.
- Jira comments for non-status Azure DevOps field updates, with duplicate detection.
- Plain-text formatting for HTML field values in update comments.
