from config import AUDIT_DIR, LEDGER_PATH, configure_logging, update_run_state
from models import RUNTIME
from network import create_http_session
from sync_ledger import SyncLedger
from sync_service import run_sync


def main():
    if RUNTIME.session is None:
        RUNTIME.session = create_http_session()
    RUNTIME.ledger = SyncLedger(LEDGER_PATH, AUDIT_DIR)
    try:
        configure_logging()
        update_run_state("running")
        run_sync()
    except Exception as error:
        configure_logging()
        failure_details = list(RUNTIME.run_state.get("failure_details", []))
        if not failure_details:
            failure_details.append(str(error))
        jira_issue_key = RUNTIME.run_state.get("jira_issue_key") or "UNKNOWN"
        partial_summary = RUNTIME.ledger.summarize_run(RUNTIME.run_id)
        RUNTIME.ledger.record_decision(
            run_id=RUNTIME.run_id,
            jira_issue_key=jira_issue_key,
            event_type="run",
            decision="failed",
            details={
                "error": str(error),
                "failure_details": failure_details,
                "failure_count": len(failure_details),
                **partial_summary,
            },
        )
        update_run_state(
            "failed",
            error=str(error),
            failure_details=failure_details,
            failure_count=len(failure_details),
            **partial_summary,
        )
        RUNTIME.logger.exception("Run stopped with error: %s", error)
        print("\nSync stopped with failure(s):")
        print(f"Failures: {len(failure_details)}")
        for name, count in partial_summary.items():
            print(f"{name.replace('_', ' ').capitalize()}: {count}")
        for failure in failure_details:
            print(f"- {failure}")
    finally:
        try:
            audit_path = RUNTIME.ledger.export_run(RUNTIME.run_id)
            RUNTIME.logger.info("Audit export written to %s.", audit_path)
        except Exception:
            RUNTIME.logger.exception("Could not export the sync audit record.")
        RUNTIME.ledger.close()


if __name__ == "__main__":
    main()