"""The words of a gate alert, in the shape each channel needs (a full email, a 160-character text, a printed slip)."""

from __future__ import annotations

KIND_LABEL = {
    "unauthorised": "Unauthorised removal",
    "armed_on_loan": "Item on loan left with its security bit on",
    "unissued": "Unissued tag at the gate",
    "authorised": "Authorised exit",
    "bit_off": "Item left with its security bit off, not on loan",
}


def gate_alert(channel: str, event: dict, when: str) -> tuple[str, str]:
    """(subject, body) for one alarm. `event` is `gate.service.event_out(...)`; `when` is a readable time."""
    what = KIND_LABEL.get(event["kind"], event["kind"])
    item = event["accession_no"] or "no catalogue item"
    title = event["title"] or "unknown tag"
    subject = f"ALARM at {event['gate_id']}: {what} - {item}"
    if channel == "sms":
        text = f"AISYS ALARM {event['gate_id']}: {what}. {item} '{title}' at {when}"
        return subject, text[:160]
    lines = [
        f"{what}",
        "",
        f"Gate:       {event['gate_id']}",
        f"Time:       {when}",
        f"Accession:  {event['accession_no'] or '-'}",
        f"Title:      {event['title'] or '-'}",
        f"Tag:        {event['tag_uid']}",
        f"People:     {event['people']}",
        f"Security bit read at the gate: {'ON' if event['security_armed'] in (True, None) else 'OFF'}",
    ]
    if event["on_loan"]:
        lines.append(f"On loan to: {event['loan_member_no']}")
    if event["note"]:
        lines += ["", event["note"]]
    if event["has_image"]:
        lines += ["", f"CCTV snapshot attached (camera {event['camera_id']}, image of event #{event['id']})."]
    elif event["image_error"]:
        lines += ["", f"No CCTV snapshot: {event['image_error']}"]
    if channel == "print":
        lines = ["*** SECURITY ALERT ***", *lines, "", "Please check the gate."]
    return subject, "\n".join(lines)


REMINDER_WHEN = {"due_soon": "due tomorrow", "due_today": "due today"}


def patron_reminder(
    channel: str,
    kind: str,
    *,
    member_name: str,
    title: str,
    accession_no: str,
    due_day: str,
    library: str,
    days_before: int,
) -> tuple[str, str]:
    """(subject, body) for one loan reminder. `due_day` is the due date as text, for example '12 Oct 2026'."""
    title = " ".join(title.split())  # a title from a spreadsheet may hold line breaks: never put them in a header
    when = REMINDER_WHEN.get(kind, "due soon")
    if kind == "due_soon" and days_before != 1:
        when = f"due in {days_before} days"
    subject = f"Reminder: '{title}' is {when}"[:200]
    if channel == "sms":
        return subject, f"{library}: '{title}' is {when} ({due_day}). Please return or renew it."[:160]
    first = member_name.split()[0] if member_name.strip() else "there"
    lines = [
        f"Hello {first},",
        "",
        f"This is a reminder from {library} that the item below is {when}.",
        "",
        f"Title:      {title}",
        f"Item:       {accession_no}",
        f"Due date:   {due_day}",
        "",
        "Please return it, or renew it at the desk, to avoid an overdue fine.",
    ]
    return subject, "\n".join(lines)
