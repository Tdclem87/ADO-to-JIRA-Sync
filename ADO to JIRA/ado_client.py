from models import RUNTIME
from transforms import format_ado_field_value


def get_work_item_comments(work_item_tracking_client, work_item_id, pat, work_item=None):
    work_item = work_item or work_item_tracking_client.get_work_item(work_item_id)
    links = work_item._links.additional_properties
    comments_link = links.get("workItemComments", {}).get("href")

    if not comments_link:
        return []

    comments = []
    continuation_token = None
    while True:
        params = {"$top": 100}
        if continuation_token:
            params["continuationToken"] = continuation_token
        response = RUNTIME.session.get(
            comments_link,
            params=params,
            auth=("", pat),
            timeout=30,
        )
        response.raise_for_status()
        comments.extend(response.json().get("comments", []))
        continuation_token = response.headers.get("x-ms-continuationtoken")
        if not continuation_token:
            return comments


def get_work_item_status(work_item, work_item_id):
    fields = getattr(work_item, "fields", {}) or {}
    changed_by = fields.get("System.ChangedBy") or "Unknown"
    if isinstance(changed_by, dict):
        changed_by = (
            changed_by.get("displayName")
            or changed_by.get("uniqueName")
            or changed_by.get("id")
            or "Unknown"
        )
    return {
        "work_item_id": work_item_id,
        "state": fields.get("System.State") or "Unknown",
        "changed_by": changed_by,
        "changed_date": fields.get("System.ChangedDate") or "Unknown",
    }


def get_work_item_field_update(organization_url, work_item_id, revision, pat):
    if not revision or int(revision) <= 1:
        return None

    response = RUNTIME.session.get(
        f"{organization_url}/_apis/wit/workItems/{work_item_id}/updates/{revision}",
        params={"api-version": "7.1"},
        auth=("", pat),
        timeout=30,
    )
    response.raise_for_status()
    update = response.json()
    state_change = None
    ignored_fields = {
        "System.AuthorizedAs",
        "System.AuthorizedDate",
        "System.ChangedBy",
        "System.ChangedDate",
        "System.CommentCount",
        "System.History",
        "System.PersonId",
        "System.Rev",
        "System.RevisedDate",
        "System.State",
        "System.StateChangeDate",
        "System.Watermark",
    }
    changed_fields = {}
    revision_fields = update.get("fields", {})
    state = revision_fields.get("System.State")
    if (
        isinstance(state, dict)
        and "oldValue" in state
        and "newValue" in state
        and state["oldValue"] != state["newValue"]
    ):
        state_change = {
            "old_value": state["oldValue"],
            "new_value": state["newValue"],
        }

    for field_name, change in revision_fields.items():
        if (
            field_name in ignored_fields
            or not isinstance(change, dict)
            or "oldValue" not in change
            or "newValue" not in change
            or change["oldValue"] == change["newValue"]
        ):
            continue
        old_display_value = format_ado_field_value(change["oldValue"])
        new_display_value = format_ado_field_value(change["newValue"])
        if old_display_value == new_display_value:
            continue
        changed_fields[field_name] = {
            "old_value": change["oldValue"],
            "new_value": change["newValue"],
        }

    if not changed_fields and not state_change:
        return None

    changed_by = update.get("revisedBy") or {}
    if isinstance(changed_by, dict):
        changed_by = (
            changed_by.get("displayName")
            or changed_by.get("uniqueName")
            or changed_by.get("id")
            or "Unknown"
        )
    result = {
        "work_item_id": work_item_id,
        "revision": update.get("rev") or revision,
        "changed_by": changed_by,
        "changed_date": update.get("revisedDate") or "Unknown",
        "fields": changed_fields,
    }
    if state_change:
        result["state_change"] = state_change
    return result


def get_work_item_attachments(organization_url, work_item_id, pat):
    response = RUNTIME.session.get(
        f"{organization_url}/_apis/wit/workitems/{work_item_id}",
        params={"$expand": "relations", "api-version": "7.1"},
        auth=("", pat),
        timeout=30,
    )
    response.raise_for_status()
    relations = response.json().get("relations", [])
    return [
        {
            "url": relation["url"],
            "name": relation.get("attributes", {}).get("name") or "attachment",
            "work_item_id": work_item_id,
        }
        for relation in relations
        if relation.get("rel") == "AttachedFile" and relation.get("url")
    ]