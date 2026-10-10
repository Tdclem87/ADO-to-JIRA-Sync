import tempfile
from pathlib import Path

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

from models import RUNTIME
from transforms import get_attachment_filename, get_jira_comment_text, jira_text_nodes


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


def upload_jira_attachment(
    issue_key, attachment, pat, jira_api_token, existing_filenames
):
    config = RUNTIME.config
    filename = get_attachment_filename(attachment)
    if filename in existing_filenames:
        return None, f"Skipped existing Jira attachment: {filename}", None

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
            encoder = MultipartEncoder(
                fields={"file": (filename, attachment_file, content_type)}
            )
            upload_response = RUNTIME.session.post(
                f"{config.jira_base_url}/rest/api/3/issue/{issue_key}/attachments",
                auth=(config.jira_email, jira_api_token),
                headers={
                    "X-Atlassian-Token": "no-check",
                    "Content-Type": encoder.content_type,
                },
                data=encoder,
                timeout=30,
            )
            upload_response.raise_for_status()
        uploaded_attachment = upload_response.json()[0]
        existing_filenames.add(filename)
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
    response = RUNTIME.session.post(
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