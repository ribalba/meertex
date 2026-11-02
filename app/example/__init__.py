"""The example paper every new account starts with (docs/DESIGN.md §19).

``build.py`` makes it. This module only names its made-up coauthors, so that
the mailer and the instance dashboard can leave them out without importing
the builder.
"""

# The coauthors of every example paper, made up and on example.org. The same
# two accounts serve every example paper. They never get a mail, so nobody
# can sign in as them either.
JONAS = ("jonas.lindqvist@example.org", "Jonas Lindqvist")
AMARA = ("amara.okafor@example.org", "Amara Okafor")
PEOPLE = frozenset(email for email, _ in (JONAS, AMARA))


def made_up(email: str | None) -> bool:
    """Whether ``email`` is one of the example's made-up coauthors."""
    return (email or "").strip().lower() in PEOPLE
