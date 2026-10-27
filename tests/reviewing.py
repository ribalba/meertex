"""Helpers for the review tests (test_threads.py, test_reviews.py): a project
with text and members, edits to its live document, and captured mails."""

OWNER = "didi@example.com"
TEXT = (
    "\\section{Einführung}\n"
    "Wir messen die Energie 🔋 von LLMs.\n"
    "Grüße aus Köln 😀 und Zürich.\n"
    "The end.\n"
)


def js_length(text: str) -> int:
    """What JavaScript's String.length says, in UTF-16 code units."""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def project_with(client, text: str = TEXT, name: str = "paper", path: str = "main.tex", title: str = "Green LLMs") -> dict:
    p = client.post("/api/projects", json={"name": name, "title": title}).json()
    put(client, p["id"], path, text)
    return p


def put(client, pid: str, path: str, text: str) -> None:
    r = client.put(f"/api/projects/{pid}/files/{path}", content=text.encode())
    assert r.status_code == 200, r.text


def live_text(pid: str, path: str = "main.tex") -> str | None:
    from app import live

    content = live.read_file(pid, path)
    return content.data.decode() if content else None


def snapshot(pid: str) -> str:
    from app import live

    return live.snapshot(pid)


def invite(client, pid: str, email: str, role: str) -> None:
    r = client.post(f"/api/projects/{pid}/members", json={"email": email, "role": role})
    assert r.status_code == 201, r.text


def thread(client, pid: str, needle: str, body: str = "Why?", text: str = TEXT, status: int = 201, **extra) -> dict:
    """A thread on the first ``needle`` in ``text`` (the live text)."""
    start = text.index(needle)
    payload = {"path": "main.tex", "start": start, "end": start + len(needle), "body": body, **extra}
    r = client.post(f"/api/projects/{pid}/threads", json=payload)
    assert r.status_code == status, r.text
    return r.json()


def capture_mails(monkeypatch) -> list[dict]:
    """Every mail sent from now on, invites left out."""
    from app import emailer
    from app.routers import members

    sent: list[dict] = []
    monkeypatch.setattr(emailer, "send_email", lambda to, subject, body: sent.append(
        {"to": to, "subject": subject, "body": body}))
    monkeypatch.setattr(members, "send_invite", lambda *args: None)
    return sent
