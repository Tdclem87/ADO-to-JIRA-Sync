import json
import io
import sys
import tempfile
import unittest
from contextlib import ExitStack
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import sync_service
from network import call_with_retry
from sync_ledger import SyncLedger
from transforms import get_attachment_filename, jira_comment_matching_text


class ConfigurationSetupTests(unittest.TestCase):
    def test_initial_setup_collects_organizations_until_f4(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.json"
            with ExitStack() as stack:
                stack.enter_context(patch.object(config, "CONFIG_PATH", config_path))
                read_prefix = stack.enter_context(
                    patch.object(
                        config,
                        "read_console_input_or_function_key",
                        side_effect=["MBSD", "FLEET", "F4"],
                    )
                )
                read_other_values = stack.enter_context(
                    patch.object(
                        config,
                        "get_required_input",
                        side_effect=[
                            "https://dev.azure.com/mbsd",
                            "https://dev.azure.com/fleet",
                            "https://jira.example",
                            "user@example.com",
                        ],
                    )
                )
                stack.enter_context(patch.object(config.RUNTIME, "logger"))
                with redirect_stdout(io.StringIO()):
                    config.prompt_for_configuration()

            saved_configuration = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(
            saved_configuration["ado_configs"],
            {
                "MBSD": {"organization_url": "https://dev.azure.com/mbsd"},
                "FLEET": {"organization_url": "https://dev.azure.com/fleet"},
            },
        )
        self.assertEqual(read_prefix.call_count, 3)
        self.assertEqual(read_other_values.call_count, 4)


class SyncLedgerTests(unittest.TestCase):
    def test_attachment_filename_prefers_ado_download_filename(self):
        filename = get_attachment_filename(
            {
                "url": "https://ado.example/_apis/wit/attachments/id",
                "name": "Image",
            },
            content_disposition='attachment; filename="customer-logo.png"',
            content_type="image/png",
        )

        self.assertEqual(filename, "customer-logo.png")

    def test_attachment_filename_uses_url_filename_and_adds_mime_extension(self):
        filename = get_attachment_filename(
            {
                "url": "https://ado.example/_apis/wit/attachments/id?fileName=invoice",
                "name": "Image",
            },
            content_type="application/pdf",
        )

        self.assertEqual(filename, "invoice.pdf")

    def test_attachment_filename_preserves_extensionless_ado_name_for_binary_type(self):
        filename = get_attachment_filename(
            {
                "url": "https://ado.example/_apis/wit/attachments/id",
                "name": "customer-document",
            },
            content_type="application/octet-stream",
        )

        self.assertEqual(filename, "customer-document")

    def test_attachment_filename_adds_image_extension_to_generic_alt_text(self):
        filename = get_attachment_filename(
            {
                "url": "https://ado.example/_apis/wit/attachments/"
                "7ce97e52-465a-4f4f-a00a-3db5ce37d287",
                "name": "Image",
            },
            content_type="image/jpeg",
        )

        self.assertEqual(filename, "attachment.jpg")

    def test_attachment_upload_uses_ado_filename_and_image_content_type(self):
        source_response = MagicMock()
        source_response.__enter__.return_value = source_response
        source_response.headers = {
            "Content-Disposition": 'attachment; filename="customer-logo"',
            "Content-Type": "image/png",
            "Content-Length": "5",
        }
        source_response.iter_content.return_value = [b"image"]
        upload_response = Mock()
        upload_response.status_code = 200
        upload_response.json.return_value = [
            {"id": "jira-attachment", "filename": "customer-logo.png"}
        ]
        multipart_fields = {}

        class MultipartPayload:
            content_type = "multipart/form-data; boundary=test"

        def create_payload(fields):
            multipart_fields.update(fields)
            return MultipartPayload()

        with ExitStack() as stack:
            config = stack.enter_context(
                patch.object(sync_service.jira_client.RUNTIME, "config")
            )
            config.jira_base_url = "https://jira.example"
            config.jira_email = "user@example.com"
            config.max_attachment_bytes = 1024
            session = stack.enter_context(
                patch.object(sync_service.jira_client.RUNTIME, "session")
            )
            session.get.return_value = source_response
            session.post.return_value = upload_response
            stack.enter_context(
                patch.object(
                    sync_service.jira_client, "MultipartEncoder", side_effect=create_payload
                )
            )

            uploaded, _, _ = sync_service.jira_client.upload_jira_attachment(
                "PROJ-7",
                {
                    "url": "https://ado.example/_apis/wit/attachments/id",
                    "name": "Image",
                },
                "ado-token",
                "jira-token",
                set(),
            )

        self.assertEqual(uploaded["filename"], "customer-logo.png")
        self.assertEqual(multipart_fields["file"][0], "customer-logo.png")
        self.assertEqual(multipart_fields["file"][2], "image/png")

    def test_jira_comments_use_clear_headers_and_unbolded_body(self):
        response = MagicMock()
        response.status_code = 201
        response.json.return_value = {"id": "comment-1"}
        with ExitStack() as stack:
            config = stack.enter_context(
                patch.object(sync_service.jira_client.RUNTIME, "config")
            )
            config.jira_base_url = "https://jira.example"
            config.jira_email = "user@example.com"
            session = stack.enter_context(
                patch.object(sync_service.jira_client.RUNTIME, "session")
            )
            session.post.return_value = response

            sync_service.jira_client.add_jira_comment(
                "PROJ-7",
                "Azure DevOps field update\nWork item: 523291\nRevision: 26",
                "Changed by: K. Melisa\n\nUpdated fields:\nColumn: Old -> New",
                [],
                "jira-token",
                sync_marker="[ADO_TO_JIRA_FIELDS:abc123]",
            )

        content = session.post.call_args.kwargs["json"]["body"]["content"]
        self.assertEqual(content[0]["type"], "heading")
        self.assertEqual(content[0]["attrs"], {"level": 3})
        self.assertEqual(content[0]["content"][0]["text"], "Azure DevOps field update")
        self.assertEqual(content[1]["type"], "paragraph")
        self.assertEqual(content[2]["type"], "paragraph")
        self.assertNotIn("marks", content[2]["content"][0])
        self.assertNotIn(
            "ADO_TO_JIRA",
            "".join(item.get("text", "") for item in content[0]["content"]),
        )
        footer = content[-1]["content"][0]
        self.assertEqual(
            footer["text"], "Sync reference: ADO_TO_JIRA_FIELDS:abc123"
        )
        self.assertEqual(footer["marks"][0]["type"], "textColor")
        self.assertEqual(footer["marks"][0]["attrs"]["color"], "#6B778C")

    def test_jira_comment_http_error_includes_validation_details(self):
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError(
            "400 Client Error: Bad Request"
        )
        response.json.return_value = {
            "errorMessages": ["Comment body is not valid!"],
            "errors": {"body": "Invalid document"},
            "unrelated": "not included",
        }
        with ExitStack() as stack:
            config = stack.enter_context(
                patch.object(sync_service.jira_client.RUNTIME, "config")
            )
            config.jira_base_url = "https://jira.example"
            config.jira_email = "user@example.com"
            session = stack.enter_context(
                patch.object(sync_service.jira_client.RUNTIME, "session")
            )
            session.post.return_value = response

            with self.assertRaises(requests.HTTPError) as raised:
                sync_service.jira_client.add_jira_comment(
                    "PROJ-7",
                    "Azure DevOps field update\nWork item: 523291\nRevision: 26",
                    "Updated field: Old -> New",
                    [],
                    "jira-token",
                    sync_marker="[ADO_TO_JIRA_FIELDS:abc123]",
                )

        self.assertIn("Comment body is not valid!", str(raised.exception))
        self.assertIn("Invalid document", str(raised.exception))
        self.assertNotIn("unrelated", str(raised.exception))
        self.assertIs(raised.exception.response, response)

    def test_comment_matching_supports_markerless_and_legacy_comments(self):
        header = "Azure DevOps field update\nWork item: 523291\nRevision: 26"
        body = "Changed by: K. Melisa\nUpdated fields:\nColumn: Old -> New"
        current_comment = {
            "id": "current",
            "body": {
                "type": "doc",
                "content": [
                    {
                        "type": "heading",
                        "content": [{"type": "text", "text": "Azure DevOps field update"}],
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "Work item: 523291\nRevision: 26"}
                        ],
                    },
                    {"type": "paragraph", "content": [{"type": "text", "text": body}]},
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Sync reference: ADO_TO_JIRA_FIELDS:new-marker",
                            }
                        ],
                    },
                ],
            },
        }
        legacy_comment = {
            "id": "legacy",
            "body": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "[ADO_TO_JIRA_FIELDS:523291:26]",
                            }
                        ],
                    }
                ],
            },
        }

        markerless_match = sync_service._find_jira_comment(
            [current_comment],
            marker="[ADO_TO_JIRA_FIELDS:new-marker]",
            legacy_text=[
                jira_comment_matching_text(header, body),
                "[ADO_TO_JIRA_FIELDS:523291:26]",
            ],
        )
        legacy_match = sync_service._find_jira_comment(
            [legacy_comment],
            marker="[new-marker]",
            legacy_text=["[ADO_TO_JIRA_FIELDS:523291:26]"],
        )

        self.assertIs(markerless_match, current_comment)
        self.assertIs(legacy_match, legacy_comment)

    def test_work_item_event_identity_keeps_same_revision_items_distinct(self):
        first_item = sync_service._work_item_event_identity(
            "https://ado.example/org", 42, 7
        )
        second_item = sync_service._work_item_event_identity(
            "https://ado.example/org", 43, 7
        )

        self.assertNotEqual(first_item, second_item)

    def test_field_revision_window_resumes_from_pending_revision(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ledger = SyncLedger(root / "ledger.sqlite3", root / "audit")
            for revision in range(2, 9):
                ledger.complete_event(
                    "https://ado.example/org",
                    42,
                    "fields",
                    str(revision),
                    "PROJ-7",
                    "posted",
                    run_id="run-one",
                )
            ledger.complete_event(
                "https://ado.example/org",
                42,
                "fields",
                "9",
                "PROJ-7",
                "failed",
                run_id="run-one",
            )

            self.assertEqual(
                ledger.get_field_revisions_to_check(
                    "https://ado.example/org", 42, "PROJ-7", 11
                ),
                [9, 10, 11],
            )
            ledger.close()

    def test_ledger_reports_posted_comments_missing_from_jira(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ledger = SyncLedger(root / "ledger.sqlite3", root / "audit")
            ledger.complete_event(
                "https://ado.example/org",
                42,
                "fields",
                "8",
                "PROJ-7",
                "posted",
                target_id="field-comment-deleted",
                details={"revision": 8},
                run_id="run-one",
            )
            ledger.complete_event(
                "https://ado.example/org",
                42,
                "fields",
                "9",
                "PROJ-7",
                "posted",
                target_id="field-comment-present",
                details={"revision": 9},
                run_id="run-one",
            )
            ledger.complete_event(
                "https://ado.example/org",
                42,
                "state",
                "Active:2026-10-10",
                "PROJ-7",
                "posted",
                target_id="status-comment-deleted",
                details={"source": {"work_item_id": 42, "state": "Active"}},
                run_id="run-one",
            )

            missing = ledger.get_comment_events_with_missing_targets(
                "https://ado.example/org",
                42,
                "PROJ-7",
                {"field-comment-present"},
            )

            self.assertEqual(
                {(event["event_type"], event["source_id"]) for event in missing},
                {("fields", "8"), ("state", "Active:2026-10-10")},
            )
            self.assertEqual(
                next(
                    event
                    for event in missing
                    if event["event_type"] == "state"
                )["details"]["source"]["state"],
                "Active",
            )
            ledger.close()

    def test_completed_ledger_event_is_present_only_when_jira_has_its_target(self):
        event = {"status": "posted", "target_id": "comment-8"}

        self.assertTrue(
            sync_service._ledger_comment_is_present(event, {"comment-8"})
        )
        self.assertFalse(
            sync_service._ledger_comment_is_present(event, set())
        )

    def test_field_revision_window_recovers_gaps_before_latest_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ledger = SyncLedger(root / "ledger.sqlite3", root / "audit")
            ledger.complete_event(
                "https://ado.example/org",
                42,
                "fields",
                "14",
                "PROJ-7",
                "no_changes",
                run_id="run-one",
            )

            self.assertEqual(
                ledger.get_field_revisions_to_check(
                    "https://ado.example/org", 42, "PROJ-7", 14
                ),
                list(range(2, 15)),
            )
            ledger.close()

    def test_export_includes_run_summary_and_attachment_mapping(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ledger = SyncLedger(root / "ledger.sqlite3", root / "audit")
            ledger.complete_event(
                "https://ado.example/org",
                42,
                "attachment",
                "https://ado.example/file/1",
                "PROJ-7",
                "uploaded",
                target_id="jira-file-9",
                details={
                    "filename": "image.png",
                    "content_url": "https://jira.example/file/9",
                },
                run_id="run-test",
            )
            ledger.record_decision(
                "run-test", "PROJ-7", "run", "completed", details={"failures": []}
            )
            ledger.record_decision("run-test", "PROJ-7", "comment", "posted")
            ledger.record_decision(
                "run-test", "PROJ-7", "state", "skipped_existing"
            )

            export = json.loads(
                ledger.export_run("run-test").read_text(encoding="utf-8")
            )

            self.assertEqual(export["summary"], {"failures": []})
            run_summary = ledger.summarize_run("run-test")
            self.assertEqual(run_summary["posted_comments"], 1)
            self.assertEqual(run_summary["skipped_status_updates"], 1)
            self.assertEqual(
                export["attachment_url_mapping"][0]["source_url"],
                "https://ado.example/file/1",
            )
            self.assertEqual(
                export["attachment_url_mapping"][0]["jira_url"],
                "https://jira.example/file/9",
            )
            ledger.close()

    def test_sync_confirmation_accepts_case_insensitive_y_or_n(self):
        with patch("builtins.input", side_effect=["Y", "n", "YeS", "N"]) as prompt:
            self.assertTrue(sync_service._confirm_sync("PROJ-7"))
            self.assertFalse(sync_service._confirm_sync("PROJ-7"))
            self.assertTrue(sync_service._confirm_sync("PROJ-7"))
            self.assertFalse(sync_service._confirm_sync("PROJ-7"))

        self.assertEqual(
            prompt.call_args_list[0].args[0],
            "Post the planned updates to Jira issue PROJ-7? (y/n): ",
        )

    def test_previously_synced_comment_is_reimported_when_missing_from_jira(self):
        posted_event = {"status": "posted"}

        self.assertTrue(
            sync_service._was_synced_comment_deleted(posted_event, None)
        )
        self.assertFalse(
            sync_service._was_synced_comment_deleted(
                posted_event, {"id": "jira-comment-1"}
            )
        )

    def test_jira_issue_data_loads_all_comment_pages(self):
        issue_response = Mock()
        issue_response.raise_for_status.return_value = None
        issue_response.json.return_value = {
            "fields": {"summary": "ADO 526950", "attachment": []}
        }
        first_page = Mock()
        first_page.raise_for_status.return_value = None
        first_page.json.return_value = {
            "total": 3,
            "comments": [{"id": "1"}, {"id": "2"}],
        }
        second_page = Mock()
        second_page.raise_for_status.return_value = None
        second_page.json.return_value = {
            "total": 3,
            "comments": [{"id": "3"}],
        }
        with patch.object(sync_service.jira_client.RUNTIME, "config") as config:
            config.jira_base_url = "https://jira.example"
            config.jira_email = "user@example.com"
            with patch.object(sync_service.jira_client.RUNTIME, "session") as session:
                session.get.side_effect = [issue_response, first_page, second_page]
                summary, attachments, comments = (
                    sync_service.jira_client.get_jira_issue_data("PROJ-7", "token")
                )

        self.assertEqual(summary, "ADO 526950")
        self.assertEqual(attachments, [])
        self.assertEqual([comment["id"] for comment in comments], ["1", "2", "3"])
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["startAt"], 0)
        self.assertEqual(session.get.call_args_list[2].kwargs["params"]["startAt"], 2)

    def test_acceptance_criteria_field_update_is_not_ignored(self):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "rev": 7,
            "revisedBy": {"displayName": "Caleb Irving"},
            "revisedDate": "2026-10-10T00:00:00Z",
            "fields": {
                "Microsoft.VSTS.Common.AcceptanceCriteria": {
                    "oldValue": "<p>old</p>",
                    "newValue": "<p>new</p>",
                },
                "Custom.RootCause": {"oldValue": None, "newValue": "Data issue"},
                "Custom.RootCauseDescription": {
                    "oldValue": "Previous explanation",
                    "newValue": "Updated explanation",
                },
                "Custom.Priority": {"oldValue": "3", "newValue": "2"},
            },
        }
        with patch.object(sync_service.RUNTIME, "session") as session:
            session.get.return_value = mock_response
            update = sync_service.ado_client.get_work_item_field_update(
                "https://ado.example/org", 42, 7, "token"
            )
        self.assertIsNotNone(update)
        self.assertIn("Microsoft.VSTS.Common.AcceptanceCriteria", update["fields"])
        self.assertEqual(update["changed_by"], "Caleb Irving")
        self.assertNotIn("state_change", update)
        body = sync_service._format_field_update_body(update)
        self.assertIn("Changed by: Caleb Irving", body)
        self.assertIn("Acceptance Criteria: old -> new", body)
        self.assertIn("Root Cause: (empty) -> Data issue", body)
        self.assertIn(
            "Root Cause Description: Previous explanation -> Updated explanation",
            body,
        )
        self.assertIn("Priority: 3 -> 2", body)

    def test_state_change_is_taken_from_revision_with_its_author(self):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "rev": 7,
            "revisedBy": {"displayName": "Caleb Irving"},
            "revisedDate": "2026-10-05T00:00:00Z",
            "fields": {
                "System.State": {"oldValue": "Committed", "newValue": "Done"},
                "Microsoft.VSTS.Common.AcceptanceCriteria": {
                    "oldValue": "old criteria",
                    "newValue": "new criteria",
                },
            },
        }
        with patch.object(sync_service.RUNTIME, "session") as session:
            session.get.return_value = mock_response
            update = sync_service.ado_client.get_work_item_field_update(
                "https://ado.example/org", 42, 7, "token"
            )

        self.assertEqual(update["state_change"], {"old_value": "Committed", "new_value": "Done"})
        self.assertEqual(update["changed_by"], "Caleb Irving")
        self.assertIn("Microsoft.VSTS.Common.AcceptanceCriteria", update["fields"])
        body = sync_service._format_status_update_body(
            {
                "old_state": "Committed",
                "state": "Done",
                "changed_by": update["changed_by"],
                "changed_date": update["changed_date"],
            }
        )
        self.assertIn("Committed to Done", body)
        self.assertIn("Caleb Irving", body)

    def test_only_latest_state_transition_is_returned_per_work_item(self):
        earlier = {"work_item_id": 42, "revision": 12, "state": "Committed"}
        latest = {"work_item_id": 42, "revision": 14, "state": "Done"}

        self.assertEqual(
            sync_service._latest_state_updates([earlier, latest]), [latest]
        )


