import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class SyncLedger:
    def __init__(self, database_path, audit_dir):
        self.database_path = Path(database_path)
        self.audit_dir = Path(audit_dir)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_events (
                source_org TEXT NOT NULL,
                work_item_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                jira_issue_key TEXT NOT NULL,
                status TEXT NOT NULL,
                target_id TEXT,
                details_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                run_id TEXT NOT NULL,
                PRIMARY KEY (
                    source_org, work_item_id, event_type, source_id, jira_issue_key
                )
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                jira_issue_key TEXT NOT NULL,
                source_org TEXT,
                work_item_id TEXT,
                event_type TEXT NOT NULL,
                source_id TEXT,
                decision TEXT NOT NULL,
                target_id TEXT,
                details_json TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def get_event(
        self, source_org, work_item_id, event_type, source_id, jira_issue_key
    ):
        row = self.connection.execute(
            """
            SELECT * FROM sync_events
            WHERE source_org = ? AND work_item_id = ? AND event_type = ?
              AND source_id = ? AND jira_issue_key = ?
            """,
            (source_org, str(work_item_id), event_type, str(source_id), jira_issue_key),
        ).fetchone()
        if row is None:
            return None
        event = dict(row)
        event["details"] = json.loads(event.pop("details_json"))
        return event

    def complete_event(
        self,
        source_org,
        work_item_id,
        event_type,
        source_id,
        jira_issue_key,
        status,
        target_id=None,
        details=None,
        run_id="",
    ):
        self.connection.execute(
            """
            INSERT INTO sync_events (
                source_org, work_item_id, event_type, source_id, jira_issue_key,
                status, target_id, details_json, updated_at, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                source_org, work_item_id, event_type, source_id, jira_issue_key
            ) DO UPDATE SET
                status = excluded.status,
                target_id = excluded.target_id,
                details_json = excluded.details_json,
                updated_at = excluded.updated_at,
                run_id = excluded.run_id
            """,
            (
                source_org,
                str(work_item_id),
                event_type,
                str(source_id),
                jira_issue_key,
                status,
                target_id,
                json.dumps(details or {}, ensure_ascii=False, default=str),
                datetime.now(timezone.utc).isoformat(),
                run_id,
            ),
        )
        self.connection.commit()

    def record_decision(
        self,
        run_id,
        jira_issue_key,
        event_type,
        decision,
        source_org=None,
        work_item_id=None,
        source_id=None,
        target_id=None,
        details=None,
    ):
        self.connection.execute(
            """
            INSERT INTO sync_decisions (
                run_id, created_at, jira_issue_key, source_org, work_item_id,
                event_type, source_id, decision, target_id, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                datetime.now(timezone.utc).isoformat(),
                jira_issue_key,
                source_org,
                str(work_item_id) if work_item_id is not None else None,
                event_type,
                str(source_id) if source_id is not None else None,
                decision,
                target_id,
                json.dumps(details or {}, ensure_ascii=False, default=str),
            ),
        )
        self.connection.commit()

    def get_field_revisions_to_check(
        self, source_org, work_item_id, jira_issue_key, latest_revision
    ):
        latest_revision = int(latest_revision or 0)
        if latest_revision <= 1:
            return []

        rows = self.connection.execute(
            """
            SELECT source_id, status FROM sync_events
            WHERE source_org = ? AND work_item_id = ? AND event_type = 'fields'
              AND jira_issue_key = ?
            """,
            (source_org, str(work_item_id), jira_issue_key),
        ).fetchall()
        completed_revisions = set()
        completed_statuses = {"posted", "reconciled", "existing", "no_changes"}
        for row in rows:
            try:
                revision = int(row["source_id"])
            except (TypeError, ValueError):
                continue
            if row["status"] in completed_statuses:
                completed_revisions.add(revision)

        for revision in range(2, latest_revision + 1):
            if revision not in completed_revisions:
                return list(range(revision, latest_revision + 1))
        return []

    def summarize_run(self, run_id):
        rows = self.connection.execute(
            """
            SELECT event_type, decision, COUNT(*) AS total
            FROM sync_decisions WHERE run_id = ?
            GROUP BY event_type, decision
            """,
            (run_id,),
        ).fetchall()
        counts = {
            (row["event_type"], row["decision"]): row["total"] for row in rows
        }

        def count(event_type, decisions):
            return sum(counts.get((event_type, decision), 0) for decision in decisions)

        return {
            "posted_comments": count("comment", {"posted", "reconciled"}),
            "posted_status_comments": count("state", {"posted", "reconciled"}),
            "skipped_status_updates": count(
                "state", {"skipped_existing", "skipped_ledger"}
            ),
            "posted_field_update_comments": count("fields", {"posted", "reconciled"}),
            "skipped_field_revisions": count(
                "fields", {"skipped_existing", "skipped_ledger"}
            ),
            "uploaded_attachments": count("attachment", {"uploaded"}),
            "existing_attachments": count("attachment", {"uploaded_existing"}),
            "linked_attachments": count("attachment", {"linked"}),
            "skipped_duplicates": count(
                "comment", {"skipped_existing", "skipped_ledger"}
            ),
        }

    def export_run(self, run_id):
        rows = self.connection.execute(
            """
            SELECT created_at, jira_issue_key, source_org, work_item_id,
                   event_type, source_id, decision, target_id, details_json
            FROM sync_decisions WHERE run_id = ? ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        decisions = []
        for row in rows:
            decision = dict(row)
            decision["details"] = json.loads(decision.pop("details_json"))
            decisions.append(decision)

        attachment_rows = self.connection.execute(
            """
            SELECT source_org, work_item_id, source_id, status, target_id,
                   details_json
            FROM sync_events
            WHERE run_id = ? AND event_type = 'attachment'
              AND status IN ('uploaded', 'uploaded_existing', 'linked')
            ORDER BY updated_at
            """,
            (run_id,),
        ).fetchall()
        attachment_url_mapping = []
        for row in attachment_rows:
            attachment = dict(row)
            details = json.loads(attachment.pop("details_json"))
            attachment_url_mapping.append(
                {
                    "source_url": attachment["source_id"],
                    "jira_url": details.get("content_url")
                    or attachment["source_id"],
                    "filename": details.get("filename"),
                    "delivery": attachment["status"],
                    "jira_target_id": attachment["target_id"],
                    "source_org": attachment["source_org"],
                    "work_item_id": attachment["work_item_id"],
                }
            )

        run_summary = next(
            (
                decision["details"]
                for decision in reversed(decisions)
                if decision["event_type"] == "run"
            ),
            {},
        )

        export_path = self.audit_dir / f"sync-{run_id}.json"
        temporary_path = export_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "summary": run_summary,
                    "decisions": decisions,
                    "attachment_url_mapping": attachment_url_mapping,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary_path.replace(export_path)
        return export_path

    def close(self):
        self.connection.close()