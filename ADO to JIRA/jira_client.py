import json
import tempfile
import time
from pathlib import Path

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

from models import RUNTIME
from transforms import get_attachment_filename, get_jira_comment_text, jira_text_nodes


def _jira_comment_error_details(response):
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        details = {
            key: payload[key]
            for key in ("errorMessages", "errors")
            if key in payload
        }
        if details:
            return json.dumps(details, ensure_ascii=False, sort_keys=True)[:1500]

    response_text = getattr(response, "text", "")
    if isinstance(response_text, str):
        return " ".join(response_text.split())[:1500]
    return ""


def _raise_for_jira_comment_response(response):
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        details = _jira_comment_error_details(response)
        if not details:
            raise
        raise requests.HTTPError(
            f"{error}; Jira response details: {details}",
            response=response,
        ) from error


def _post_with_rate_limit_retry(url, data_factory=None, **kwargs):
    for attempt in range(4):
        if data_factory:
            data = data_factory()
            kwargs["data"] = data
            kwargs["headers"] = {
                **kwargs.get("headers", {}),
                "Content-Type": data.content_type,
            }
        response = RUNTIME.session.post(url, **kwargs)
        if response.status_code != 429 or attempt == 3:
            return response
        retry_after = response.headers.get("Retry-After")
        response.close()
        try:
            delay = (
                min(max(float(retry_after), 0), 30)
                if retry_after
                else 0.5 * (2**attempt)
            )
        except (TypeError, ValueError):
            delay = 0.5 * (2**attempt)
        time.sleep(delay)


def get_attachment_size(attachment, pat):
    response = RUNTIME.session.head(
        attachment["url"],
        auth=("", pat),
        allow_redirects=True,
        timeout=30,
    )
    if response.status_code in {405, 501}:
        response.close()
        response = RUNTIME.session.get(
            attachment["url"],
            auth=("", pat),
            stream=True,
            timeout=30,
        )
    try:
        response.raise_for_status()
        try:
            return int(response.headers.get("Content-Length", ""))
        except ValueError:
            return None
    finally:
        response.close()


