"""Email delivery (meerpad's, rebranded).

If ``smtp_host`` is configured the message is sent via SMTP. Otherwise it is
printed to the console, so the app is fully usable in development without an
email provider.
"""

import smtplib
import textwrap
from email.message import EmailMessage
from email.utils import formataddr

from .config import get_settings

settings = get_settings()

APP_NAME = "meertex"
SIGNATURE = f"-- \n{APP_NAME} · {settings.base_url}"


def _compose(subject: str, body: str) -> tuple[str, str]:
    full_subject = f"{APP_NAME} · {subject}"
    full_body = f"{textwrap.dedent(body).strip()}\n\n{SIGNATURE}"
    return full_subject, full_body


def send_email(to: str, subject: str, body: str) -> None:
    subject, body = _compose(subject, body)

    if not settings.smtp_host:
        _print_to_console(to, subject, body)
        return

    msg = EmailMessage()
    msg["From"] = formataddr((APP_NAME, settings.email_from))
    msg["To"] = to
    msg["Subject"] = subject
    # Disable Brevo/Sendinblue (Mailin) open and click tracking on a per-email basis.
    msg["X-Mailin-Track"] = "false"
    msg["X-Mailin-Track-Clicks"] = "false"
    msg["X-Mailin-Track-Opens"] = "false"
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    except Exception as exc:  # noqa: BLE001 - never let email failure break a request
        print(f"[emailer] SMTP send failed ({exc!r}); falling back to console:")
        _print_to_console(to, subject, body)


def _print_to_console(to: str, subject: str, body: str) -> None:
    banner = "=" * 70
    print(
        f"\n{banner}\n[EMAIL] From: {APP_NAME} <{settings.email_from}>\nTo: {to}\n"
        f"Subject: {subject}\n{'-' * 70}\n{body}\n{banner}\n",
        flush=True,
    )


def send_login_link(to: str, link: str, code: str) -> None:
    """Both ways to sign in: the magic link, and the code to type in by hand
    (the CLI only ever uses the code)."""
    send_email(
        to,
        "Your sign-in link",
        f"""
        Hi,

        Click the link below to sign in to {APP_NAME}:

        {link}

        Or enter this code on the sign-in screen:

            {code}

        Both expire in {settings.login_token_ttl_minutes} minutes.

        If you didn't request this, you can ignore this email.
        """,
    )


def send_invite(to: str, inviter: str, project_title: str, role: str, link: str) -> None:
    """Someone added you to a project. The link signs you in and lands on it."""
    send_email(
        to,
        f"{inviter} added you to {project_title}",
        f"""
        Hi,

        {inviter} added you to the paper "{project_title}" as {role}.

        Open it here (the link also signs you in):

        {link}

        You can also sign in at {settings.base_url} with this email address.
        """,
    )


# --- Reviews (DESIGN §13) --------------------------------------------------------

VERDICTS = {
    "approve": "approved it",
    "comment": "left comments",
    "changes_requested": "asked for changes",
}
THREAD_EVENTS = {
    "comment": "{actor} commented on {title}",
    "reply": "{actor} replied on {title}",
    "resolved": "{actor} resolved a thread on {title}",
    "applied": "{actor} applied a suggestion on {title}",
    "accepted": "{actor} accepted a thread on {title}",
    "reopened": "{actor} reopened a thread on {title}",
}
# The same, for the people the comment or the note @-mentions (§13, Mentions).
MENTION_EVENTS = {
    "comment": "{actor} mentioned you in a comment on {title}",
    "reply": "{actor} mentioned you in a reply on {title}",
    "edited": "{actor} mentioned you in a comment on {title}",
    "resolved": "{actor} resolved a thread on {title} and mentioned you",
    "accepted": "{actor} accepted a thread on {title} and mentioned you",
    "reopened": "{actor} reopened a thread on {title} and mentioned you",
}
QUOTE_LINES = 8
QUOTE_CHARS = 600


