# Changelog

Notable user-visible changes are recorded here for each release.

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
