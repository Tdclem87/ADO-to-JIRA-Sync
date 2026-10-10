import tempfile
import time
from pathlib import Path

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

from models import RUNTIME
from transforms import get_attachment_filename, get_jira_comment_text, jira_text_nodes


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


def get_jira_issue_data(issue_key, jira_api_token):
    config = RUNTIME.config
    response = RUNTIME.session.get(
        f"{config.jira_base_url}/rest/api/3/issue/{issue_key}",
        params={"fields": "summary,attachment,comment"},
        auth=(config.jira_email, jira_api_token),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    fields = response.json().get("fields", {})
    return (
        fields.get("summary", ""),
        fields.get("attachment", []),
        fields.get("comment", {}).get("comments", []),
    )


def _find_new_jira_attachment(
    issue_key, filename, jira_api_token, known_attachment_ids
):
    _, attachments, _ = get_jira_issue_data(issue_key, jira_api_token)
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
            content_type = response.headers.get(
                "Content-Type", "application/octet-stream"
            )

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
):
    config = RUNTIME.config
    content = [
        {"type": "paragraph", "content": jira_text_nodes(comment_header)}
    ]
    if comment_body:
        content.append(
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": comment_body,
                        "marks": [{"type": "strong"}],
                    }
                ],
            }
        )
    if attachment_links:
        content.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Attachments:"}],
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
    response.raise_for_status()
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