def get_jira_issue_data(issue_key, jira_api_token, include_comments=True):
    config = RUNTIME.config
    response = RUNTIME.session.get(
        f"{config.jira_base_url}/rest/api/3/issue/{issue_key}",
        params={"fields": "summary,attachment"},
        auth=(config.jira_email, jira_api_token),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    fields = response.json().get("fields", {})
    summary = fields.get("summary", "")
    attachments = fields.get("attachment", [])
    if not include_comments:
        return summary, attachments, []

    comments = []
    start_at = 0
    max_results = 100
    while True:
        comment_response = RUNTIME.session.get(
            f"{config.jira_base_url}/rest/api/3/issue/{issue_key}/comment",
            params={"startAt": start_at, "maxResults": max_results},
            auth=(config.jira_email, jira_api_token),
            headers={"Accept": "application/json"},
            timeout=30,
        )
        comment_response.raise_for_status()
        comment_page = comment_response.json()
        page_comments = comment_page.get("comments", [])
        comments.extend(page_comments)
        start_at += len(page_comments)
        total = comment_page.get("total")
        if (
            not page_comments
            or comment_page.get("isLast") is True
            or (total is not None and start_at >= int(total))
            or (total is None and len(page_comments) < max_results)
        ):
            break
    return (
        summary,
        attachments,
        comments,
    )


def _find_new_jira_attachment(
    issue_key, filename, jira_api_token, known_attachment_ids
):
    _, attachments, _ = get_jira_issue_data(
        issue_key, jira_api_token, include_comments=False
    )
    existing = next(
        (
            attachment
            for attachment in attachments
            if attachment.get("filename") == filename
            and attachment.get("id") not in known_attachment_ids
        ),
        None,
    )
    return {**existing, "_reconciled": True} if existing else None


def upload_jira_attachment(
    issue_key, attachment, pat, jira_api_token, known_attachment_ids
):
    config = RUNTIME.config
    filename = get_attachment_filename(attachment)

    temporary_path = None
    try:
        with RUNTIME.session.get(
            attachment["url"],
            auth=("", pat),
            stream=True,
            timeout=30,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get(
                "Content-Type", "application/octet-stream"
            )
            filename = get_attachment_filename(
                attachment,
                content_disposition=response.headers.get("Content-Disposition"),
                content_type=content_type,
            )
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > config.max_attachment_bytes:
                message = (
                    f"Stored link for oversized attachment: {filename} "
                    f"(limit {config.max_attachment_bytes // (1024 * 1024)} MB)"
                )
                return None, message, {"filename": filename, "url": attachment["url"]}

            with tempfile.NamedTemporaryFile(delete=False) as temporary_file:
                temporary_path = temporary_file.name
                total_bytes = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > config.max_attachment_bytes:
                        message = (
                            f"Stored link for oversized attachment: {filename} "
                            f"(limit {config.max_attachment_bytes // (1024 * 1024)} MB)"
                        )
                        return None, message, {
                            "filename": filename,
                            "url": attachment["url"],
                        }
                    temporary_file.write(chunk)
        with open(temporary_path, "rb") as attachment_file:
            for attempt in range(4):
                def create_encoder():
                    attachment_file.seek(0)
                    return MultipartEncoder(
                        fields={"file": (filename, attachment_file, content_type)}
                    )

                try:
                    upload_response = _post_with_rate_limit_retry(
                        f"{config.jira_base_url}/rest/api/3/issue/{issue_key}/attachments",
                        data_factory=create_encoder,
                        auth=(config.jira_email, jira_api_token),
                        headers={"X-Atlassian-Token": "no-check"},
                        timeout=30,
                    )
                    if 500 <= upload_response.status_code < 600:
                        upload_response.close()
                        time.sleep(min(0.5 * (2**attempt), 8))
                        existing = _find_new_jira_attachment(
                            issue_key,
                            filename,
                            jira_api_token,
                            known_attachment_ids,
                        )
                        if existing:
                            known_attachment_ids.add(existing.get("id"))
                            return (
                                existing,
                                f"Reconciled existing Jira attachment: {filename}",
                                None,
                            )
                        if attempt == 3:
                            upload_response.raise_for_status()
                        continue
                    upload_response.raise_for_status()
                    uploaded_attachment = upload_response.json()[0]
                    break
                except requests.RequestException as error:
                    response = error.response
                    if response is not None and response.status_code < 500:
                        raise
                    time.sleep(min(0.5 * (2**attempt), 8))
                    try:
                        existing = _find_new_jira_attachment(
                            issue_key,
                            filename,
                            jira_api_token,
                            known_attachment_ids,
                        )
                    except requests.RequestException:
                        raise error
                    if existing:
                        known_attachment_ids.add(existing.get("id"))
                        return (
                            existing,
                            f"Reconciled existing Jira attachment: {filename}",
                            None,
                        )
                    if attempt == 3:
                        raise
            else:
                raise RuntimeError(f"Attachment upload did not complete: {filename}")
        known_attachment_ids.add(uploaded_attachment.get("id"))
        return uploaded_attachment, f"Uploaded attachment: {filename}", None
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)


def add_jira_comment(
    issue_key,
    comment_header,
    comment_body,
    attachment_links,
    jira_api_token,
    sync_marker=None,
):
    config = RUNTIME.config
    header_title, separator, header_details = comment_header.partition("\n")
    content = []
    if header_title:
        content.append(
            {
                "type": "heading",
                "attrs": {"level": 3},
                "content": jira_text_nodes(header_title),
            }
        )
    if separator and header_details:
        content.append(
            {"type": "paragraph", "content": jira_text_nodes(header_details)}
        )
    if comment_body:
        content.append(
            {
                "type": "paragraph",
                "content": jira_text_nodes(comment_body),
            }
        )
    if attachment_links:
        content.append(
            {
                "type": "heading",
                "attrs": {"level": 4},
                "content": [{"type": "text", "text": "Attachments:"}],
            }
        )
    if sync_marker:
        content.append(
        {
            "type": "paragraph",
            "content": [
                {
                    "type": "text",
                    "text": f"Sync reference: {sync_marker.strip('[]')}",
                    "marks": [
                        {
                            "type": "textColor",
                            "attrs": {"color": "#6B778C"},
                        }
                    ],
                }
            ],
        }
        )
        for attachment in attachment_links:
            content.append(
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": attachment["filename"],
                            "marks": [
                                {
                                    "type": "link",
                                    "attrs": {"href": attachment["url"]},
                                }
                            ],
                        }
                    ],
                }
            )
    response = _post_with_rate_limit_retry(
        f"{config.jira_base_url}/rest/api/3/issue/{issue_key}/comment",
        auth=(config.jira_email, jira_api_token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={
            "body": {"type": "doc", "version": 1, "content": content},
            "properties": [
                {"key": "sd.public.comment", "value": {"internal": True}}
            ],
        },
        timeout=30,
    )
    _raise_for_jira_comment_response(response)
    return response.json()


def add_jira_comment_attachments(
    issue_key, jira_comment, attachment_links, jira_api_token
):
    config = RUNTIME.config
    body = jira_comment.get("body", {})
    if not isinstance(body, dict):
        return False

    existing_text = get_jira_comment_text(body)
    missing_attachments = [
        attachment
        for attachment in attachment_links
        if attachment["filename"] not in existing_text
    ]
    if not missing_attachments:
        return False

    updated_body = dict(body)
    content = list(body.get("content", []))
    if "Attachments:" not in existing_text:
        content.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Attachments:"}],
            }
        )
    for attachment in missing_attachments:
        content.append(
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": attachment["filename"],
                        "marks": [
                            {
                                "type": "link",
                                "attrs": {"href": attachment["url"]},
                            }
                        ],
                    }
                ],
            }
        )
    updated_body["content"] = content

    response = RUNTIME.session.put(
        f"{config.jira_base_url}/rest/api/3/issue/{issue_key}/comment/{jira_comment['id']}",
        auth=(config.jira_email, jira_api_token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={"body": updated_body},
        timeout=30,
    )
    response.raise_for_status()
    return True