"""Pydantic request payload models and small input-normalization helpers
shared by app_routes and app_storage.
"""
from typing import Any, Dict, Optional

from pydantic import BaseModel


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Normalize loose on/off input from env vars or JSON into a real boolean."""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return bool(default)


def normalize_language_preference(value: Any, default: str = "en") -> str:
    """Clamp user-facing language choices to the supported portal locales."""
    text = str(value or "").strip().lower()
    return text if text in ("en", "es") else default


def normalize_theme_preference(value: Any, default: str = "light") -> str:
    """Clamp theme preference to the supported light/dark pair."""
    text = str(value or "").strip().lower()
    return text if text in ("light", "dark") else default


class LoginPayload(BaseModel):
    username: str
    password: str


class SignupPayload(BaseModel):
    username: str
    password: str
    role: Optional[str] = "user"
    preferred_language: Optional[str] = "en"
    preferred_theme: Optional[str] = "light"


class PreferencePayload(BaseModel):
    preferred_language: Optional[str] = None
    preferred_theme: Optional[str] = None


class ResetPasswordPayload(BaseModel):
    password: str


class RolePayload(BaseModel):
    role: str
    reason: Optional[str] = None
    confirm: Optional[str] = None


class UnlockPayload(BaseModel):
    reason: Optional[str] = None


class LockPayload(BaseModel):
    reason: str
    duration_minutes: Optional[int] = 30
    permanent: Optional[bool] = False


class CreateChatPayload(BaseModel):
    title: Optional[str] = "New Chat"


class UpdateChatPayload(BaseModel):
    title: Optional[str] = None
    is_saved: Optional[bool] = None
    folder_id: Optional[str] = None


class CreateChatFolderPayload(BaseModel):
    name: str


class UpdateChatFolderPayload(BaseModel):
    name: str


class CreateDocPayload(BaseModel):
    title: Optional[str] = "Untitled Document"
    type: Optional[str] = "markdown"
    content_markdown: Optional[str] = ""


class UpdateDocPayload(BaseModel):
    title: Optional[str] = None
    type: Optional[str] = None
    content_markdown: Optional[str] = None


class StarDocPayload(BaseModel):
    starred: bool


class PasteAbusePayload(BaseModel):
    doc_id: Optional[str] = None
    abuse_type: str  # "paste_cooldown" | "paste_duplicate" | "paste_too_long"
    detail: Optional[str] = None


class AnalyticsEventPayload(BaseModel):
    event_name: str
    surface: str
    metadata: Optional[Dict[str, Any]] = None


class CleanupPayload(BaseModel):
    dry_run: Optional[bool] = True
    reason: Optional[str] = "admin"
    required_bytes: Optional[int] = 0
