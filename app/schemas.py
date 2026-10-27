"""Pydantic request/response models for sign-in and the account.

Project, folder, date and version request models are in app/project_schemas.py.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_serializer

# --- Auth ------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    # "cli" only changes nothing but the log line today. The CLI types the code.
    client: Literal["web", "cli"] = "web"
    # Where to land after signing in, e.g. an invited project.
    # Only same-site paths are honoured.
    next: str | None = None


class VerifyCodeRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=1, max_length=32)


class UserOut(BaseModel):
    id: str
    email: str
    name: str | None = None
    # None until the web app sends the browser's zone. Read as UTC until then.
    timezone: str | None = None
    api_token: str | None = None
    naming_pattern: str
    writing_days: int
    # The daily reminder mail (DESIGN §5, Reminders). 0 in reminder_days is
    # the day itself. reminder_hour is in the user's own time zone.
    reminders: bool = True
    reminder_days: list[int] = [14, 3, 1, 0]
    reminder_hour: int = 8
    # The profile picture (§2, Accounts), or None for the initials.
    avatar_url: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}

    @field_serializer("created_at")
    def _utc(self, value: datetime | None) -> str | None:
        # Naive UTC inside, ISO with Z outside, like every other route.
        return value.replace(microsecond=0).isoformat() + "Z" if value else None


class ProfileUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    naming_pattern: str | None = Field(default=None, max_length=200)
    writing_days: int | None = Field(default=None, ge=1, le=365)
    reminders: bool | None = None
    # Checked in the route, which says what is wrong with a value.
    reminder_days: list[int] | None = None
    reminder_hour: int | None = Field(default=None, ge=0, le=23)