def _quoted(text: str) -> str:
    """The first lines of a span, each behind "> " as mail quotes are."""
    short = text if len(text) <= QUOTE_CHARS else text[:QUOTE_CHARS] + "…"
    lines = short.splitlines() or [""]
    if len(lines) > QUOTE_LINES:
        lines = [*lines[:QUOTE_LINES], "…"]
    return "\n".join(f"> {line}".rstrip() for line in lines)


def _why_mentioned(actor: str) -> str:
    base = settings.base_url.rstrip("/")
    return f"You get this mail because {actor} mentioned you. Turn these mails off at {base}/settings#mentions."


def send_review_submitted(
    to: str, reviewer: str, project_title: str, verdict: str, summary: str, comments: int, link: str,
    mentions_you: bool = False,
) -> None:
    """A review was submitted. Goes to the owner and the editors, and says so
    when the review mentions the one it goes to."""
    headline = f'{reviewer} reviewed "{project_title}" and {VERDICTS.get(verdict, verdict)}.'
    parts = [f"{headline} It mentions you." if mentions_you else headline]
    if summary.strip():
        parts.append(summary.strip())
    if comments:
        noun = "comment" if comments == 1 else "comments"
        parts.append(f"The review has {comments} {noun} on the text. Open it to answer them.")
    else:
        parts.append("Open the review to read it.")
    parts.append(link)
    send_email(to, f"{reviewer} reviewed {project_title}", "\n\n".join(parts))


def send_review_request(
    to: str, requester: str, project_title: str, version: str | None, note: str, link: str
) -> None:
    """An author asks a member to review the paper, on a version (its label)
    or on the text as it was then. The link starts the review there."""
    what = f'the version "{version}" of "{project_title}"' if version else f'"{project_title}" as it reads now'
    parts = [f"{requester} asked you to review {what}."]
    if note.strip():
        parts.append(note.strip())
    parts.append(
        "The link starts your review there. Your comments stay pending, and only you see them,"
        " until you finish the review."
    )
    parts.append(link)
    send_email(to, f"{requester} asked you to review {project_title}", "\n\n".join(parts))


def send_thread_event(
    to: str, actor: str, project_title: str, event: str, where: str, quote: str, text: str, link: str
) -> None:
    """Something happened in a thread: ``event`` is a key of THREAD_EVENTS.
    ``where`` names the file and lines, ``text`` is the comment or the note."""
    headline = THREAD_EVENTS.get(event, "{actor} wrote on {title}")
    parts = [headline.format(actor=actor, title=f'"{project_title}"') + "."]
    parts.append(f"{where}\n{_quoted(quote)}" if quote else where)
    if text.strip():
        parts.append(text.strip())
    parts.append("Open the thread to answer.")
    parts.append(link)
    send_email(to, headline.format(actor=actor, title=project_title), "\n\n".join(parts))


def send_review_mention(
    to: str, reviewer: str, project_title: str, verdict: str, summary: str, link: str
) -> None:
    """A submitted review mentions somebody who does not get the review mail
    (a reviewer or a viewer), in its summary or in one of its comments."""
    parts = [f'{reviewer} reviewed "{project_title}", {VERDICTS.get(verdict, verdict)} and mentioned you.']
    if summary.strip():
        parts.append(summary.strip())
    parts.append("Open the review to read it.")
    parts.append(link)
    parts.append(_why_mentioned(reviewer))
    send_email(to, f"{reviewer} mentioned you in a review of {project_title}", "\n\n".join(parts))


def send_mention(
    to: str, actor: str, project_title: str, event: str, where: str, quote: str, text: str, link: str
) -> None:
    """``send_thread_event`` for somebody the comment or the note mentions.
    It takes the place of that mail, so nobody gets both."""
    headline = MENTION_EVENTS.get(event, "{actor} mentioned you on {title}")
    parts = [headline.format(actor=actor, title=f'"{project_title}"') + "."]
    parts.append(f"{where}\n{_quoted(quote)}" if quote else where)
    if text.strip():
        parts.append(text.strip())
    parts.append("Open the thread to answer.")
    parts.append(link)
    parts.append(_why_mentioned(actor))
    send_email(to, f"{actor} mentioned you on {project_title}", "\n\n".join(parts))
