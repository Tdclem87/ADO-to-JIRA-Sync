import csv
import os

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
from transforms import (
    comment_timestamp_sort_key,
    extract_ado_work_item_ids,
    format_ado_field_value,
    format_ado_timestamp,
    get_attachment_filename,
    get_comment_attachment_urls,
    get_comment_author,
    get_jira_comment_text,
    remove_html_formatting,
)


LOGGER = RUNTIME.logger


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
    update_run_state(
        "running",
        jira_issue_key=jira_issue_key,
        work_item_ids=work_item_inputs,
    )
    config = RUNTIME.config.ado_configs[jira_prefix]
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
    preview_first = input("Preview first? (y/n): ").strip().lower() in {"y", "yes"}
    update_run_state("running", preview_first=preview_first)

    comments = []
    attachments = []
    work_item_statuses = []
    work_item_field_updates = []
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
            ado_work_item = work_item_tracking_client.get_work_item(work_item_id)
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
                attachment["pat"] = work_item["pat"]
            attachments.extend(ado_attachments)
            revision = getattr(ado_work_item, "rev", None)
            if revision is None:
                revision = (getattr(ado_work_item, "fields", {}) or {}).get("System.Rev")
            field_update = ado_client.get_work_item_field_update(
                work_item["organization_url"],
                work_item_id,
                revision,
                work_item["pat"],
            )
            if field_update:
                work_item_field_updates.append(field_update)
        except requests.RequestException as error:
            LOGGER.error(
                "Azure DevOps request failed for work item %s: %s", work_item_id, error
            )
            failures.append(f"ADO work item {work_item_id}: {error}")
        except Exception as error:
            LOGGER.exception("Unexpected error retrieving work item %s.", work_item_id)
            failures.append(f"ADO work item {work_item_id}: {error}")

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
    existing_filenames = {
        attachment.get("filename")
        for attachment in jira_attachments
        if attachment.get("filename")
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
    if preview_first:
        LOGGER.info("Preview displayed before Jira posting.")

    if input("Continue posting to Jira? (y/n): ").strip().lower() not in {"y", "yes"}:
        update_run_state("cancelled", reason="User declined Jira posting.")
        LOGGER.info("Sync cancelled before Jira posting.")
        return

    uploaded_by_url = {}
    uploaded_by_filename = {
        attachment["filename"]: attachment
        for attachment in jira_attachments
        if attachment.get("filename")
    }
    fallback_links = {}
    for attachment in unique_attachments.values():
        try:
            uploaded_attachment, message, fallback_link = jira_client.upload_jira_attachment(
                jira_issue_key,
                attachment,
                attachment["pat"],
                jira_api_token,
                existing_filenames,
            )
            LOGGER.info(message)
            if fallback_link:
                fallback_links[fallback_link["url"]] = fallback_link
            if uploaded_attachment:
                uploaded_by_filename[uploaded_attachment["filename"]] = uploaded_attachment
                uploaded_by_url[attachment["url"]] = {
                    "filename": uploaded_attachment["filename"],
                    "url": uploaded_attachment["content"],
                }
            else:
                existing_attachment = uploaded_by_filename.get(
                    get_attachment_filename(attachment)
                )
                if existing_attachment:
                    uploaded_by_url[attachment["url"]] = {
                        "filename": existing_attachment["filename"],
                        "url": existing_attachment["content"],
                    }
        except (requests.RequestException, KeyError) as error:
            LOGGER.error("Attachment upload failed for %s: %s", attachment["name"], error)
            failures.append(f"Attachment {attachment['name']}: {error}")

    posted_count = 0
    skipped_count = 0
    for comment in comments:
        comment_header = (
            f"Comment Created by: {comment['CommentCreatedBy']}\n"
            f"Date Created: {format_ado_timestamp(comment['CommentCreatedDate'])}"
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
        existing_jira_comment = next(
            (
                jira_comment
                for jira_comment in jira_comments
                if comment_header + comment["PlainText"]
                in get_jira_comment_text(jira_comment.get("body", {}))
            ),
            None,
        )
        if existing_jira_comment:
            skipped_count += 1
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
                except requests.RequestException as error:
                    LOGGER.error(
                        "Could not update attachments on Jira comment %s: %s",
                        existing_jira_comment.get("id", "unknown ID"),
                        error,
                    )
                    failures.append(
                        f"Comment attachments for {comment['CommentCreatedBy']}: {error}"
                    )
            continue

        try:
            jira_client.add_jira_comment(
                jira_issue_key,
                comment_header,
                comment["PlainText"],
                attachment_links,
                jira_api_token,
            )
            posted_count += 1
            LOGGER.info("Posted one Jira comment.")
        except requests.RequestException as error:
            LOGGER.error(
                "Jira comment post failed for %s: %s", comment["CommentCreatedBy"], error
            )
            failures.append(f"Comment from {comment['CommentCreatedBy']}: {error}")

    status_posted_count = 0
    seen_status_markers = set()
    for work_item_status in work_item_statuses:
        changed_date = work_item_status["changed_date"]
        status_marker = (
            f"[ADO_TO_JIRA_STATE:{work_item_status['work_item_id']}:"
            f"{work_item_status['state']}:{changed_date}]"
        )
        if status_marker in seen_status_markers or any(
            status_marker in text for text in existing_comment_text
        ):
            continue
        status_header = (
            f"{status_marker} ADO work item {work_item_status['work_item_id']} status"
        )
        status_body = (
            f"State: {work_item_status['state']}\n"
            f"Last changed by: {work_item_status['changed_by']}\n"
            f"Last changed: {format_ado_timestamp(changed_date)}"
        )
        try:
            jira_client.add_jira_comment(
                jira_issue_key, status_header, status_body, [], jira_api_token
            )
            status_posted_count += 1
            seen_status_markers.add(status_marker)
            existing_comment_text.add(status_marker)
            LOGGER.info(
                "Posted status for ADO work item %s.",
                work_item_status["work_item_id"],
            )
        except requests.RequestException as error:
            failures.append(
                f"Status for work item {work_item_status['work_item_id']}: {error}"
            )
            LOGGER.error(
                "Jira status post failed for work item %s: %s",
                work_item_status["work_item_id"],
                error,
            )

    field_update_posted_count = 0
    seen_field_update_markers = set()
    for field_update in work_item_field_updates:
        field_marker = (
            f"[ADO_TO_JIRA_FIELDS:{field_update['work_item_id']}:"
            f"{field_update['revision']}]"
        )
        if field_marker in seen_field_update_markers or any(
            field_marker in text for text in existing_comment_text
        ):
            continue

        field_header = (
            f"{field_marker} ADO work item {field_update['work_item_id']} fields updated"
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
            jira_client.add_jira_comment(
                jira_issue_key, field_header, field_body, [], jira_api_token
            )
            field_update_posted_count += 1
            seen_field_update_markers.add(field_marker)
            existing_comment_text.add(field_marker)
            LOGGER.info(
                "Posted field updates for ADO work item %s revision %s.",
                field_update["work_item_id"],
                field_update["revision"],
            )
        except requests.RequestException as error:
            failures.append(
                f"Field updates for work item {field_update['work_item_id']}: {error}"
            )
            LOGGER.error(
                "Jira field update comment failed for work item %s revision %s: %s",
                field_update["work_item_id"],
                field_update["revision"],
                error,
            )

    if fallback_links:
        link_header = "[ADO_TO_JIRA_LINKS] Attachments not uploaded"
        link_body = (
            "These Azure DevOps attachments exceeded the configured upload limit "
            "and were stored as links instead."
        )
        if link_header + link_body not in existing_comment_text:
            jira_client.add_jira_comment(
                jira_issue_key,
                link_header,
                link_body,
                list(fallback_links.values()),
                jira_api_token,
            )
            LOGGER.info("Posted %d oversized attachment link(s).", len(fallback_links))

    LOGGER.info(
        "Sync run completed: posted %d comment(s), skipped %d duplicate(s), failures %d.",
        posted_count,
        skipped_count,
        len(failures),
    )
    update_run_state(
        "completed",
        posted_comments=posted_count,
        posted_status_comments=status_posted_count,
        posted_field_update_comments=field_update_posted_count,
        skipped_duplicates=skipped_count,
        failures=len(failures),
    )
    if failures:
        for failure in failures:
            LOGGER.error("Failure: %s", failure)