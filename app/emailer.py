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

        The link expires in {settings.login_token_ttl_minutes} minutes. After that,
        sign in at {settings.base_url} with this email address.
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
QUOTE_LINES = 8
QUOTE_CHARS = 600


def _quoted(text: str) -> str:
    """The first lines of a span, each behind "> " as mail quotes are."""
    short = text if len(text) <= QUOTE_CHARS else text[:QUOTE_CHARS] + "…"
    lines = short.splitlines() or [""]
    if len(lines) > QUOTE_LINES:
        lines = [*lines[:QUOTE_LINES], "…"]
    return "\n".join(f"> {line}".rstrip() for line in lines)


def send_review_submitted(
    to: str, reviewer: str, project_title: str, verdict: str, summary: str, comments: int, link: str
) -> None:
    """A review was submitted. Goes to the owner and the editors."""
    parts = [f'{reviewer} reviewed "{project_title}" and {VERDICTS.get(verdict, verdict)}.']
    if summary.strip():
        parts.append(summary.strip())
    if comments:
        noun = "comment" if comments == 1 else "comments"
        parts.append(f"The review has {comments} {noun} on the text. Open it to answer them.")
    else:
        parts.append("Open the review to read it.")
    parts.append(link)
    send_email(to, f"{reviewer} reviewed {project_title}", "\n\n".join(parts))


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
