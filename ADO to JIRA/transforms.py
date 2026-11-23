import json
import mimetypes
import os
import re
from datetime import datetime, timezone
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse


class CommentTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def plain_text(self):
        text = "".join(self.parts)
        lines = (line.strip() for line in text.splitlines())
        return "\n".join(line for line in lines if line)


class AttachmentLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "img":
            image_url = (
                attributes.get("src")
                or attributes.get("data-src")
                or attributes.get("data-original")
            )
            if image_url:
                self.urls[image_url] = attributes.get("alt") or "screenshot.png"
        elif tag == "a" and attributes.get("href"):
            self.urls.setdefault(attributes["href"], None)


def remove_html_formatting(comment_text):
    parser = CommentTextParser()
    parser.feed(comment_text or "")
    parser.close()
    return parser.plain_text()


def get_comment_attachment_urls(comment_text, organization_url):
    comment_text = comment_text or ""
    parser = AttachmentLinkParser()
    parser.feed(comment_text)
    parser.close()
    for match in re.finditer(r"!\[([^\]]*)\]\(\s*(<[^>]+>|[^)\s]+)", comment_text):
        url = match.group(2).strip("<>")
        parser.urls.setdefault(url, match.group(1) or "screenshot.png")

    attachments = []
    for url, name in parser.urls.items():
        parsed_url = urlparse(url)
        if parsed_url.scheme and parsed_url.scheme not in {"http", "https"}:
            continue
        if "/attachments/" not in parsed_url.path.lower():
            continue
        if not parsed_url.scheme and not parsed_url.netloc:
            url = urljoin(organization_url.rstrip("/") + "/", url.lstrip("/"))
        elif not parsed_url.scheme:
            url = urljoin(organization_url, url)
        if urlparse(url).scheme in {"http", "https"}:
            attachments.append({"url": url, "name": name})
    return attachments


def get_attachment_filename(
    attachment, content_disposition=None, content_type=None
):
    candidates = []
    if content_disposition:
        disposition = Message()
        disposition["Content-Disposition"] = content_disposition
        candidates.append(disposition.get_filename())

    query = parse_qs(urlparse(attachment["url"]).query)
    for key, values in query.items():
        if key.casefold() == "filename" and values:
            candidates.append(values[0])

    name = attachment.get("name")
    if name and name.strip().casefold() not in {"image", "attachment"}:
        candidates.append(name)

    candidates.append(unquote(os.path.basename(urlparse(attachment["url"]).path)))
    filename = ""
    for candidate in candidates:
        if not candidate:
            continue
        candidate = os.path.basename(str(candidate).replace("\\", "/").strip())
        if not candidate or candidate.casefold() in {"image", "attachment"}:
            continue
        if re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            candidate,
            re.IGNORECASE,
        ):
            continue
        filename = candidate
        break

    extension = None
    if content_type:
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/octet-stream":
            extension = mimetypes.guess_extension(media_type)
    if not filename:
        return f"attachment{extension or '.bin'}"
    if not os.path.splitext(filename)[1] and extension:
        return filename + extension
    return filename


def format_ado_timestamp(timestamp):
    if not timestamp:
        return "Unknown"
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return parsed_timestamp.strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return timestamp


def comment_timestamp_sort_key(comment):
    timestamp = comment["CommentCreatedDate"]
    if not timestamp:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed_timestamp.tzinfo is None:
            parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
        return parsed_timestamp.astimezone(timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def get_comment_author(comment):
    created_by = comment.get("createdBy", {})
    return (
        created_by.get("displayName")
        or created_by.get("uniqueName")
        or created_by.get("id")
        or "Unknown"
    )


def format_ado_field_value(value):
    if value is None:
        text = "(empty)"
    elif isinstance(value, dict):
        text = (
            value.get("displayName")
            or value.get("uniqueName")
            or value.get("name")
            or json.dumps(value, ensure_ascii=False, sort_keys=True)
        )
    elif isinstance(value, list):
        text = ", ".join(format_ado_field_value(item) for item in value)
    elif isinstance(value, str):
        text = remove_html_formatting(value)
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    else:
        text = str(value)

    if len(text) > 500:
        return text[:497] + "..."
    return text


def format_ado_field_name(field_name):
    label = field_name.rsplit(".", 1)[-1].replace("_", " ")
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", label)


def extract_ado_work_item_ids(summary):
    match = re.search(r"(?<!\d)\d{4,}(?!\d)", summary or "")
    if not match:
        raise ValueError(
            "No ADO work item number was found in the Jira issue summary."
        )
    return [match.group(0)]


def jira_text_nodes(text):
    return [{"type": "text", "text": text}] if text else []


def jira_body_contains_marker(body, marker):
    if isinstance(body, dict):
        return any(jira_body_contains_marker(value, marker) for value in body.values())
    if isinstance(body, list):
        return any(jira_body_contains_marker(value, marker) for value in body)
    return isinstance(body, str) and marker in body


def get_jira_comment_text(body):
    if isinstance(body, dict):
        if body.get("type") == "text":
            return body.get("text", "")
        return "".join(get_jira_comment_text(value) for value in body.values())
    if isinstance(body, list):
        return "".join(get_jira_comment_text(value) for value in body)
    return ""


def jira_comment_matching_text(header, body):
    title, separator, details = header.partition("\n")
    return title + (details if separator else "") + body


def comment_fingerprint(comment):
    return "\n".join(
        [
            comment["CommentCreatedBy"],
            comment["CommentCreatedDate"],
            comment["PlainText"],
        ]
    )