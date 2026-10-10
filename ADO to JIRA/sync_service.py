import csv
import hashlib
import json
import os
import time

import requests
from azure.devops.connection import Connection
from msrest.authentication import BasicAuthentication

import ado_client
import jira_client
from config import (
    APP_VERSION,
    BASE_DIR,
    ensure_environment_variables,
    ensure_setup,
    get_jira_issue_key,
    get_required_environment_variable,
    update_run_state,
)
from models import RUNTIME
from network import call_with_retry
from transforms import (
    comment_timestamp_sort_key,
    extract_ado_work_item_ids,
    format_ado_field_value,
    format_ado_timestamp,
    get_comment_attachment_urls,
    get_comment_author,
    get_jira_comment_text,
    remove_html_formatting,
)


LOGGER = RUNTIME.logger
PREFLIGHT_TEXT_WARNING_CHARS = 30000


def _confirm_sync(jira_issue_key):
    response = input(f"Type Y/N to apply this sync to {jira_issue_key}: ").strip().lower()
    return response in {"y", "yes"}


def _event_marker(event_type, source_org, work_item_id, source_id):
    identity = json.dumps(
        [source_org, str(work_item_id), event_type, str(source_id)],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"[ADO_TO_JIRA_{event_type.upper()}:{digest}]"


def _comment_source_id(comment):
    source_id = comment.get("CommentId")
    if source_id:
        return str(source_id)
    identity = "\n".join(
        [
            comment.get("CommentCreatedDate", ""),
            comment.get("CommentCreatedBy", ""),
            comment.get("PlainText", ""),
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _record_decision(
    jira_issue_key,
    event_type,
    decision,
    source_org=None,
    work_item_id=None,
    source_id=None,
    target_id=None,
    details=None,
):
    RUNTIME.ledger.record_decision(
        run_id=RUNTIME.run_id,
        jira_issue_key=jira_issue_key,
        event_type=event_type,
        decision=decision,
        source_org=source_org,
        work_item_id=work_item_id,
        source_id=source_id,
        target_id=target_id,
        details=details,
    )


def _get_ledger_event(source_org, work_item_id, event_type, source_id, jira_issue_key):
    return RUNTIME.ledger.get_event(
        source_org, work_item_id, event_type, source_id, jira_issue_key
    )


def _complete_ledger_event(
    source_org,
    work_item_id,
    event_type,
    source_id,
    jira_issue_key,
    status,
    target_id=None,
    details=None,
    source_data=None,
):
    if source_data is not None:
        details = {**(details or {}), "source": source_data}
    RUNTIME.ledger.complete_event(
        source_org,
        work_item_id,
        event_type,
        source_id,
        jira_issue_key,
        status,
        target_id=target_id,
        details=details,
        run_id=RUNTIME.run_id,
    )


def _attachment_source_record(attachment):
    return {
        key: attachment.get(key)
        for key in ("url", "name", "work_item_id", "ado", "organization_url")
    }


def _work_item_event_identity(source_org, work_item_id, source_id):
    return source_org, str(work_item_id), str(source_id)


def _find_jira_comment(jira_comments, marker=None, legacy_text=None):
    for jira_comment in jira_comments:
        body_text = get_jira_comment_text(jira_comment.get("body", {}))
        if marker and marker in body_text:
            return jira_comment
        if legacy_text and legacy_text in body_text:
            return jira_comment
    return None


def _post_or_reconcile_comment(
    jira_issue_key,
    header,
    body,
    attachment_links,
    jira_api_token,
    marker,
    legacy_text=None,
):
    for attempt in range(4):
        try:
            created = jira_client.add_jira_comment(
                jira_issue_key, header, body, attachment_links, jira_api_token
            )
            return created, "posted"
        except requests.RequestException as error:
            status_code = (
                error.response.status_code if error.response is not None else None
            )
            retryable = status_code is None or status_code >= 500
            if retryable:
                time.sleep(min(0.5 * (2**attempt), 8))
            try:
                _, _, refreshed_comments = jira_client.get_jira_issue_data(
                    jira_issue_key, jira_api_token
                )
            except requests.RequestException:
                raise error
            existing = _find_jira_comment(
                refreshed_comments, marker=marker, legacy_text=legacy_text
            )
            if existing:
                return existing, "reconciled"
            if not retryable or attempt == 3:
                raise


def _record_failure(failures, message, jira_issue_key, event_type, **source):
    failures.append(message)
    RUNTIME.run_state["failure_details"] = list(failures)
    _record_decision(
        jira_issue_key,
        event_type,
        "failed",
        details={"reason": message},
        **source,
    )
    LOGGER.error("%s", message)
    if RUNTIME.config.failure_mode == "stop":
        raise RuntimeError(f"Stopped on first sync error: {message}")


def write_comments_csv(comments):
    output_path = BASE_DIR / "work_item_comments.csv"
    with output_path.open("w", newline="", encoding="utf-8-sig") as output_file:
        fieldnames = [
            "ADO",
            "WorkItemId",
            "CommentId",
            "CommentCreatedDate",
            "CommentCreatedBy",
            "CommentText",
            "PlainText",
        ]
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(comments)


def run_sync():
    LOGGER.info("Sync session started (version %s).", APP_VERSION)
    ensure_setup()
    ensure_environment_variables()
    jira_issue_key = get_jira_issue_key()
    update_run_state("running", jira_issue_key=jira_issue_key)
    jira_issue_key_upper = jira_issue_key.upper()
    jira_prefix = next(
        (
            prefix
            for prefix in RUNTIME.config.ado_configs
            if jira_issue_key_upper.startswith(prefix.upper() + "-")
        ),
        None,
    )
    if jira_prefix is None:
        valid_prefixes = ", ".join(
            sorted(prefix.upper() for prefix in RUNTIME.config.ado_configs.keys())
        )
        raise ValueError(
            f"The Jira issue key must begin with one of: {valid_prefixes}."
        )
    ensure_environment_variables()
    required_credentials = ["JIRA_API_TOKEN", f"AZDO_{jira_prefix.upper()}_PAT"]
    missing_credentials = [
        name for name in required_credentials if not os.environ.get(name)
    ]
    if missing_credentials:
        raise RuntimeError(
            "Restart the launcher after setting the required environment variable(s): "
            + ", ".join(missing_credentials)
        )
    jira_api_token = get_required_environment_variable("JIRA_API_TOKEN")
    LOGGER.info("Validated Jira issue key prefix.")

    try:
        jira_summary, jira_attachments, jira_comments = jira_client.get_jira_issue_data(
            jira_issue_key, jira_api_token
        )
    except requests.RequestException as error:
        raise RuntimeError(f"Could not read Jira issue {jira_issue_key}: {error}") from error

    work_item_inputs = extract_ado_work_item_ids(jira_summary)
    LOGGER.info(
        "Found ADO work item ID(s) in Jira summary '%s': %s",
        jira_summary,
        ", ".join(work_item_inputs),
    )
    config = RUNTIME.config.ado_configs[jira_prefix]
    update_run_state(
        "running",
        jira_issue_key=jira_issue_key,
        work_item_ids=work_item_inputs,
        source_orgs=[config["organization_url"]],
    )
    LOGGER.info("Target Jira issue: %s", jira_issue_key)
    print("\nADO work item matching:")
    print("The script searched the Jira title/summary for the first standalone number with at least four digits.")
    print(f"Jira title/summary: {jira_summary}")
    print(f"Selected ADO work item ID: {', '.join(work_item_inputs)}")
    work_items = []
    for work_item_input in work_item_inputs:
        if not work_item_input.isdigit():
            raise ValueError(f"'{work_item_input}' must be a numeric work item ID.")
        pat_env_var = f"AZDO_{jira_prefix.upper()}_PAT"
        work_items.append(
            {
                "display_id": work_item_input,
                "work_item_id": int(work_item_input),
                "prefix": jira_prefix,
                "organization_url": config["organization_url"],
                "pat": get_required_environment_variable(pat_env_var),
            }
        )
    preview_first = (
        input("Show detailed retrieval logs? (y/n): ").strip().lower()
        in {"y", "yes"}
    )
    update_run_state("running", preview_first=preview_first)

    comments = []
    attachments = []
    work_item_statuses = []
    work_item_field_updates = []
    field_skipped_count = 0
    failures = []
    for work_item in work_items:
        work_item_id = work_item["work_item_id"]
        try:
            credentials = BasicAuthentication("", work_item["pat"])
            connection = Connection(
                base_url=work_item["organization_url"],
                creds=credentials,
            )
            work_item_tracking_client = connection.clients.get_work_item_tracking_client()
            ado_work_item = call_with_retry(
                work_item_tracking_client.get_work_item, work_item_id
            )
            work_item_statuses.append(
                ado_client.get_work_item_status(ado_work_item, work_item_id)
            )
            for comment in ado_client.get_work_item_comments(
                work_item_tracking_client,
                work_item_id,
                work_item["pat"],
                work_item=ado_work_item,
            ):
                raw_text = comment.get("text", "")
                comments.append(
                    {
                        "WorkItemId": work_item_id,
                        "ADO": work_item["prefix"],
                        "CommentId": comment.get("id", ""),
                        "CommentCreatedDate": comment.get("createdDate", ""),
                        "CommentCreatedBy": get_comment_author(comment),
                        "CommentText": raw_text,
                        "PlainText": remove_html_formatting(raw_text),
                    }
                )
            ado_attachments = ado_client.get_work_item_attachments(
                work_item["organization_url"],
                work_item_id,
                work_item["pat"],
            )
            for attachment in ado_attachments:
                attachment["ado"] = work_item["prefix"]
                attachment["organization_url"] = work_item["organization_url"]
                attachment["pat"] = work_item["pat"]
            attachments.extend(ado_attachments)
            revision = getattr(ado_work_item, "rev", None)
            if revision is None:
                revision = (getattr(ado_work_item, "fields", {}) or {}).get("System.Rev")
            revisions_to_check = RUNTIME.ledger.get_field_revisions_to_check(
                work_item["organization_url"],
                work_item_id,
                jira_issue_key,
                revision,
            )
            for field_revision in revisions_to_check:
                source_id = str(field_revision)
                event = _get_ledger_event(
                    work_item["organization_url"],
                    work_item_id,
                    "fields",
                    source_id,
                    jira_issue_key,
                )
                if event and event["status"] in {
                    "posted",
                    "reconciled",
                    "existing",
                    "no_changes",
                }:
                    field_skipped_count += 1
                    _record_decision(
                        jira_issue_key,
                        "fields",
                        "skipped_ledger",
                        source_org=work_item["organization_url"],
                        work_item_id=work_item_id,
                        source_id=source_id,
                        target_id=event.get("target_id"),
                        details={"revision": field_revision, "status": event["status"]},
                    )
                    continue
                try:
                    field_update = ado_client.get_work_item_field_update(
                        work_item["organization_url"],
                        work_item_id,
                        field_revision,
                        work_item["pat"],
                    )
                except Exception as error:
                    _complete_ledger_event(
                        work_item["organization_url"],
                        work_item_id,
                        "fields",
                        source_id,
                        jira_issue_key,
                        "failed",
                        details={"error": str(error), "revision": field_revision},
                        source_data={"revision": field_revision},
                    )
                    _record_decision(
                        jira_issue_key,
                        "fields",
                        "failed",
                        source_org=work_item["organization_url"],
                        work_item_id=work_item_id,
                        source_id=source_id,
                        details={"error": str(error), "revision": field_revision},
                    )
                    raise
                if field_update:
                    work_item_field_updates.append(field_update)
                else:
                    _complete_ledger_event(
                        work_item["organization_url"],
                        work_item_id,
                        "fields",
                        source_id,
                        jira_issue_key,
                        "no_changes",
                        details={"revision": field_revision},
                        source_data={"revision": field_revision},
                    )
                    _record_decision(
                        jira_issue_key,
                        "fields",
                        "no_changes",
                        source_org=work_item["organization_url"],
                        work_item_id=work_item_id,
                        source_id=source_id,
                        details={"revision": field_revision},
                    )
        except requests.RequestException as error:
            _record_failure(
                failures,
                f"ADO work item {work_item_id}: {error}",
                jira_issue_key,
                "work_item_retrieval",
                source_org=work_item["organization_url"],
                work_item_id=work_item_id,
            )
        except Exception as error:
            LOGGER.exception("Unexpected error retrieving work item %s.", work_item_id)
            _record_failure(
                failures,
                f"ADO work item {work_item_id}: {error}",
                jira_issue_key,
                "work_item_retrieval",
                source_org=work_item["organization_url"],
                work_item_id=work_item_id,
            )

    comments.sort(key=comment_timestamp_sort_key)
    write_comments_csv(comments)
    LOGGER.info(
        "Retrieved %d comment(s) and %d attachment(s); CSV export written.",
        len(comments),
        len(attachments),
    )

    existing_comment_text = {
        get_jira_comment_text(comment.get("body", {}))
        for comment in jira_comments
    }
    unique_attachments = {attachment["url"]: attachment for attachment in attachments}
    organization_urls_by_prefix = {
        work_item["prefix"]: work_item["organization_url"]
        for work_item in work_items
    }
    comment_attachment_count = 0
    for comment in comments:
        comment_attachments = get_comment_attachment_urls(
            comment["CommentText"], organization_urls_by_prefix[comment["ADO"]]
        )
        comment_attachment_count += len(comment_attachments)
        for comment_attachment in comment_attachments:
            url = comment_attachment["url"]
            unique_attachments.setdefault(
                url,
                {
                    "url": url,
                    "name": comment_attachment["name"] or "screenshot.png",
                    "work_item_id": comment["WorkItemId"],
                    "ado": comment["ADO"],
                    "organization_url": organization_urls_by_prefix[comment["ADO"]],
                    "pat": next(
                        work_item["pat"]
                        for work_item in work_items
                        if work_item["prefix"] == comment["ADO"]
                    ),
                },
            )
    LOGGER.info(
        "Found %d attachment URL(s) embedded in ADO comments.",
        comment_attachment_count,
    )

    LOGGER.info(
        "Found %d ADO comments.",
        len(comments),
        extra={"show_console": preview_first},
    )
    LOGGER.info(
        "Found %d unique ADO attachments.",
        len(unique_attachments),
        extra={"show_console": preview_first},
    )
    LOGGER.info(
        "Found status information for %d work item(s).",
        len(work_item_statuses),
        extra={"show_console": preview_first},
    )
    LOGGER.info(
        "Found non-status field updates for %d work item(s).",
        len(work_item_field_updates),
        extra={"show_console": preview_first},
    )
    LOGGER.info(
        "Target Jira issue: %s",
        jira_issue_key,
        extra={"show_console": preview_first},
    )
    if failures:
        LOGGER.warning(
            "Encountered %d retrieval failure(s).",
            len(failures),
            extra={"show_console": preview_first},
        )
    attachment_sizes = {}
    oversized_attachments = []
    unknown_size_attachments = []
    attachments_to_process = []
    for attachment in unique_attachments.values():
        source_org = attachment["organization_url"]
        source_id = attachment["url"]
        event = _get_ledger_event(
            source_org,
            attachment["work_item_id"],
            "attachment",
            source_id,
            jira_issue_key,
        )
        if event and event["status"] in {"uploaded", "uploaded_existing", "linked"}:
            continue
        attachments_to_process.append(attachment)
        try:
            size = jira_client.get_attachment_size(attachment, attachment["pat"])
        except Exception as error:
            size = None
            LOGGER.warning(
                "Attachment size could not be checked for %s: %s",
                attachment.get("name", source_id),
                error,
            )
        attachment_sizes[source_id] = size
        if size is None:
            unknown_size_attachments.append(attachment)
        elif size > RUNTIME.config.max_attachment_bytes:
            oversized_attachments.append(attachment)

    comments_to_post = 0
    comments_already_synced = 0
    comments_preview = []
    for comment in comments:
        source_org = organization_urls_by_prefix[comment["ADO"]]
        source_id = _comment_source_id(comment)
        marker = _event_marker(
            "comment", source_org, comment["WorkItemId"], source_id
        )
        event = _get_ledger_event(
            source_org,
            comment["WorkItemId"],
            "comment",
            source_id,
            jira_issue_key,
        )
        legacy_header = (
            f"Comment Created by: {comment['CommentCreatedBy']}\n"
            f"Date Created: {format_ado_timestamp(comment['CommentCreatedDate'])}"
        )
        existing = _find_jira_comment(
            jira_comments,
            marker=marker,
            legacy_text=legacy_header + comment["PlainText"],
        )
        if (event and event["status"] in {"posted", "reconciled", "existing"}) or existing:
            comments_already_synced += 1
        else:
            comments_to_post += 1
            comments_preview.append(comment)

    work_item_by_id = {item["work_item_id"]: item for item in work_items}
    states_to_post = 0
    states_preview = []
    for item in work_item_statuses:
        work_item = work_item_by_id[item["work_item_id"]]
        source_id = f"{item['state']}:{item['changed_date']}"
        event = _get_ledger_event(
            work_item["organization_url"],
            item["work_item_id"],
            "state",
            source_id,
            jira_issue_key,
        )
        marker = _event_marker(
            "state", work_item["organization_url"], item["work_item_id"], source_id
        )
        if not (
            (event and event["status"] in {"posted", "reconciled", "existing"})
            or any(marker in text for text in existing_comment_text)
        ):
            states_to_post += 1
            states_preview.append(item)

    fields_to_post = 0
    fields_preview = []
    for item in work_item_field_updates:
        work_item = work_item_by_id[item["work_item_id"]]
        source_id = str(item["revision"])
        event = _get_ledger_event(
            work_item["organization_url"],
            item["work_item_id"],
            "fields",
            source_id,
            jira_issue_key,
        )
        marker = _event_marker(
            "fields", work_item["organization_url"], item["work_item_id"], source_id
        )
        if not (
            (event and event["status"] in {"posted", "reconciled", "existing"})
            or any(marker in text for text in existing_comment_text)
        ):
            fields_to_post += 1
            fields_preview.append(item)

    preflight = {
        "jira_issue_key": jira_issue_key,
        "source_orgs": sorted(set(organization_urls_by_prefix.values())),
        "work_item_ids": work_item_inputs,
        "comments_to_post": comments_to_post,
        "comments_already_synced": comments_already_synced,
        "states_to_post": states_to_post,
        "field_revisions_to_post": fields_to_post,
        "attachments_to_process": len(attachments_to_process),
        "oversized_attachment_urls": [item["url"] for item in oversized_attachments],
        "unknown_size_attachment_urls": [item["url"] for item in unknown_size_attachments],
    }
    _record_decision(
        jira_issue_key, "preflight", "awaiting_confirmation", details=preflight
    )
    print("\nSync preflight")
    print(f"Run ID: {RUNTIME.run_id}")
    print(f"Jira issue: {jira_issue_key}")
    print(f"Source organizations: {', '.join(preflight['source_orgs'])}")
    print(f"ADO work items: {', '.join(work_item_inputs)}")
    print(
        f"Comments: {comments_to_post} to post, "
        f"{comments_already_synced} already synced/present"
    )
    print(f"State updates to post: {states_to_post}")
    print(f"Field revisions to post: {fields_to_post}")
    print(f"Attachments to process: {len(attachments_to_process)}")
    for comment in comments_preview:
        print(
            f"\nComment from ADO {comment['ADO']} item {comment['WorkItemId']} "
            f"({comment['CommentCreatedBy']}, {comment['CommentCreatedDate']}):"
        )
        print(comment["PlainText"] or "(empty comment)")
        if len(comment["PlainText"]) > PREFLIGHT_TEXT_WARNING_CHARS:
            print(
                f"WARNING: this comment is {len(comment['PlainText'])} characters. "
                "It will not be truncated; Jira may reject the full value."
            )
    for status in states_preview:
        print(
            f"\nStatus for ADO item {status['work_item_id']}: {status['state']} "
            f"(changed by {status['changed_by']} at {status['changed_date']})"
        )
    for update in fields_preview:
        print(
            f"\nField revision {update['revision']} for ADO item "
            f"{update['work_item_id']} (changed by {update['changed_by']}):"
        )
        for field_name, change in update["fields"].items():
            old_value = format_ado_field_value(change["old_value"])
            new_value = format_ado_field_value(change["new_value"])
            print(f"- {field_name}: {old_value} -> {new_value}")
            if max(len(old_value), len(new_value)) > PREFLIGHT_TEXT_WARNING_CHARS:
                print(
                    f"WARNING: field {field_name} contains a value longer than "
                    f"{PREFLIGHT_TEXT_WARNING_CHARS} characters. It will not be "
                    "truncated; Jira may reject the full value."
                )
    if oversized_attachments:
        limit_mb = RUNTIME.config.max_attachment_bytes // (1024 * 1024)
        print(
            f"WARNING: {len(oversized_attachments)} attachment(s) exceed {limit_mb} MB "
            "and will be linked instead of uploaded."
        )
    if unknown_size_attachments:
        print(
            f"WARNING: Could not verify the size of {len(unknown_size_attachments)} "
            "attachment(s); the upload limit will be enforced during transfer."
        )
    uploaded_attachment_count = 0
    for attachment in attachments_to_process:
        size = attachment_sizes.get(attachment["url"])
        size_text = "unknown" if size is None else f"{size / (1024 * 1024):.1f} MB"
        outcome = "will be linked" if attachment in oversized_attachments else "will be uploaded"
        print(
            f"- {attachment.get('name', 'attachment')}: {size_text}; {outcome}; "
            f"source {attachment['url']}"
        )

    if not _confirm_sync(jira_issue_key):
        update_run_state("cancelled", reason="User declined Jira posting.")
        _record_decision(jira_issue_key, "preflight", "cancelled", details=preflight)
        LOGGER.info("Sync cancelled before Jira posting.")
        return

    uploaded_by_url = {}
    known_attachment_ids = {
        attachment.get("id")
        for attachment in jira_attachments
        if attachment.get("id")
    }
    fallback_links = {}
    for attachment in unique_attachments.values():
        source_org = attachment["organization_url"]
        event = _get_ledger_event(
            source_org,
            attachment["work_item_id"],
            "attachment",
            attachment["url"],
            jira_issue_key,
        )
        if event and event["status"] in {"uploaded", "uploaded_existing"}:
            details = event["details"]
            if details.get("filename") and details.get("content_url"):
                uploaded_by_url[attachment["url"]] = {
                    "filename": details["filename"],
                    "url": details["content_url"],
                }
                _complete_ledger_event(
                    source_org,
                    attachment["work_item_id"],
                    "attachment",
                    attachment["url"],
                    jira_issue_key,
                    event["status"],
                    target_id=event.get("target_id"),
                    details=details,
                    source_data=_attachment_source_record(attachment),
                )
                _record_decision(
                    jira_issue_key,
                    "attachment",
                    event["status"],
                    source_org=source_org,
                    work_item_id=attachment["work_item_id"],
                    source_id=attachment["url"],
                    target_id=event.get("target_id"),
                    details={
                        "filename": details["filename"],
                        "content_url": details["content_url"],
                        "source_url": attachment["url"],
                    },
                )
        elif event and event["status"] == "linked":
            details = event["details"]
            fallback_links[attachment["url"]] = {
                "filename": details.get("filename", attachment.get("name", "attachment")),
                "url": attachment["url"],
            }
            _complete_ledger_event(
                source_org,
                attachment["work_item_id"],
                "attachment",
                attachment["url"],
                jira_issue_key,
                "linked",
                target_id=event.get("target_id"),
                details=details,
                source_data=_attachment_source_record(attachment),
            )
            _record_decision(
                jira_issue_key,
                "attachment",
                "linked",
                source_org=source_org,
                work_item_id=attachment["work_item_id"],
                source_id=attachment["url"],
                target_id=event.get("target_id"),
                details={"filename": fallback_links[attachment["url"]]["filename"]},
            )

    reconciled_attachment_count = 0
    for attachment in attachments_to_process:
        try:
            uploaded_attachment, message, fallback_link = jira_client.upload_jira_attachment(
                jira_issue_key,
                attachment,
                attachment["pat"],
                jira_api_token,
                known_attachment_ids,
            )
            LOGGER.info(message)
            if fallback_link:
                fallback_links[fallback_link["url"]] = fallback_link
                _complete_ledger_event(
                    attachment["organization_url"],
                    attachment["work_item_id"],
                    "attachment",
                    attachment["url"],
                    jira_issue_key,
                    "fallback_pending",
                    details=fallback_link,
                    source_data=_attachment_source_record(attachment),
                )
                _record_decision(
                    jira_issue_key,
                    "attachment",
                    "fallback_pending",
                    source_org=attachment["organization_url"],
                    work_item_id=attachment["work_item_id"],
                    source_id=attachment["url"],
                    details=fallback_link,
                )
            if uploaded_attachment:
                was_reconciled = uploaded_attachment.get("_reconciled", False)
                if was_reconciled:
                    reconciled_attachment_count += 1
                else:
                    uploaded_attachment_count += 1
                uploaded_by_url[attachment["url"]] = {
                    "filename": uploaded_attachment["filename"],
                    "url": uploaded_attachment["content"],
                }
                _complete_ledger_event(
                    attachment["organization_url"],
                    attachment["work_item_id"],
                    "attachment",
                    attachment["url"],
                    jira_issue_key,
                    "uploaded_existing" if was_reconciled else "uploaded",
                    target_id=uploaded_attachment.get("id"),
                    details={
                        "filename": uploaded_attachment["filename"],
                        "content_url": uploaded_attachment["content"],
                    },
                    source_data=_attachment_source_record(attachment),
                )
                _record_decision(
                    jira_issue_key,
                    "attachment",
                    "uploaded_existing" if was_reconciled else "uploaded",
                    source_org=attachment["organization_url"],
                    work_item_id=attachment["work_item_id"],
                    source_id=attachment["url"],
                    target_id=uploaded_attachment.get("id"),
                    details={
                        "filename": uploaded_attachment["filename"],
                        "content_url": uploaded_attachment["content"],
                        "source_url": attachment["url"],
                    },
                )
        except Exception as error:
            LOGGER.error("Attachment upload failed for %s: %s", attachment["name"], error)
            _complete_ledger_event(
                attachment["organization_url"],
                attachment["work_item_id"],
                "attachment",
                attachment["url"],
                jira_issue_key,
                "failed",
                details={"error": str(error)},
                source_data=_attachment_source_record(attachment),
            )
            _record_failure(
                failures,
                f"Attachment {attachment['name']}: {error}",
                jira_issue_key,
                "attachment",
                source_org=attachment["organization_url"],
                work_item_id=attachment["work_item_id"],
                source_id=attachment["url"],
            )

    posted_count = 0
    skipped_count = 0
    for comment in comments:
        source_org = organization_urls_by_prefix[comment["ADO"]]
        source_id = _comment_source_id(comment)
        marker = _event_marker(
            "comment", source_org, comment["WorkItemId"], source_id
        )
        legacy_header = (
            f"Comment Created by: {comment['CommentCreatedBy']}\n"
            f"Date Created: {format_ado_timestamp(comment['CommentCreatedDate'])}"
        )
        comment_header = f"{marker} {legacy_header}"
        event = _get_ledger_event(
            source_org,
            comment["WorkItemId"],
            "comment",
            source_id,
            jira_issue_key,
        )
        comment_attachments = get_comment_attachment_urls(
            comment["CommentText"], organization_urls_by_prefix[comment["ADO"]]
        )
        attachment_links = [
            uploaded_by_url[item["url"]]
            for item in comment_attachments
            if item["url"] in uploaded_by_url
        ]
        attachment_links.extend(
            fallback_links[item["url"]]
            for item in comment_attachments
            if item["url"] in fallback_links
        )
        legacy_text = legacy_header + comment["PlainText"]
        existing_jira_comment = _find_jira_comment(
            jira_comments, marker=marker, legacy_text=legacy_text
        )
        ledger_completed = bool(
            event and event["status"] in {"posted", "reconciled", "existing"}
        )
        if ledger_completed and existing_jira_comment is None:
            skipped_count += 1
            _record_decision(
                jira_issue_key,
                "comment",
                "skipped_ledger",
                source_org=source_org,
                work_item_id=comment["WorkItemId"],
                source_id=source_id,
                target_id=event.get("target_id"),
                details={"comment_id": comment["CommentId"]},
            )
            continue
        if existing_jira_comment:
            skipped_count += 1
            _complete_ledger_event(
                source_org,
                comment["WorkItemId"],
                "comment",
                source_id,
                jira_issue_key,
                "existing",
                target_id=existing_jira_comment.get("id"),
                details={"comment_id": comment["CommentId"]},
                source_data=comment,
            )
            _record_decision(
                jira_issue_key,
                "comment",
                "skipped_existing",
                source_org=source_org,
                work_item_id=comment["WorkItemId"],
                source_id=source_id,
                target_id=existing_jira_comment.get("id"),
                details={"comment_id": comment["CommentId"]},
            )
            if attachment_links:
                try:
                    if jira_client.add_jira_comment_attachments(
                        jira_issue_key,
                        existing_jira_comment,
                        attachment_links,
                        jira_api_token,
                    ):
                        LOGGER.info(
                            "Added missing attachment link(s) to existing Jira comment %s.",
                            existing_jira_comment["id"],
                        )
                except Exception as error:
                    LOGGER.error(
                        "Could not update attachments on Jira comment %s: %s",
                        existing_jira_comment.get("id", "unknown ID"),
                        error,
                    )
                    _record_failure(
                        failures,
                        f"Comment attachments for {comment['CommentCreatedBy']}: {error}",
                        jira_issue_key,
                        "comment_attachments",
                        source_org=source_org,
                        work_item_id=comment["WorkItemId"],
                        source_id=source_id,
                    )
            continue

        try:
            target_comment, outcome = _post_or_reconcile_comment(
                jira_issue_key,
                comment_header,
                comment["PlainText"],
                attachment_links,
                jira_api_token,
                marker,
                legacy_text,
            )
            target_id = target_comment.get("id") if target_comment else None
            _complete_ledger_event(
                source_org,
                comment["WorkItemId"],
                "comment",
                source_id,
                jira_issue_key,
                "reconciled" if outcome == "reconciled" else "posted",
                target_id=target_id,
                details={"comment_id": comment["CommentId"]},
                source_data=comment,
            )
            _record_decision(
                jira_issue_key,
                "comment",
                outcome,
                source_org=source_org,
                work_item_id=comment["WorkItemId"],
                source_id=source_id,
                target_id=target_id,
                details={"comment_id": comment["CommentId"]},
            )
            posted_count += 1
            LOGGER.info("%s one Jira comment.", outcome.capitalize())
        except Exception as error:
            _complete_ledger_event(
                source_org,
                comment["WorkItemId"],
                "comment",
                source_id,
                jira_issue_key,
                "failed",
                details={"error": str(error)},
                source_data=comment,
            )
            _record_failure(
                failures,
                f"Jira comment post failed for {comment['CommentCreatedBy']}: {error}",
                jira_issue_key,
                "comment",
                source_org=source_org,
                work_item_id=comment["WorkItemId"],
                source_id=source_id,
            )

    status_posted_count = 0
    status_skipped_count = 0
    seen_status_markers = set()
    for work_item_status in work_item_statuses:
        source_org = work_item_by_id[work_item_status["work_item_id"]]["organization_url"]
        changed_date = work_item_status["changed_date"]
        source_id = f"{work_item_status['state']}:{changed_date}"
        legacy_marker = (
            f"[ADO_TO_JIRA_STATE:{work_item_status['work_item_id']}:"
            f"{work_item_status['state']}:{changed_date}]"
        )
        marker = _event_marker(
            "state", source_org, work_item_status["work_item_id"], source_id
        )
        status_identity = _work_item_event_identity(
            source_org, work_item_status["work_item_id"], source_id
        )
        event = _get_ledger_event(
            source_org,
            work_item_status["work_item_id"],
            "state",
            source_id,
            jira_issue_key,
        )
        existing_status = _find_jira_comment(
            jira_comments, marker=marker, legacy_text=legacy_marker
        )
        if (
            status_identity in seen_status_markers
            or (event and event["status"] in {"posted", "reconciled", "existing"})
            or existing_status
        ):
            status_skipped_count += 1
            _complete_ledger_event(
                source_org,
                work_item_status["work_item_id"],
                "state",
                source_id,
                jira_issue_key,
                "existing",
                target_id=existing_status.get("id") if existing_status else (
                    event.get("target_id") if event else None
                ),
                details={"state": work_item_status["state"], "changed_date": changed_date},
                source_data=work_item_status,
            )
            _record_decision(
                jira_issue_key,
                "state",
                "skipped_existing",
                source_org=source_org,
                work_item_id=work_item_status["work_item_id"],
                source_id=source_id,
                target_id=existing_status.get("id") if existing_status else None,
                details={"state": work_item_status["state"], "changed_date": changed_date},
            )
            continue
        status_header = (
            f"{marker} {legacy_marker} ADO work item "
            f"{work_item_status['work_item_id']} status"
        )
        status_body = (
            f"State: {work_item_status['state']}\n"
            f"Last changed by: {work_item_status['changed_by']}\n"
            f"Last changed: {format_ado_timestamp(changed_date)}"
        )
        try:
            target_comment, outcome = _post_or_reconcile_comment(
                jira_issue_key,
                status_header,
                status_body,
                [],
                jira_api_token,
                marker,
                legacy_marker,
            )
            status_posted_count += 1
            seen_status_markers.add(status_identity)
            target_id = target_comment.get("id") if target_comment else None
            _complete_ledger_event(
                source_org,
                work_item_status["work_item_id"],
                "state",
                source_id,
                jira_issue_key,
                "reconciled" if outcome == "reconciled" else "posted",
                target_id=target_id,
                details={"state": work_item_status["state"], "changed_date": changed_date},
                source_data=work_item_status,
            )
            _record_decision(
                jira_issue_key,
                "state",
                outcome,
                source_org=source_org,
                work_item_id=work_item_status["work_item_id"],
                source_id=source_id,
                target_id=target_id,
                details={"state": work_item_status["state"], "changed_date": changed_date},
            )
            existing_comment_text.add(marker)
            existing_comment_text.add(legacy_marker)
            LOGGER.info(
                "%s status for ADO work item %s.",
                outcome.capitalize(),
                work_item_status["work_item_id"],
            )
        except Exception as error:
            _complete_ledger_event(
                source_org,
                work_item_status["work_item_id"],
                "state",
                source_id,
                jira_issue_key,
                "failed",
                details={"error": str(error)},
                source_data=work_item_status,
            )
            _record_failure(
                failures,
                f"Status for work item {work_item_status['work_item_id']}: {error}",
                jira_issue_key,
                "state",
                source_org=source_org,
                work_item_id=work_item_status["work_item_id"],
                source_id=source_id,
            )

    field_update_posted_count = 0
    seen_field_update_markers = set()
    for field_update in work_item_field_updates:
        source_org = work_item_by_id[field_update["work_item_id"]]["organization_url"]
        source_id = str(field_update["revision"])
        legacy_marker = (
            f"[ADO_TO_JIRA_FIELDS:{field_update['work_item_id']}:"
            f"{field_update['revision']}]"
        )
        marker = _event_marker(
            "fields", source_org, field_update["work_item_id"], source_id
        )
        field_identity = _work_item_event_identity(
            source_org, field_update["work_item_id"], source_id
        )
        event = _get_ledger_event(
            source_org,
            field_update["work_item_id"],
            "fields",
            source_id,
            jira_issue_key,
        )
        existing_update = _find_jira_comment(
            jira_comments, marker=marker, legacy_text=legacy_marker
        )
        if (
            field_identity in seen_field_update_markers
            or (event and event["status"] in {"posted", "reconciled", "existing"})
            or existing_update
        ):
            field_skipped_count += 1
            _complete_ledger_event(
                source_org,
                field_update["work_item_id"],
                "fields",
                source_id,
                jira_issue_key,
                "existing",
                target_id=existing_update.get("id") if existing_update else (
                    event.get("target_id") if event else None
                ),
                details={"revision": field_update["revision"]},
                source_data=field_update,
            )
            _record_decision(
                jira_issue_key,
                "fields",
                "skipped_existing",
                source_org=source_org,
                work_item_id=field_update["work_item_id"],
                source_id=source_id,
                target_id=existing_update.get("id") if existing_update else None,
                details={"revision": field_update["revision"]},
            )
            continue

        field_header = (
            f"{marker} {legacy_marker} ADO work item "
            f"{field_update['work_item_id']} fields updated"
        )
        field_lines = [
            f"{field_name}: "
            f"{format_ado_field_value(change['old_value'])} -> "
            f"{format_ado_field_value(change['new_value'])}"
            for field_name, change in field_update["fields"].items()
        ]
        field_body = (
            f"Revision: {field_update['revision']}\n"
            f"Changed by: {field_update['changed_by']}\n"
            f"Changed: {format_ado_timestamp(field_update['changed_date'])}\n\n"
            "Updated fields:\n"
            + "\n".join(field_lines)
        )
        try:
            target_comment, outcome = _post_or_reconcile_comment(
                jira_issue_key,
                field_header,
                field_body,
                [],
                jira_api_token,
                marker,
                legacy_marker,
            )
            field_update_posted_count += 1
            seen_field_update_markers.add(field_identity)
            target_id = target_comment.get("id") if target_comment else None
            _complete_ledger_event(
                source_org,
                field_update["work_item_id"],
                "fields",
                source_id,
                jira_issue_key,
                "reconciled" if outcome == "reconciled" else "posted",
                target_id=target_id,
                details={"revision": field_update["revision"]},
                source_data=field_update,
            )
            _record_decision(
                jira_issue_key,
                "fields",
                outcome,
                source_org=source_org,
                work_item_id=field_update["work_item_id"],
                source_id=source_id,
                target_id=target_id,
                details={"revision": field_update["revision"]},
            )
            existing_comment_text.add(marker)
            existing_comment_text.add(legacy_marker)
            LOGGER.info(
                "%s field updates for ADO work item %s revision %s.",
                outcome.capitalize(),
                field_update["work_item_id"],
                field_update["revision"],
            )
        except Exception as error:
            _complete_ledger_event(
                source_org,
                field_update["work_item_id"],
                "fields",
                source_id,
                jira_issue_key,
                "failed",
                details={"error": str(error)},
                source_data=field_update,
            )
            _record_failure(
                failures,
                f"Field updates for work item {field_update['work_item_id']}: {error}",
                jira_issue_key,
                "fields",
                source_org=source_org,
                work_item_id=field_update["work_item_id"],
                source_id=source_id,
            )

    linked_attachment_count = 0
    if fallback_links:
        link_header = "[ADO_TO_JIRA_LINKS] Attachments not uploaded"
        link_body = (
            "These Azure DevOps attachments exceeded the configured upload limit "
            "and were stored as links instead."
        )
        fallback_source_id = "|".join(sorted(fallback_links))
        fallback_marker = _event_marker(
            "links", jira_issue_key, 0, fallback_source_id
        )
        try:
            target_comment, outcome = _post_or_reconcile_comment(
                jira_issue_key,
                f"{fallback_marker} {link_header}",
                link_body,
                list(fallback_links.values()),
                jira_api_token,
                fallback_marker,
                link_header + link_body,
            )
            target_id = target_comment.get("id") if target_comment else None
            existing_comment_text.add(fallback_marker)
            for source_url, fallback_link in fallback_links.items():
                source_attachment = unique_attachments[source_url]
                _complete_ledger_event(
                    source_attachment["organization_url"],
                    source_attachment["work_item_id"],
                    "attachment",
                    source_url,
                    jira_issue_key,
                    "linked",
                    target_id=target_id,
                    details={
                        "filename": fallback_link["filename"],
                        "source_url": source_url,
                        "target_comment_id": target_id,
                    },
                    source_data=_attachment_source_record(source_attachment),
                )
                _record_decision(
                    jira_issue_key,
                    "attachment",
                    "linked",
                    source_org=source_attachment["organization_url"],
                    work_item_id=source_attachment["work_item_id"],
                    source_id=source_url,
                    target_id=target_id,
                    details={"filename": fallback_link["filename"]},
                )
            LOGGER.info(
                "%s %d oversized attachment link(s).",
                outcome.capitalize(),
                len(fallback_links),
            )
            linked_attachment_count = len(fallback_links)
        except Exception as error:
            _record_failure(
                failures,
                f"Could not post oversized attachment links: {error}",
                jira_issue_key,
                "attachment_links",
                source_id=fallback_source_id,
            )

    LOGGER.info(
        "Sync run completed: posted %d comment(s), skipped %d duplicate(s), failures %d.",
        posted_count,
        skipped_count,
        len(failures),
    )
    print("\nSync summary")
    print(f"Run ID: {RUNTIME.run_id}")
    print(f"Jira issue: {jira_issue_key}")
    print(f"Comments posted or reconciled: {posted_count}")
    print(f"Comments skipped as duplicates: {skipped_count}")
    print(f"State updates posted: {status_posted_count}")
    print(f"State updates skipped: {status_skipped_count}")
    print(f"Field revisions posted: {field_update_posted_count}")
    print(f"Field revisions skipped: {field_skipped_count}")
    print(f"Attachments uploaded: {uploaded_attachment_count}")
    print(f"Attachments reconciled after retry: {reconciled_attachment_count}")
    print(f"Attachments linked as fallbacks: {linked_attachment_count}")
    print(f"Failures: {len(failures)}")
    for failure in failures:
        print(f"- {failure}")
    run_summary = {
        "posted_comments": posted_count,
        "posted_status_comments": status_posted_count,
        "skipped_status_updates": status_skipped_count,
        "posted_field_update_comments": field_update_posted_count,
        "skipped_field_revisions": field_skipped_count,
        "uploaded_attachments": uploaded_attachment_count,
        "reconciled_attachments": reconciled_attachment_count,
        "linked_attachments": linked_attachment_count,
        "skipped_duplicates": skipped_count,
        "failures": failures,
        "failure_details": failures,
        "failure_count": len(failures),
    }
    _record_decision(
        jira_issue_key,
        "run",
        "completed_with_errors" if failures else "completed",
        details=run_summary,
    )
    update_run_state(
        "completed_with_errors" if failures else "completed",
        **run_summary,
    )
    if failures:
        for failure in failures:
            LOGGER.error("Failure: %s", failure)