class CommentRetryTests(unittest.TestCase):
    def _server_error(self):
        response = requests.Response()
        response.status_code = 503
        return requests.HTTPError("temporary Jira failure", response=response)

    def test_retries_503_after_marker_is_absent(self):
        with ExitStack() as stack:
            post_comment = stack.enter_context(patch.object(
                sync_service.jira_client,
                "add_jira_comment",
                side_effect=[self._server_error(), {"id": "comment-1"}],
            ))
            stack.enter_context(patch.object(
                sync_service.jira_client,
                "get_jira_issue_data",
                return_value=("", [], []),
            ))
            stack.enter_context(patch.object(sync_service, "_find_jira_comment", return_value=None))
            stack.enter_context(patch.object(sync_service.time, "sleep"))
            result, outcome = sync_service._post_or_reconcile_comment(
                "PROJ-7", "marker", "body", [], "token", "marker"
            )

        self.assertEqual(result["id"], "comment-1")
        self.assertEqual(outcome, "posted")
        self.assertEqual(post_comment.call_count, 2)

    def test_reconciles_503_when_marker_is_present(self):
        existing = {"id": "comment-1"}
        with ExitStack() as stack:
            post_comment = stack.enter_context(patch.object(
                sync_service.jira_client,
                "add_jira_comment",
                side_effect=self._server_error(),
            ))
            stack.enter_context(patch.object(
                sync_service.jira_client,
                "get_jira_issue_data",
                return_value=("", [], [{"id": "comment-1"}]),
            ))
            stack.enter_context(patch.object(
                sync_service, "_find_jira_comment", return_value=existing
            ))
            result, outcome = sync_service._post_or_reconcile_comment(
                "PROJ-7", "marker", "body", [], "token", "marker"
            )

        self.assertIs(result, existing)
        self.assertEqual(outcome, "reconciled")
        post_comment.assert_called_once()

    def test_rate_limit_retry_recreates_stream_payload(self):
        class Payload:
            def __init__(self, content_type):
                self.content_type = content_type

        rate_limited = Mock(status_code=429, headers={})
        success = Mock(status_code=200)
        payloads = iter([Payload("multipart/first"), Payload("multipart/second")])
        with ExitStack() as stack:
            session = stack.enter_context(patch.object(sync_service.jira_client.RUNTIME, "session"))
            stack.enter_context(patch.object(sync_service.jira_client.time, "sleep"))
            session.post.side_effect = [rate_limited, success]

            response = sync_service.jira_client._post_with_rate_limit_retry(
                "https://jira.example/attachments",
                data_factory=lambda: next(payloads),
                headers={"X-Atlassian-Token": "no-check"},
            )

        self.assertIs(response, success)
        first_payload = session.post.call_args_list[0].kwargs["data"]
        second_payload = session.post.call_args_list[1].kwargs["data"]
        self.assertIsNot(first_payload, second_payload)
        self.assertEqual(
            session.post.call_args_list[1].kwargs["headers"]["Content-Type"],
            "multipart/second",
        )

    def test_attachment_reconciliation_ignores_preexisting_same_name(self):
        existing = {"id": "attachment-old", "filename": "report.pdf"}
        uploaded = {"id": "attachment-new", "filename": "report.pdf"}
        with patch.object(
            sync_service.jira_client,
            "get_jira_issue_data",
            return_value=("", [existing, uploaded], []),
        ):
            result = sync_service.jira_client._find_new_jira_attachment(
                "PROJ-7", "report.pdf", "token", {"attachment-old"}
            )

        self.assertEqual(result["id"], "attachment-new")
        self.assertTrue(result["_reconciled"])


class AzureRetryTests(unittest.TestCase):
    def test_retries_transient_sdk_server_error(self):
        response = requests.Response()
        response.status_code = 503
        transient_error = requests.HTTPError("temporary Azure failure", response=response)
        operation = Mock(side_effect=[transient_error, "work-item"])

        with patch("network.time.sleep"):
            result = call_with_retry(operation, 42)

        self.assertEqual(result, "work-item")
        self.assertEqual(operation.call_count, 2)

    def test_attachment_source_snapshot_excludes_credentials(self):
        source = sync_service._attachment_source_record(
            {
                "url": "https://ado.example/file/1",
                "name": "image.png",
                "work_item_id": 42,
                "organization_url": "https://ado.example/org",
                "pat": "must-not-be-stored",
            }
        )

        self.assertNotIn("pat", source)
        self.assertEqual(source["work_item_id"], 42)


if __name__ == "__main__":
    unittest.main()