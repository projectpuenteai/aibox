"""FastAPI route registrations for the ai-control storage layer.

Mounted by ``mount_app_storage`` in ``app_storage``. All handlers close over a
``StorageRuntime`` instance (``rt``) and reference its methods/state. Pulling
the routes out keeps ``app_storage.py`` focused on the runtime class itself.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from app_payloads import (
    AnalyticsEventPayload,
    CleanupPayload,
    CreateChatFolderPayload,
    CreateChatPayload,
    CreateDocPayload,
    LockPayload,
    LoginPayload,
    PasteAbusePayload,
    PreferencePayload,
    ResetPasswordPayload,
    RolePayload,
    SignupPayload,
    StarDocPayload,
    UnlockPayload,
    UpdateChatFolderPayload,
    UpdateChatPayload,
    UpdateDocPayload,
    _coerce_bool,
    normalize_language_preference,
    normalize_theme_preference,
)

logger = logging.getLogger("aibox.ai_control.storage")


def register_routes(app, rt) -> None:
    """Register every /v1/app/* route + the /wiki redirects + same-origin guard."""

    @app.middleware("http")
    async def same_origin_write_guard(req: Request, call_next):
        method = str(req.method or "").upper()
        if method in ("POST", "PUT", "PATCH", "DELETE") and req.url.path.startswith("/v1/") and req.cookies.get(rt.cookie):
            try:
                rt.validate_same_origin_write(req)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(req)

    @app.get("/wiki")
    @app.get("/wiki/")
    def wiki_root_redirect(req: Request):
        return RedirectResponse(url=rt.build_wiki_redirect_target(req), status_code=307)

    @app.get("/wiki/{wiki_path:path}")
    def wiki_language_redirect(wiki_path: str, req: Request):
        return RedirectResponse(url=rt.build_wiki_redirect_target(req, wiki_path), status_code=307)

    @app.post("/v1/app/auth/signup")
    def app_signup(p: SignupPayload, req: Request):
        """Create a new account, user directories, and an initial restrictions row.

        Enforces a per-IP hourly signup cap and an admin-controlled kill switch so a
        single abusive client cannot flood the user table. All outcomes are logged to
        security_events for audit visibility.
        """
        username = (p.username or "").strip(); pw = p.password or ""; role = (p.role or "user").strip().lower()
        preferred_language = normalize_language_preference(p.preferred_language, default="es")
        preferred_theme = normalize_theme_preference(p.preferred_theme, default="light")
        ip_ = rt.ip(req)
        endpoint = req.url.path
        if not rt.allow_public_signup:
            with rt.tx() as c:
                rt.log_event(c, user=None, ip_=ip_, endpoint=endpoint, et="signup_disabled", sev="warning", detail=f"Signup blocked (public signup disabled) for username={username[:50]!r}", act="blocked")
            raise HTTPException(403, "Public signup is disabled")
        if len(username) < 3: raise HTTPException(400, "Username must be at least 3 characters")
        if len(username) > 50: raise HTTPException(400, "Username must be at most 50 characters")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,50}", username):
            raise HTTPException(400, "Username may only contain letters, digits, and the characters . _ @ -")
        min_password_len = rt.guest_password_min_length if role == "guest" else rt.user_password_min_length
        if len(pw) < min_password_len: raise HTTPException(400, f"Password must be at least {min_password_len} characters")
        if len(pw) > 128: raise HTTPException(400, "Password must be at most 128 characters")
        if role not in ("user", "guest"): role = "user"
        # Per-IP signup rate limit. Stored in the FK-free ip_rate_events table
        # since there is no user row to anchor a synthetic ip:<addr> key against.
        rate_key = ip_ or "unknown"
        with rt.tx() as c:
            recent = rt.ip_rate_count(c, rate_key, "signup", sec=3600)
            if recent >= rt.signup_max_per_hour_per_ip:
                rt.log_event(c, user=None, ip_=ip_, endpoint=endpoint, et="signup_rate_block", sev="warning", detail=f"Signup blocked: {recent} attempts in last hour from ip={ip_}", obs=float(recent), th=float(rt.signup_max_per_hour_per_ip), act="rate_block")
                rt.persist_and_raise(c, 429, "Too many signups from this network; try later")
            if c.execute("SELECT 1 FROM users WHERE username_norm=?", (rt.nuser(username),)).fetchone() is not None:
                rt.log_event(c, user=None, ip_=ip_, endpoint=endpoint, et="signup_conflict", sev="info", detail=f"Signup rejected: username {username!r} already exists", act="blocked")
                rt.ip_rate_add(c, rate_key, "signup")
                rt.persist_and_raise(c, 409, "Username already exists")
            uid_ = rt.uid(); rt.ensure_user_dirs(uid_)
            c.execute(
                "INSERT INTO users(id,username,username_norm,password_hash,role,created_at,storage_bytes_used,preferred_language,preferred_theme) VALUES(?,?,?,?,?,?,?,?,?)",
                (uid_, username, rt.nuser(username), rt._ph.hash(pw), role, rt.now_iso(), 0, preferred_language, preferred_theme),
            )
            rt.ensure_restrictions_row(c, uid_)
            created_user = c.execute("SELECT * FROM users WHERE id=?", (uid_,)).fetchone()
            rt.record_analytics_event(c, event_type="accounts_created", surface="auth", user=created_user)
            rt.ip_rate_add(c, rate_key, "signup")
            rt.log_event(c, user=created_user, ip_=ip_, endpoint=endpoint, et="account_created", sev="info", detail=f"Account created: {username} role={role}", act="signup")
        return {"ok": True, "user": {"id": uid_, "username": username, "role": role, "preferred_language": preferred_language, "preferred_theme": preferred_theme}}

    @app.post("/v1/app/auth/login")
    def app_login(p: LoginPayload, req: Request):
        """Validate credentials, enforce lockouts, and issue a session cookie."""
        username = (p.username or "").strip(); pw = p.password or ""
        if not username or not pw: raise HTTPException(400, "Missing username or password")
        if len(username) > 50: raise HTTPException(400, "Username must be at most 50 characters")
        if len(pw) > 128: raise HTTPException(400, "Password must be at most 128 characters")
        nn, ip_ = rt.nuser(username), rt.ip(req)
        endpoint = req.url.path
        with rt.tx() as c:
            a = c.execute("SELECT * FROM login_attempts WHERE username_norm=? AND ip=?", (nn, ip_)).fetchone()
            if a is not None and a["lockout_until_ts"] and int(a["lockout_until_ts"]) > rt.now_ts():
                rt.log_event(c, user=None, ip_=ip_, endpoint=endpoint, et="auth_login_blocked", sev="warning", detail=f"Login blocked while locked out: username={username!r}", obs=float(a["fail_count"] or 0), th=float(rt.login_fail_limit), act="locked_out")
                rt.persist_and_raise(c, 429, "Too many login attempts; try later")
            user = c.execute("SELECT * FROM users WHERE username_norm=? AND is_deleted=0", (nn,)).fetchone()
            valid = False
            if user is not None:
                try: rt._ph.verify(user["password_hash"], pw); valid = True
                except VerifyMismatchError: valid = False
            if not valid:
                if a is None or rt.now_ts() - int(a["first_attempt_ts"]) > rt.login_window:
                    fc = 1
                    c.execute("INSERT OR REPLACE INTO login_attempts(username_norm,ip,fail_count,first_attempt_ts,last_attempt_ts,lockout_until_ts) VALUES(?,?,1,?,?,NULL)", (nn, ip_, rt.now_ts(), rt.now_ts()))
                    lock = None
                else:
                    fc = int(a["fail_count"]) + 1
                    lock = rt.now_ts() + rt.login_window if fc >= rt.login_fail_limit else None
                    c.execute("UPDATE login_attempts SET fail_count=?, last_attempt_ts=?, lockout_until_ts=? WHERE username_norm=? AND ip=?", (fc, rt.now_ts(), lock, nn, ip_))
                # Log the failure with attempt count but never the submitted password.
                rt.log_event(c, user=user, ip_=ip_, endpoint=endpoint, et="auth_login_failure", sev="warning", detail=f"Failed login for username={username!r}", obs=float(fc), th=float(rt.login_fail_limit), act="auth_fail")
                if lock is not None:
                    rt.log_event(c, user=user, ip_=ip_, endpoint=endpoint, et="auth_login_lockout", sev="error", detail=f"Lockout engaged for username={username!r} ip={ip_}", obs=float(fc), th=float(rt.login_fail_limit), act="lockout")
                detail = "Too many login attempts; try later" if a is not None and int(a["fail_count"]) + 1 >= rt.login_fail_limit else "Invalid username or password"
                status_code = 429 if detail.startswith("Too many login attempts") else 401
                rt.persist_and_raise(c, status_code, detail)
            c.execute("DELETE FROM login_attempts WHERE username_norm=? AND ip=?", (nn, ip_))
            rt.ensure_restrictions_row(c, user["id"])
            if rt.is_manual_locked(c, user) and user["role"] != "admin":
                rt.log_event(c, user=user, ip_=ip_, endpoint=endpoint, et="auth_login_blocked", sev="warning", detail=f"Login blocked: account manually locked ({user['username']})", act="manual_locked")
                rt.persist_and_raise(c, 423, "Account is locked by administrator")
            raw = secrets.token_urlsafe(32)
            c.execute("INSERT INTO sessions(id,user_id,token_hash,created_at,expires_at,last_accessed_at,ip,user_agent,revoked_at) VALUES(?,?,?,?,?,?,?,?,NULL)", (rt.uid(), user["id"], rt.token_hash(raw), rt.now_iso(), (datetime.now(timezone.utc)+timedelta(days=rt.session_days)).isoformat(), rt.now_iso(), ip_, req.headers.get("user-agent", "")))
            c.execute("UPDATE users SET last_login_at=?, last_active_at=? WHERE id=?", (rt.now_iso(), rt.now_iso(), user["id"]))
            refreshed_user = c.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone() or user
            rt.record_analytics_event(c, event_type="logins_succeeded", surface="auth", user=refreshed_user)
            rt.log_event(c, user=refreshed_user, ip_=ip_, endpoint=endpoint, et="auth_login_success", sev="info", detail=f"Login success: {user['username']} role={user['role']}", act="auth_success")
            return rt.mk_resp({"ok": True, "user": {"id": user["id"], "username": user["username"], "role": user["role"]}}, token=raw)

    @app.post("/v1/app/auth/logout")
    def app_logout(req: Request):
        tok = req.cookies.get(rt.cookie)
        if tok:
            with rt.tx() as c:
                row = c.execute(
                    "SELECT u.id AS id, u.username AS username, u.role AS role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? LIMIT 1",
                    (rt.token_hash(tok),),
                ).fetchone()
                c.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (rt.now_iso(), rt.token_hash(tok)))
                if row is not None:
                    rt.log_event(c, user=row, ip_=rt.ip(req), endpoint=req.url.path, et="auth_logout", sev="info", detail=f"Logout: {row['username']}", act="auth_logout")
                    if row["role"] == "guest":
                        c.execute("UPDATE users SET guest_logout_at=? WHERE id=?", (rt.now_iso(), row["id"]))
        return rt.mk_resp({"ok": True}, clear=True)

    @app.get("/v1/app/auth/me")
    def app_me(req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req)
            rr = rt.get_restrictions(c, u["id"])
            lock_until = "permanent" if int(rr["manual_lock_permanent"] or 0) == 1 else rr["manual_locked_until"]
            return {
                "ok": True,
                "user": {
                    "id": u["id"],
                    "username": u["username"],
                    "role": u["role"],
                    "created_at": u["created_at"],
                    "last_login_at": u["last_login_at"],
                    "last_active_at": u["last_active_at"],
                    "storage_bytes_used": int(u["storage_bytes_used"]),
                    "preferred_language": normalize_language_preference(u["preferred_language"], default="es"),
                    "preferred_theme": normalize_theme_preference(u["preferred_theme"], default="light"),
                    "locked_until": lock_until,
                    "docs_write_blocked_until": rr["docs_write_blocked_until"],
                    "ai_prompt_cooldown_until": rr["ai_prompt_cooldown_until"],
                    "ai_send_blocked_until": rr["ai_send_blocked_until"],
                },
            }

    @app.post("/v1/app/auth/preferences")
    def app_update_preferences(p: PreferencePayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            preferred_language = normalize_language_preference(p.preferred_language, default=normalize_language_preference(u["preferred_language"], default="es"))
            preferred_theme = normalize_theme_preference(p.preferred_theme, default=normalize_theme_preference(u["preferred_theme"], default="light"))
            c.execute("UPDATE users SET preferred_language=?, preferred_theme=? WHERE id=?", (preferred_language, preferred_theme, u["id"]))
            return {"ok": True, "user": {"id": u["id"], "username": u["username"], "role": u["role"], "preferred_language": preferred_language, "preferred_theme": preferred_theme}}

    @app.get("/v1/app/admin/users")
    def app_admin_users(req: Request):
        with rt.tx() as c:
            rt.req_user(c, req, admin=True)
            flag_cutoff = rt.now_ts() - 86400
            rows = c.execute(
                """
SELECT u.id,u.username,u.role,u.created_at,u.last_login_at,u.last_active_at,u.storage_bytes_used,
       CASE WHEN COALESCE(r.manual_lock_permanent,0)=1 THEN 'permanent' ELSE r.manual_locked_until END AS locked_until,
       r.manual_lock_reason AS lock_reason,
       r.docs_write_blocked_until,r.ai_prompt_cooldown_until,r.ai_send_blocked_until,
       COALESCE(r.manual_lock_permanent,0) AS manual_lock_permanent,
       COALESCE((SELECT COUNT(*) FROM security_events se
                 WHERE se.user_id=u.id AND se.severity IN ('warn','block')
                 AND se.created_ts >= ?), 0) AS recent_flag_count
FROM users u
LEFT JOIN user_restrictions r ON r.user_id=u.id
WHERE u.is_deleted=0
ORDER BY u.username COLLATE NOCASE
                """,
                (flag_cutoff,)
            ).fetchall()
            return {"ok": True, "users": [dict(r) for r in rows]}

    @app.post("/v1/app/admin/users/{uid_}/reset-password")
    def app_admin_reset(uid_: str, p: ResetPasswordPayload, req: Request):
        if len(p.password or "") < rt.admin_password_min_length: raise HTTPException(400, f"Password must be at least {rt.admin_password_min_length} characters")
        if len(p.password or "") > 128: raise HTTPException(400, "Password must be at most 128 characters")
        with rt.tx() as c:
            admin = rt.req_user(c, req, admin=True, write=True)
            u = c.execute("SELECT * FROM users WHERE id=? AND is_deleted=0", (uid_,)).fetchone()
            if u is None: raise HTTPException(404, "User not found")
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (rt._ph.hash(p.password), uid_))
            rt.log_event(c, user=admin, ip_=rt.ip(req), endpoint=req.url.path, et="admin_password_reset", sev="info", detail=f"Password reset for {u['username']}", act="password_reset")
        return {"ok": True}

    @app.post("/v1/app/admin/users/{uid_}/role")
    def app_admin_role(uid_: str, p: RolePayload, req: Request):
        role = (p.role or "").strip().lower()
        if role not in ("admin", "user", "guest"): raise HTTPException(400, "Invalid role")
        with rt.tx() as c:
            admin = rt.req_user(c, req, admin=True, write=True)
            target = c.execute("SELECT * FROM users WHERE id=? AND is_deleted=0", (uid_,)).fetchone()
            if target is None: raise HTTPException(404, "User not found")
            old_role = target["role"]
            if old_role == role:
                return {"ok": True, "role": role, "unchanged": True}
            reason = str(p.reason or "").strip()
            if role == "admin" and old_role != "admin":
                if p.confirm != "PROMOTE_ADMIN" or len(reason) < 8:
                    raise HTTPException(400, "Promoting a user to admin requires confirm='PROMOTE_ADMIN' and a reason")
            if old_role == "admin" and role != "admin":
                admin_count = int(c.execute("SELECT COUNT(*) c FROM users WHERE role='admin' AND is_deleted=0").fetchone()["c"])
                if admin_count <= 1:
                    raise HTTPException(409, "Cannot demote the last admin account")
                if len(reason) < 8:
                    raise HTTPException(400, "Demoting an admin requires a reason")
            c.execute("UPDATE users SET role=? WHERE id=?", (role, uid_))
            rt.ensure_restrictions_row(c, uid_)
            sev = "warning" if role == "admin" or old_role == "admin" else "info"
            reason_suffix = f" reason={reason[:120]!r}" if reason else ""
            rt.log_event(c, user=admin, ip_=rt.ip(req), endpoint=req.url.path, et="admin_role_change", sev=sev, detail=f"{admin['username']} changed role of {target['username']}: {old_role} -> {role}.{reason_suffix}", act="role_change")
        return {"ok": True, "role": role}

    @app.post("/v1/app/admin/users/{uid_}/lock")
    def app_admin_lock(uid_: str, p: LockPayload, req: Request):
        reason = (p.reason or "").strip()
        if not reason:
            raise HTTPException(400, "Lock reason is required")
        with rt.tx() as c:
            admin = rt.req_user(c, req, admin=True, write=True)
            u = c.execute("SELECT * FROM users WHERE id=? AND is_deleted=0", (uid_,)).fetchone()
            if u is None: raise HTTPException(404, "User not found")
            permanent = bool(p.permanent)
            until = None
            if not permanent:
                minutes = max(1, min(int(p.duration_minutes or 30), 525600))
                until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
            rt.ensure_restrictions_row(c, uid_)
            c.execute(
                "UPDATE user_restrictions SET manual_locked_until=?, manual_lock_reason=?, manual_locked_by=?, manual_lock_permanent=?, updated_at=? WHERE user_id=?",
                (until, reason, admin["username"], 1 if permanent else 0, rt.now_iso(), uid_),
            )
            rt.sync_legacy_lock_fields(c, uid_)
            rt.log_event(c, user=admin, ip_=rt.ip(req), endpoint=req.url.path, et="admin_lock", sev="warning", detail=f"Locked {u['username']} ({reason})", act="lock")
        return {"ok": True, "locked_until": (until or "permanent"), "permanent": permanent}

    @app.post("/v1/app/admin/users/{uid_}/unlock")
    def app_admin_unlock(uid_: str, p: UnlockPayload, req: Request):
        with rt.tx() as c:
            admin = rt.req_user(c, req, admin=True, write=True)
            u = c.execute("SELECT * FROM users WHERE id=? AND is_deleted=0", (uid_,)).fetchone()
            if u is None: raise HTTPException(404, "User not found")
            rt.ensure_restrictions_row(c, uid_)
            c.execute("UPDATE user_restrictions SET manual_locked_until=NULL, manual_lock_reason=NULL, manual_locked_by=NULL, manual_lock_permanent=0, updated_at=? WHERE user_id=?", (rt.now_iso(), uid_))
            rt.sync_legacy_lock_fields(c, uid_)
            rt.log_event(c, user=admin, ip_=rt.ip(req), endpoint=req.url.path, et="admin_unlock", sev="info", detail=f"Unlocked {u['username']} ({p.reason or 'manual'})", act="unlock")
        return {"ok": True}

    @app.post("/v1/app/admin/users/{uid_}/delete")
    def app_admin_delete_user(uid_: str, req: Request):
        user_dir_removed = False
        with rt.tx() as c:
            admin = rt.req_user(c, req, admin=True, write=True)
            u = c.execute("SELECT * FROM users WHERE id=? AND is_deleted=0", (uid_,)).fetchone()
            if u is None:
                raise HTTPException(404, "User not found")
            if u["id"] == admin["id"]:
                raise HTTPException(409, "You cannot delete your own account")
            if u["role"] == "admin":
                raise HTTPException(403, "Admin accounts cannot be deleted")
            username_norm = str(u["username_norm"] or "")
            c.execute("DELETE FROM login_attempts WHERE username_norm=?", (username_norm,))
            c.execute("DELETE FROM security_events WHERE user_id=?", (uid_,))
            c.execute("DELETE FROM rate_events WHERE user_id=?", (uid_,))
            c.execute("DELETE FROM users WHERE id=?", (uid_,))
            rt.log_event(c, user=admin, ip_=rt.ip(req), endpoint=req.url.path, et="admin_delete_user", sev="warning", detail=f"Deleted {u['username']}", act="delete_user")
        try:
            user_dir_removed = rt.remove_user_dir(uid_)
        except Exception:
            logger.exception("Failed to remove user directory for deleted user %s", uid_)
        return {"ok": True, "user_dir_removed": user_dir_removed}

    @app.get("/v1/app/admin/storage-insights")
    def app_admin_storage_insights(req: Request, top_users: int = 20, top_files: int = 40):
        top_users = max(1, min(int(top_users), 200))
        top_files = max(1, min(int(top_files), 200))
        with rt.tx() as c:
            rt.req_user(c, req, admin=True)
            users = c.execute(
                "SELECT id,username,role,storage_bytes_used,last_active_at,last_login_at,created_at FROM users WHERE is_deleted=0 ORDER BY storage_bytes_used DESC LIMIT ?",
                (top_users,),
            ).fetchall()
            docs = c.execute(
                """
SELECT d.id,d.user_id,u.username,d.title,d.type,d.size_bytes,d.created_at,d.updated_at,d.last_accessed_at,
       d.is_deleted,d.is_starred,d.deleted_at
FROM documents d JOIN users u ON u.id=d.user_id
ORDER BY d.size_bytes DESC, COALESCE(d.last_accessed_at,d.updated_at,d.created_at) ASC
LIMIT ?
                """,
                (top_files,),
            ).fetchall()
            chats = c.execute(
                """
SELECT h.id,h.user_id,u.username,h.title,h.size_bytes,h.token_count_estimate,h.created_at,h.updated_at,h.last_accessed_at,
       h.is_deleted,h.deleted_at
FROM chats h JOIN users u ON u.id=h.user_id
ORDER BY h.size_bytes DESC, COALESCE(h.last_accessed_at,h.updated_at,h.created_at) ASC
LIMIT ?
                """,
                (top_files,),
            ).fetchall()
            return {"ok": True, "top_users": [dict(r) for r in users], "largest_documents": [dict(r) for r in docs], "largest_chats": [dict(r) for r in chats]}

    @app.post("/v1/app/admin/storage-cleanup")
    def app_admin_storage_cleanup(p: CleanupPayload, req: Request):
        reason = str(p.reason or "admin").strip()[:80] or "admin"
        required = max(0, int(p.required_bytes or 0))
        dry_run = bool(True if p.dry_run is None else p.dry_run)
        with rt.tx() as c:
            rt.req_user(c, req, admin=True, write=not dry_run)
        try:
            result = rt.run_cleanup(reason=f"admin:{reason}", required=required, dry_run=dry_run)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True, "cleanup": result}

    @app.get("/v1/app/admin/security-events")
    def app_admin_events(req: Request, limit: int = 200):
        limit = max(1, min(int(limit), 1000))
        with rt.tx() as c:
            rt.req_user(c, req, admin=True)
            rows = c.execute("SELECT * FROM security_events ORDER BY created_ts DESC LIMIT ?", (limit,)).fetchall()
            return {"ok": True, "events": [dict(r) for r in rows]}

    @app.get("/v1/app/admin/analytics/summary")
    def app_admin_analytics_summary(req: Request, date_from: Optional[str] = None, date_to: Optional[str] = None):
        with rt.tx() as c:
            rt.req_user(c, req, admin=True)
            start_day, end_day = rt.analytics_range(date_from, date_to)
            payload = rt.analytics_payload(c, start_day, end_day)
            return {
                "ok": True,
                "date_from": payload["date_from"],
                "date_to": payload["date_to"],
                "generated_at": payload["generated_at"],
                "metrics_version": payload["metrics_version"],
                "summary": payload["summary"],
            }

    @app.get("/v1/app/admin/analytics/timeseries")
    def app_admin_analytics_timeseries(req: Request, date_from: Optional[str] = None, date_to: Optional[str] = None):
        with rt.tx() as c:
            rt.req_user(c, req, admin=True)
            start_day, end_day = rt.analytics_range(date_from, date_to)
            payload = rt.analytics_payload(c, start_day, end_day)
            return {
                "ok": True,
                "date_from": payload["date_from"],
                "date_to": payload["date_to"],
                "generated_at": payload["generated_at"],
                "metrics_version": payload["metrics_version"],
                "timeseries": payload["timeseries"],
            }

    @app.get("/v1/app/admin/analytics/export")
    def app_admin_analytics_export(
        req: Request,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        export_format: str = "json",
    ):
        fmt = str(export_format or "json").strip().lower()
        if fmt not in ("json", "csv"):
            raise HTTPException(400, "Invalid export_format")
        with rt.tx() as c:
            admin = rt.req_user(c, req, admin=True, write=True)
            start_day, end_day = rt.analytics_range(date_from, date_to)
            payload = rt.analytics_payload(c, start_day, end_day)
            c.execute(
                """
                INSERT INTO analytics_exports(
                    id,format,date_from,date_to,created_at,created_by_user_id,status,file_path,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    rt.uid(),
                    fmt,
                    start_day,
                    end_day,
                    rt.now_iso(),
                    admin["id"],
                    "generated",
                    None,
                    rt._analytics_metadata_json({
                        "metrics_version": payload.get("metrics_version"),
                        "row_count": len(payload.get("timeseries") or []),
                    }),
                ),
            )
            filename = f"aibox-analytics-{start_day}-to-{end_day}.{fmt}"
            headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
            if fmt == "csv":
                return Response(rt.analytics_csv(payload), media_type="text/csv; charset=utf-8", headers=headers)
            return JSONResponse(payload, headers=headers)

    @app.post("/v1/app/analytics/events")
    def app_analytics_event_ingest(p: AnalyticsEventPayload, req: Request):
        event_name = str(p.event_name or "").strip().lower()
        surface = str(p.surface or "").strip().lower()
        allowed_surfaces = {"chat", "docs", "wiki", "learn", "portal", "auth", "admin"}
        with rt.tx() as c:
            u = rt.req_user(c, req)
            if event_name not in rt.analytics_frontend_events:
                rt.log_event(c, user=u, ip_=rt.ip(req), endpoint=req.url.path, et="analytics_event_rejected", sev="warn", detail=f"Invalid analytics event_name: {event_name}", act="analytics_rejected")
                raise HTTPException(400, "Invalid event_name")
            if surface not in allowed_surfaces:
                rt.log_event(c, user=u, ip_=rt.ip(req), endpoint=req.url.path, et="analytics_event_rejected", sev="warn", detail=f"Invalid analytics surface: {surface}", act="analytics_rejected")
                raise HTTPException(400, "Invalid surface")
            rt.record_analytics_event(
                c,
                event_type=event_name,
                surface=surface,
                user=u,
                metadata=rt._clone_data(p.metadata or {}),
            )
        return {"ok": True}

    def write_payload(c: sqlite3.Connection, user: sqlite3.Row, req: Request, path: Path, obj: Dict[str, Any], op: str) -> int:
        try:
            return rt.write_json_atomic(path, obj)
        except Exception as e:
            rt.log_event(
                c,
                user=user,
                ip_=rt.ip(req),
                endpoint=req.url.path,
                et="file_write_failed",
                sev="error",
                detail=f"{op}: {type(e).__name__}: {e}",
                act="write_failed",
            )
            raise HTTPException(500, "Failed to persist content")

    def enforce_content_access(user: sqlite3.Row, owner_id: str) -> None:
        if user["role"] == "admin" and user["id"] != owner_id:
            raise HTTPException(403, "Admin cannot access other users' content bodies")

    def get_doc(c: sqlite3.Connection, u: sqlite3.Row, did: str, include_deleted: bool = False) -> sqlite3.Row:
        q = "SELECT * FROM documents WHERE id=?" + ("" if u["role"] == "admin" else " AND user_id=?")
        args = (did,) if u["role"] == "admin" else (did, u["id"])
        r = c.execute(q, args).fetchone()
        if r is None:
            raise HTTPException(404, "Document not found")
        enforce_content_access(u, r["user_id"])
        if not include_deleted and int(r["is_deleted"]) == 1:
            raise HTTPException(400, "Document is deleted")
        return r

    def get_chat(c: sqlite3.Connection, u: sqlite3.Row, cid: str, include_deleted: bool = False) -> sqlite3.Row:
        q = "SELECT * FROM chats WHERE id=?" + ("" if u["role"] == "admin" else " AND user_id=?")
        args = (cid,) if u["role"] == "admin" else (cid, u["id"])
        r = c.execute(q, args).fetchone()
        if r is None:
            raise HTTPException(404, "Chat not found")
        enforce_content_access(u, r["user_id"])
        if not include_deleted and int(r["is_deleted"]) == 1:
            raise HTTPException(400, "Chat is deleted")
        return r

    def load_chat_json(row: sqlite3.Row) -> Dict[str, Any]:
        p = rt.safe_path(row["file_path"], row["user_id"])
        if not p.exists():
            return {"id": row["id"], "title": row["title"], "messages": [], "created_at": row["created_at"], "updated_at": row["updated_at"], "version": 1}
        return rt.read_json(p)

    def append_chat(
        c: sqlite3.Connection,
        row: sqlite3.Row,
        role: str,
        text: str,
        user: sqlite3.Row,
        req: Request,
        reason: str,
        scope: str = "chat",
        citations: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[int, int, List[Dict[str, Any]]]:
        o = load_chat_json(row)
        msgs = o.get("messages") if isinstance(o.get("messages"), list) else []
        message = {"role": role, "content": str(text or ""), "created_at": rt.now_iso()}
        if citations:
            message["citations"] = rt._clone_data(citations)
        msgs.append(message)
        o["messages"] = msgs
        o["updated_at"] = rt.now_iso()
        o["version"] = int(o.get("version", 0)) + 1
        o["title"] = row["title"]
        size_est = len(json.dumps(o, ensure_ascii=False).encode("utf-8"))
        warns = rt.chat_limits(c, user, req, size_est, create=False, scope=scope)
        rt.ensure_capacity(size_est, reason, c)
        b = write_payload(c, user, req, rt.safe_path(row["file_path"], row["user_id"]), o, reason)
        t = sum(rt.estimate_tokens(str(m.get("content", ""))) for m in msgs)
        return b, t, warns

    def normalize_messages(msgs_in: Any) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        if not isinstance(msgs_in, list):
            return out
        for m in msgs_in:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", "")).strip().lower()
            if role == "ai":
                role = "assistant"
            if role not in ("user", "assistant"):
                continue
            out.append({"role": role, "content": str(m.get("content", "") or "")})
        return out

    async def build_model_conversation(
        base_messages: List[Dict[str, str]],
        retrieval_enabled: bool,
        summary: Optional[Dict[str, Any]] = None,
        request_start_t: Optional[float] = None,
        response_language: Optional[str] = None,
        user_language: str = "en",
        request_base_url: Optional[str] = None,
    ) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
        conversation = [dict(m) for m in base_messages]
        retrieval_meta = rt._base_retrieval_meta(retrieval_enabled)
        latest_user = next((str(m.get("content", "") or "") for m in reversed(conversation) if str(m.get("role", "")).strip().lower() == "user"), "")
        query = rt.build_retrieval_query(conversation).strip() if retrieval_enabled else ""
        retrieval_meta["retrieval_query"] = query or None
        retrieval_meta["retrieval_attempted"] = bool(retrieval_enabled)
        if summary is not None:
            summary["raw_user_message"] = latest_user
            summary.setdefault("normalized_user_message", latest_user.strip())
        if summary is not None and request_start_t is not None:
            rt._trace_diagnostics(summary, request_start_t, "conversation_history_loaded", message_count=len(conversation))
        if not retrieval_enabled:
            conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
            return conversation, retrieval_meta
        if summary is not None and request_start_t is not None:
            rt._trace_diagnostics(summary, request_start_t, "rag_query_built", query_length=len(query or ""))
        if rt._should_skip_retrieval(latest_user):
            retrieval_meta["retrieval_skipped_reason"] = "skipped_non_encyclopedic_query"
            retrieval_meta["retrieval_error"] = "skipped_non_encyclopedic_query"
            retrieval_meta["no_context_answer_mode"] = True
            conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
            return conversation, retrieval_meta
        logger.debug("retrieval query len=%d", len(query))
        if not query:
            retrieval_meta["retrieval_skipped_reason"] = "missing_user_query"
            retrieval_meta["retrieval_error"] = "missing_user_query"
            retrieval_meta["no_context_answer_mode"] = True
            conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
            return conversation, retrieval_meta
        start_t = time.perf_counter()
        if summary is not None and request_start_t is not None:
            rt._trace_diagnostics(summary, request_start_t, "retrieval_started")
        try:
            work = asyncio.to_thread(rt.prepare_wiki_context, query, user_language)
            if rt.retrieval_timeout_seconds > 0:
                payload = await asyncio.wait_for(work, timeout=rt.retrieval_timeout_seconds)
            else:
                payload = await work
            context = str(payload.get("context", "") or "")
            selected_chunks = rt._decorate_chunk_list_with_citations(payload.get("selected_chunks") or [], request_base_url)
            candidate_chunks = rt._decorate_chunk_list_with_citations(payload.get("retrieval_candidates") or [], request_base_url)
            citations = rt._citations_from_chunks(selected_chunks, request_base_url)
            retrieval_meta["retrieval_ms"] = int((time.perf_counter() - start_t) * 1000)
            retrieval_meta["retrieval_candidate_count"] = int(payload.get("candidate_count") or 0)
            retrieval_meta["retrieval_count"] = len(selected_chunks)
            retrieval_meta["retrieval_context_chars"] = len(context)
            retrieval_meta["retrieval_used"] = bool(context)
            retrieval_meta["retrieved_chunks"] = selected_chunks
            retrieval_meta["retrieval_candidates"] = candidate_chunks
            retrieval_meta["citations"] = citations
            retrieval_meta["primary_citation"] = citations[0] if citations else None
            retrieval_meta["rerank_model_path"] = rt.rerank_model
            retrieval_meta["rerank_enabled"] = bool(payload.get("rerank_enabled"))
            retrieval_meta["rerank_ms"] = int(payload.get("rerank_ms") or 0)
            retrieval_meta["rerank_error"] = payload.get("rerank_error")
            retrieval_meta["chunks_after_rerank"] = int(payload.get("chunks_after_rerank") or 0)
            retrieval_meta["chunks_after_budget_trim"] = int(payload.get("chunks_after_budget_trim") or 0)
            retrieval_meta["final_context_estimated_tokens"] = int(payload.get("context_tokens_estimate") or 0)
            retrieval_meta["rag_chunk_truncation_count"] = int(payload.get("chunk_truncation_count") or 0)
            retrieval_meta["rag_index_language"] = payload.get("rag_index_language", "en")
            retrieval_meta["rag_collection_path"] = payload.get("rag_collection_path")
            retrieval_meta["index_manifest"] = payload.get("index_manifest")
            retrieval_meta["index_manifest_path"] = payload.get("index_manifest_path")
            retrieval_meta["index_manifest_error"] = payload.get("index_manifest_error")
            if summary is not None and request_start_t is not None:
                rt._trace_diagnostics(summary, request_start_t, "retrieval_completed", candidate_count=retrieval_meta["retrieval_candidate_count"], selected_count=retrieval_meta["retrieval_count"])
                rt._trace_diagnostics(summary, request_start_t, "rerank_completed", rerank_ms=retrieval_meta["rerank_ms"], rerank_enabled=retrieval_meta["rerank_enabled"])
            _rag_idx_lang = retrieval_meta.get("rag_index_language", "en")
            if context:
                retrieval_message = rt._build_retrieval_system_message(context, response_language=response_language, rag_index_language=_rag_idx_lang)
                retrieval_meta["retrieval_system_message"] = retrieval_message
                conversation = rt.inject_wiki_context(conversation, context, response_language=response_language, rag_index_language=_rag_idx_lang)
            else:
                retrieval_meta["retrieval_error"] = str(payload.get("reason") or "no_relevant_chunks")
                retrieval_meta["retrieval_skipped_reason"] = retrieval_meta["retrieval_error"]
                retrieval_meta["rag_fallback_triggered"] = True
                retrieval_meta["no_context_answer_mode"] = True
                conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
        except asyncio.TimeoutError:
            retrieval_meta["retrieval_ms"] = int((time.perf_counter() - start_t) * 1000)
            retrieval_meta["retrieval_timed_out"] = True
            retrieval_meta["retrieval_error"] = "retrieval_timeout"
            retrieval_meta["rag_fallback_triggered"] = True
            retrieval_meta["no_context_answer_mode"] = True
            conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
        except Exception as exc:
            retrieval_meta["retrieval_ms"] = int((time.perf_counter() - start_t) * 1000)
            retrieval_meta["retrieval_error"] = f"{type(exc).__name__}: {exc}"
            retrieval_meta["rag_fallback_triggered"] = True
            retrieval_meta["no_context_answer_mode"] = True
            conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
        if retrieval_meta.get("retrieval_used"):
            chunk_titles = list(dict.fromkeys(
                str(c.get("title") or "Unknown") for c in retrieval_meta.get("retrieved_chunks", [])
            ))
            lang_label = "ES index" if retrieval_meta.get("rag_index_language") == "es" else "EN index"
            retrieval_meta["rag_summary"] = f"Retrieved {retrieval_meta.get('retrieval_count', 0)} chunks from: {', '.join(chunk_titles)} [{lang_label}]"
        elif retrieval_meta.get("retrieval_skipped_reason"):
            retrieval_meta["rag_summary"] = f"Retrieval skipped: {retrieval_meta['retrieval_skipped_reason']}"
        elif retrieval_meta.get("retrieval_error"):
            retrieval_meta["rag_summary"] = f"Retrieval failed: {retrieval_meta['retrieval_error']}"
        else:
            retrieval_meta["rag_summary"] = "Retrieval not attempted"
        return conversation, retrieval_meta

    async def stream_model_text(
        model: str,
        base_messages: List[Dict[str, str]],
        summary: Dict[str, Any],
        retrieval_enabled: bool,
        response_language: Optional[str] = None,
        user_language: str = "en",
        request_base_url: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        request_start_t = float(summary.get("_request_start_perf") or time.perf_counter())
        summary.setdefault("stage_trace", [])
        summary.setdefault("request_started_at", rt.now_iso())
        summary["model_name"] = model
        summary["stream"] = True
        summary.setdefault("base_messages", rt._clone_data(base_messages))
        summary.setdefault("resource_start", rt._resource_snapshot())
        if retrieval_enabled:
            conversation, retrieval_meta = await build_model_conversation(
                base_messages,
                retrieval_enabled,
                summary,
                request_start_t,
                response_language=response_language,
                user_language=user_language,
                request_base_url=request_base_url,
            )
        else:
            conversation = [dict(m) for m in base_messages]
            conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
            retrieval_meta = rt._base_retrieval_meta(False)
            rt._trace_diagnostics(summary, request_start_t, "conversation_history_loaded", message_count=len(conversation))
        retrieval_meta["request_prep_ms"] = int((time.perf_counter() - request_start_t) * 1000)
        if not conversation:
            raise HTTPException(400, "Missing messages")
        summary["final_conversation"] = rt._clone_data(conversation)
        summary["retrieval_system_message"] = retrieval_meta.get("retrieval_system_message")
        summary.update(retrieval_meta)
        rt._trace_diagnostics(summary, request_start_t, "final_prompt_assembled", message_count=len(conversation))

        model_start_t = time.perf_counter()
        ttft_ms: Optional[int] = None
        upstream_open_ms: Optional[int] = None
        max_token_gap_ms = 0
        continuation_gap_ms = 0
        last_token_t: Optional[float] = None
        last_token_at: Optional[str] = None
        pending_continuation_started_at: Optional[float] = None
        continuation_count = 0
        final_finish = "stop"
        limit_hit = False
        text_out = ""
        prompt_tokens_est = sum(rt.estimate_tokens(str(m.get("content", ""))) for m in conversation)
        usage: Dict[str, Any] = {}
        first_stream_chunk_ms: Optional[int] = None
        # Granular timeouts: a fast dial + an unhurried read window. The old
        # flat 180 s applied to every phase, which let a stuck connect block
        # for three minutes. read=180 s preserves the existing tolerance for
        # very long completions; connect=5 s and pool=10 s surface upstream
        # outages immediately instead of hanging the request.
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=10.0)) as client:
            initial_request_logged = False
            while True:
                req_body = {"model": model, "messages": conversation, "stream": True}
                if not initial_request_logged:
                    summary["upstream_request_body"] = rt._clone_data(req_body)
                    initial_request_logged = True
                segment = ""
                finish_reason = "stop"
                open_start_t = time.perf_counter()
                rt._trace_diagnostics(summary, request_start_t, "generation_started")
                async with client.stream("POST", f"{rt.llama_base_url}/v1/chat/completions", json=req_body) as resp:
                    if upstream_open_ms is None:
                        upstream_open_ms = int((time.perf_counter() - open_start_t) * 1000)
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="ignore")
                        raise HTTPException(resp.status_code, body or "Model request failed")
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except Exception:
                            continue
                        choice = ((obj.get("choices") or [{}])[0]) if isinstance(obj, dict) else {}
                        delta = choice.get("delta") or {}
                        part = str(delta.get("content", "") or "")
                        if part:
                            now_t = time.perf_counter()
                            if ttft_ms is None:
                                ttft_ms = int((now_t - model_start_t) * 1000)
                                first_stream_chunk_ms = ttft_ms
                                rt._trace_diagnostics(summary, request_start_t, "first_token_emitted")
                            if pending_continuation_started_at is not None:
                                continuation_gap_ms = max(
                                    continuation_gap_ms,
                                    int((now_t - pending_continuation_started_at) * 1000),
                                )
                                pending_continuation_started_at = None
                            if last_token_t is not None:
                                max_token_gap_ms = max(max_token_gap_ms, int((now_t - last_token_t) * 1000))
                            last_token_t = now_t
                            last_token_at = rt.now_iso()
                            segment += part
                            text_out += part
                            yield part
                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = str(fr)
                        if isinstance(obj.get("usage"), dict):
                            usage = obj.get("usage")
                final_finish = finish_reason or final_finish
                conversation.append({"role": "assistant", "content": segment})
                truncated = final_finish in ("length", "max_tokens")
                if truncated and continuation_count < rt.chat_continuation_limit and segment.strip():
                    continuation_count += 1
                    conversation.append({"role": "user", "content": rt.chat_continue_prompt})
                    pending_continuation_started_at = time.perf_counter()
                    continue
                limit_hit = bool(truncated and continuation_count >= rt.chat_continuation_limit)
                break
        total_ms = int((time.perf_counter() - request_start_t) * 1000)
        generation_ms = max(1, total_ms - int(retrieval_meta.get("request_prep_ms") or 0) - (ttft_ms or 0))
        completion_tokens_est = rt.estimate_tokens(text_out)
        tps = round(completion_tokens_est / max(generation_ms / 1000.0, 0.001), 4)
        summary.update(
            {
                "text": text_out,
                "finish_reason": final_finish,
                "continuation_count": continuation_count,
                "continuation_limit_hit": limit_hit,
                "ttft_ms": ttft_ms,
                "first_stream_chunk_ms": first_stream_chunk_ms,
                "upstream_open_ms": int(upstream_open_ms or 0),
                "max_token_gap_ms": max_token_gap_ms,
                "continuation_gap_ms": continuation_gap_ms,
                "last_token_at": last_token_at,
                "total_ms": total_ms,
                "generation_ms": generation_ms,
                "prompt_tokens": int(usage.get("prompt_tokens") or prompt_tokens_est),
                "completion_tokens": int(usage.get("completion_tokens") or completion_tokens_est),
                "total_tokens": int(usage.get("total_tokens") or (prompt_tokens_est + completion_tokens_est)),
                "tps": tps,
                "prompt_tokens_estimate": int(usage.get("prompt_tokens") or prompt_tokens_est),
                "completion_tokens_estimate": int(usage.get("completion_tokens") or completion_tokens_est),
                "total_tokens_estimate": int(usage.get("total_tokens") or (prompt_tokens_est + completion_tokens_est)),
                "tps_estimate": tps,
                "request_finished_at": rt.now_iso(),
                "resource_end": rt._resource_snapshot(),
                "runtime_warning_text": None,
            }
        )
        summary.update(retrieval_meta)
        rt._trace_diagnostics(summary, request_start_t, "generation_completed", total_ms=total_ms)

    @app.get("/v1/app/docs")
    def docs_list(req: Request, include_deleted: bool = False):
        with rt.tx() as c:
            u = rt.req_user(c, req)
            where = "user_id=?"
            args: Tuple[Any, ...] = (u["id"],)
            if not include_deleted: where += " AND is_deleted=0"
            rows = c.execute(f"SELECT id,user_id,title,type,created_at,updated_at,last_accessed_at,size_bytes,is_starred,is_deleted,deleted_at FROM documents WHERE {where} ORDER BY COALESCE(last_accessed_at,updated_at,created_at) DESC", args).fetchall()
            return {"ok": True, "documents": [dict(r) for r in rows], "warning": rt.storage_warning()}

    @app.post("/v1/app/docs")
    def docs_create(p: CreateDocPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True); rt.ensure_docs_write_access(c, u); rt.ensure_user_dirs(u["id"])
            did = rt.uid(); title = (p.title or "Untitled Document").strip() or "Untitled Document"; typ = (p.type or "markdown").strip().lower() or "markdown"; typ = typ if typ in ("markdown", "json") else "markdown"
            obj = {"id": did, "title": title, "content_markdown": rt.sanitize_markdown(p.content_markdown), "created_at": rt.now_iso(), "updated_at": rt.now_iso(), "version": 1}
            size_est = len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
            warns = rt.doc_limits(c, u, req, size_est, create=True)
            rt.ensure_capacity(size_est, "doc_create", c)
            fp = rt.doc_rel(u["id"], did); b = write_payload(c, u, req, rt.safe_path(fp, u["id"]), obj, "doc_create")
            c.execute("INSERT INTO documents(id,user_id,title,type,created_at,updated_at,last_accessed_at,size_bytes,file_path,is_starred,is_deleted,deleted_at,deleted_by_user,is_guest_owned) VALUES(?,?,?,?,?,?,?,?,?,0,0,NULL,0,?)", (did, u["id"], title, typ, rt.now_iso(), rt.now_iso(), rt.now_iso(), int(b), fp, 1 if u["role"] == "guest" else 0))
            rt.rate_add(c, u["id"], "doc_create", 0); rt.rate_add(c, u["id"], "doc_write", int(b)); rt.recalc_storage(c, u["id"])
            rt.record_analytics_event(c, event_type="documents_created", surface="docs", user=u, metadata={"document_id": did, "type": typ, "size_bytes": int(b)})
            sw = rt.storage_warning()
            if sw: warns.append(sw)
            return {"ok": True, "document": {"id": did, "title": title, "type": typ, "updated_at": rt.now_iso(), "size_bytes": int(b), "is_starred": False}, "warnings": warns}

    @app.get("/v1/app/docs/{did}")
    def docs_get(did: str, req: Request, include_deleted: bool = False):
        with rt.tx() as c:
            u = rt.req_user(c, req)
            r = get_doc(c, u, did, include_deleted=include_deleted)
            pth = rt.safe_path(r["file_path"], r["user_id"])
            body = rt.read_json(pth) if pth.exists() else {}
            c.execute("UPDATE documents SET last_accessed_at=? WHERE id=?", (rt.now_iso(), did))
            if int(r["is_deleted"] or 0) == 0:
                rt.record_analytics_event(c, event_type="documents_opened", surface="docs", user=u, metadata={"document_id": did, "type": r["type"]})
            return {"ok": True, "document": {"id": r["id"], "title": r["title"], "type": r["type"], "content_markdown": body.get("content_markdown", ""), "created_at": r["created_at"], "updated_at": r["updated_at"], "is_starred": bool(r["is_starred"]), "is_deleted": bool(r["is_deleted"])}}

    @app.patch("/v1/app/docs/{did}")
    def docs_update(did: str, p: UpdateDocPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True); rt.ensure_docs_write_access(c, u)
            r = get_doc(c, u, did)
            path = rt.safe_path(r["file_path"], r["user_id"])
            cur = rt.read_json(path) if path.exists() else {"id": r["id"], "title": r["title"], "content_markdown": "", "created_at": r["created_at"], "updated_at": r["updated_at"], "version": 1}
            if p.title is not None: cur["title"] = p.title.strip() or "Untitled Document"
            if p.content_markdown is not None: cur["content_markdown"] = rt.sanitize_markdown(p.content_markdown)
            cur["updated_at"] = rt.now_iso(); cur["version"] = int(cur.get("version", 0)) + 1
            size_est = len(json.dumps(cur, ensure_ascii=False).encode("utf-8"))
            warns = rt.doc_limits(c, u, req, size_est, create=False)
            rt.ensure_capacity(size_est, "doc_update", c)
            b = write_payload(c, u, req, path, cur, "doc_update")
            next_type = (p.type or r["type"] or "markdown").strip().lower(); next_type = next_type if next_type in ("markdown", "json") else "markdown"
            c.execute("UPDATE documents SET title=?, type=?, updated_at=?, last_accessed_at=?, size_bytes=? WHERE id=?", (cur["title"], next_type, rt.now_iso(), rt.now_iso(), int(b), did))
            rt.rate_add(c, u["id"], "doc_write", int(b)); rt.recalc_storage(c, r["user_id"])
            rt.record_analytics_event(c, event_type="documents_updated", surface="docs", user=u, metadata={"document_id": did, "type": next_type, "size_bytes": int(b)})
            sw = rt.storage_warning()
            if sw: warns.append(sw)
            return {"ok": True, "document": {"id": did, "title": cur["title"], "updated_at": rt.now_iso(), "size_bytes": int(b)}, "warnings": warns}

    @app.post("/v1/app/docs/{did}/star")
    def docs_star(did: str, p: StarDocPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True); rt.ensure_docs_write_access(c, u)
            r = get_doc(c, u, did)
            c.execute("UPDATE documents SET is_starred=?, updated_at=? WHERE id=?", (1 if p.starred else 0, rt.now_iso(), r["id"]))
            rt.record_analytics_event(c, event_type="documents_starred" if p.starred else "documents_unstarred", surface="docs", user=u, metadata={"document_id": did})
        return {"ok": True, "starred": bool(p.starred)}

    @app.post("/v1/app/docs/trash/clear")
    def docs_trash_clear(req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True); rt.ensure_docs_write_access(c, u)
            rows = c.execute("SELECT * FROM documents WHERE user_id=? AND is_deleted=1 ORDER BY deleted_at ASC, updated_at ASC", (u["id"],)).fetchall()
            reclaimed = 0
            deleted_count = 0
            for row in rows:
                b, n = rt.hard_del_doc(c, row)
                reclaimed += int(b)
                deleted_count += int(n)
            rt.recalc_storage(c, u["id"])
            if deleted_count:
                rt.record_analytics_event(c, event_type="documents_trash_cleared", surface="docs", user=u, value=deleted_count, metadata={"deleted_count": deleted_count, "reclaimed_bytes": reclaimed})
        return {"ok": True, "deleted_count": deleted_count, "reclaimed_bytes": reclaimed}

    @app.delete("/v1/app/docs/{did}")
    def docs_delete(did: str, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True); rt.ensure_docs_write_access(c, u)
            r = get_doc(c, u, did)
            if int(r["is_starred"]) == 1:
                raise HTTPException(409, "Starred documents must be unstarred before deletion.")
            src = rt.safe_path(r["file_path"], r["user_id"]); tr = rt.trash_rel(r["user_id"], "docs", r["id"]); dst = rt.safe_path(tr, r["user_id"])
            if src.exists(): dst.parent.mkdir(parents=True, exist_ok=True); os.replace(str(src), str(dst))
            c.execute("UPDATE documents SET file_path=?, is_deleted=1, deleted_at=?, deleted_by_user=1, updated_at=? WHERE id=?", (tr, rt.now_iso(), rt.now_iso(), did)); rt.recalc_storage(c, r["user_id"])
            rt.record_analytics_event(c, event_type="documents_deleted", surface="docs", user=u, metadata={"document_id": did, "type": r["type"]})
        return {"ok": True}

    @app.post("/v1/app/docs/{did}/restore")
    def docs_restore(did: str, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True); rt.ensure_docs_write_access(c, u)
            r = get_doc(c, u, did, include_deleted=True)
            if int(r["is_deleted"]) == 0: return {"ok": True}
            src = rt.safe_path(r["file_path"], r["user_id"]); lv = rt.doc_rel(r["user_id"], r["id"]); dst = rt.safe_path(lv, r["user_id"])
            if src.exists(): dst.parent.mkdir(parents=True, exist_ok=True); os.replace(str(src), str(dst))
            c.execute("UPDATE documents SET file_path=?, is_deleted=0, deleted_at=NULL, deleted_by_user=0, updated_at=? WHERE id=?", (lv, rt.now_iso(), did)); rt.recalc_storage(c, r["user_id"])
            rt.record_analytics_event(c, event_type="documents_restored", surface="docs", user=u, metadata={"document_id": did, "type": r["type"]})
        return {"ok": True}

    @app.post("/v1/app/docs/report-paste-abuse")
    def docs_report_paste_abuse(p: PasteAbusePayload, req: Request):
        valid_types = {"paste_cooldown", "paste_duplicate", "paste_too_long"}
        if (p.abuse_type or "").strip() not in valid_types:
            raise HTTPException(400, "Invalid abuse_type")
        et_map = {
            "paste_cooldown": "docs_paste_cooldown_blocked",
            "paste_duplicate": "docs_paste_duplicate_blocked",
            "paste_too_long": "docs_paste_too_long",
        }
        et = et_map[p.abuse_type.strip()]
        with rt.tx() as c:
            u = rt.req_user(c, req)
            rt.log_event(c, user=u, ip_=rt.ip(req), endpoint=req.url.path,
                et=et, sev="warn",
                detail=p.detail or f"Paste abuse: {p.abuse_type}",
                act="client_reported")
            rt._check_flag_escalation(c, u, rt.ip(req), req.url.path, et,
                rt.docs_offense_window, rt.docs_offense_hits, "docs")
        return {"ok": True}

    def serialize_chat_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_accessed_at": row["last_accessed_at"],
            "token_count_estimate": row["token_count_estimate"],
            "size_bytes": row["size_bytes"],
            "is_deleted": bool(row["is_deleted"]),
            "deleted_at": row["deleted_at"],
            "is_saved": bool(row["is_saved"]),
            "folder_id": row["folder_id"],
        }

    def serialize_chat_folder_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @app.get("/v1/app/chat-folders")
    def chat_folders_list(req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req)
            rows = c.execute("SELECT * FROM chat_folders WHERE user_id=? ORDER BY LOWER(name) ASC, created_at ASC", (u["id"],)).fetchall()
            return {"ok": True, "folders": [serialize_chat_folder_row(r) for r in rows]}

    @app.post("/v1/app/chat-folders")
    def chat_folders_create(p: CreateChatFolderPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            name = rt.clean_chat_folder_name(p.name)
            if not name:
                raise HTTPException(400, "Folder name is required")
            name_norm = rt.nuser(name)
            existing = c.execute("SELECT id FROM chat_folders WHERE user_id=? AND name_norm=?", (u["id"], name_norm)).fetchone()
            if existing is not None:
                raise HTTPException(409, "Folder name already exists")
            fid = rt.uid()
            now = rt.now_iso()
            c.execute(
                "INSERT INTO chat_folders(id,user_id,name,name_norm,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (fid, u["id"], name, name_norm, now, now),
            )
            row = c.execute("SELECT * FROM chat_folders WHERE id=?", (fid,)).fetchone()
            rt.record_analytics_event(c, event_type="folders_created", surface="chat", user=u, metadata={"folder_id": fid})
            return {"ok": True, "folder": serialize_chat_folder_row(row)}

    @app.patch("/v1/app/chat-folders/{fid}")
    def chat_folders_update(fid: str, p: UpdateChatFolderPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            folder = rt.get_chat_folder_row(c, u["id"], fid)
            name = rt.clean_chat_folder_name(p.name)
            if not name:
                raise HTTPException(400, "Folder name is required")
            name_norm = rt.nuser(name)
            existing = c.execute("SELECT id FROM chat_folders WHERE user_id=? AND name_norm=? AND id<>?", (u["id"], name_norm, fid)).fetchone()
            if existing is not None:
                raise HTTPException(409, "Folder name already exists")
            c.execute("UPDATE chat_folders SET name=?, name_norm=?, updated_at=? WHERE id=?", (name, name_norm, rt.now_iso(), folder["id"]))
            row = c.execute("SELECT * FROM chat_folders WHERE id=?", (fid,)).fetchone()
            rt.record_analytics_event(c, event_type="folders_renamed", surface="chat", user=u, metadata={"folder_id": fid})
            return {"ok": True, "folder": serialize_chat_folder_row(row)}

    @app.delete("/v1/app/chat-folders/{fid}")
    def chat_folders_delete(fid: str, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            folder = rt.get_chat_folder_row(c, u["id"], fid)
            now = rt.now_iso()
            c.execute("UPDATE chats SET folder_id=NULL, updated_at=? WHERE user_id=? AND folder_id=?", (now, u["id"], folder["id"]))
            c.execute("DELETE FROM chat_folders WHERE id=?", (folder["id"],))
            rt.record_analytics_event(c, event_type="folders_deleted", surface="chat", user=u, metadata={"folder_id": fid})
        return {"ok": True}

    @app.get("/v1/app/chats")
    def chats_list(req: Request, include_deleted: bool = False):
        with rt.tx() as c:
            u = rt.req_user(c, req)
            where = "user_id=?"
            args: Tuple[Any, ...] = (u["id"],)
            if not include_deleted:
                where += " AND is_deleted=0"
            rows = c.execute(
                f"SELECT id,user_id,title,created_at,updated_at,last_accessed_at,token_count_estimate,size_bytes,is_deleted,deleted_at,is_saved,folder_id FROM chats WHERE {where} ORDER BY COALESCE(last_accessed_at,updated_at,created_at) DESC",
                args,
            ).fetchall()
            return {"ok": True, "chats": [serialize_chat_row(r) for r in rows], "warning": rt.storage_warning()}

    @app.get("/v1/app/chats/{cid}")
    def chats_get(cid: str, req: Request, include_deleted: bool = False):
        with rt.tx() as c:
            u = rt.req_user(c, req)
            r = get_chat(c, u, cid, include_deleted=include_deleted)
            o = load_chat_json(r)
            if int(r["is_deleted"] or 0) == 0:
                c.execute("UPDATE chats SET last_accessed_at=? WHERE id=?", (rt.now_iso(), cid))
                rt.record_analytics_event(c, event_type="chat_opened", surface="chat", user=u, metadata={"chat_id": cid})
            return {
                "ok": True,
                "chat": {
                    "id": r["id"],
                    "title": r["title"],
                    "messages": o.get("messages", []),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "is_deleted": bool(r["is_deleted"]),
                    "is_saved": bool(r["is_saved"]),
                    "folder_id": r["folder_id"],
                },
            }

    @app.post("/v1/app/chats")
    def chats_create(p: CreateChatPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            rt.ensure_user_dirs(u["id"])
            cid = rt.uid()
            title = (p.title or "New Chat").strip() or "New Chat"
            now = rt.now_iso()
            o = {"id": cid, "title": title, "messages": [], "created_at": now, "updated_at": now, "version": 1}
            size_est = len(json.dumps(o, ensure_ascii=False).encode("utf-8"))
            warns = rt.chat_limits(c, u, req, size_est, create=True, scope="chat")
            rt.ensure_capacity(size_est, "chat_create", c)
            fp = rt.chat_rel(u["id"], cid)
            b = write_payload(c, u, req, rt.safe_path(fp, u["id"]), o, "chat_create")
            c.execute(
                "INSERT INTO chats(id,user_id,title,created_at,updated_at,last_accessed_at,token_count_estimate,file_path,size_bytes,is_deleted,deleted_at,deleted_by_user,is_guest_owned,is_saved,folder_id) VALUES(?,?,?,?,?,?,0,?,?,0,NULL,0,?,0,NULL)",
                (cid, u["id"], title, now, now, now, fp, int(b), 1 if u["role"] == "guest" else 0),
            )
            rt.rate_add(c, u["id"], "chat_create", 0)
            rt.rate_add(c, u["id"], "chat_write", int(b))
            rt.recalc_storage(c, u["id"])
            rt.record_analytics_event(c, event_type="chat_sessions_created", surface="chat", user=u, metadata={"chat_id": cid})
            sw = rt.storage_warning()
            if sw:
                warns.append(sw)
            row = c.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
            return {"ok": True, "chat": serialize_chat_row(row), "warnings": warns}

    @app.patch("/v1/app/chats/{cid}")
    def chats_update(cid: str, p: UpdateChatPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            r = get_chat(c, u, cid, include_deleted=True)
            fields_set = set(getattr(p, "model_fields_set", getattr(p, "__fields_set__", set())))
            updates: List[str] = []
            args: List[Any] = []
            if "title" in fields_set:
                updates.append("title=?")
                args.append((p.title or "").strip() or "Untitled Chat")
            if "is_saved" in fields_set:
                next_saved = bool(p.is_saved)
                if next_saved and not bool(r["is_saved"]):
                    rt.ensure_saved_chat_capacity(c, u["id"], exclude_chat_id=r["id"])
                updates.append("is_saved=?")
                args.append(1 if next_saved else 0)
            if "folder_id" in fields_set:
                updates.append("folder_id=?")
                args.append(rt.resolve_chat_folder_id(c, u["id"], p.folder_id))
            if updates:
                updates.append("updated_at=?")
                args.append(rt.now_iso())
                args.append(r["id"])
                c.execute(f"UPDATE chats SET {', '.join(updates)} WHERE id=?", tuple(args))
            row = c.execute("SELECT * FROM chats WHERE id=?", (r["id"],)).fetchone()
            return {"ok": True, "chat": serialize_chat_row(row)}

    @app.delete("/v1/app/chats/{cid}")
    def chats_delete(cid: str, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            r = get_chat(c, u, cid)
            src = rt.safe_path(r["file_path"], r["user_id"])
            tr = rt.trash_rel(r["user_id"], "chats", r["id"])
            dst = rt.safe_path(tr, r["user_id"])
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(src), str(dst))
            c.execute("UPDATE chats SET file_path=?, is_deleted=1, deleted_at=?, deleted_by_user=1, updated_at=? WHERE id=?", (tr, rt.now_iso(), rt.now_iso(), cid))
            rt.recalc_storage(c, r["user_id"])
            rt.record_analytics_event(c, event_type="chat_deleted", surface="chat", user=u, metadata={"chat_id": cid})
        return {"ok": True}

    @app.post("/v1/app/chats/{cid}/restore")
    def chats_restore(cid: str, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            r = get_chat(c, u, cid, include_deleted=True)
            if int(r["is_deleted"]) == 0:
                row = c.execute("SELECT * FROM chats WHERE id=?", (r["id"],)).fetchone()
                return {"ok": True, "chat": serialize_chat_row(row)}
            folder_id = None
            if r["folder_id"] is not None:
                folder = c.execute("SELECT id FROM chat_folders WHERE user_id=? AND id=?", (r["user_id"], r["folder_id"])).fetchone()
                folder_id = folder["id"] if folder is not None else None
            if bool(r["is_saved"]):
                rt.ensure_saved_chat_capacity(c, u["id"])
            src = rt.safe_path(r["file_path"], r["user_id"])
            lv = rt.chat_rel(r["user_id"], r["id"])
            dst = rt.safe_path(lv, r["user_id"])
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(src), str(dst))
            now = rt.now_iso()
            c.execute(
                "UPDATE chats SET file_path=?, is_deleted=0, deleted_at=NULL, deleted_by_user=0, folder_id=?, updated_at=?, last_accessed_at=? WHERE id=?",
                (lv, folder_id, now, now, cid),
            )
            rt.recalc_storage(c, r["user_id"])
            row = c.execute("SELECT * FROM chats WHERE id=?", (r["id"],)).fetchone()
            rt.record_analytics_event(c, event_type="chat_restored", surface="chat", user=u, metadata={"chat_id": cid})
            return {"ok": True, "chat": serialize_chat_row(row)}

    @app.post("/v1/app/chats/trash/clear")
    def chats_trash_clear(req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            rows = c.execute("SELECT * FROM chats WHERE user_id=? AND is_deleted=1 ORDER BY deleted_at ASC, updated_at ASC", (u["id"],)).fetchall()
            reclaimed = 0
            deleted_count = 0
            for row in rows:
                b, n = rt.hard_del_chat(c, row)
                reclaimed += int(b)
                deleted_count += int(n)
            rt.recalc_storage(c, u["id"])
            if deleted_count:
                rt.record_analytics_event(c, event_type="chat_deleted", surface="chat", user=u, value=deleted_count, metadata={"deleted_count": deleted_count, "reclaimed_bytes": reclaimed})
        return {"ok": True, "deleted_count": deleted_count, "reclaimed_bytes": reclaimed}

    @app.post("/v1/app/chat/completions")
    async def app_chat_completions(req: Request):
        """Handle the main signed-in chat route backed by llama.cpp and optional wiki RAG."""
        request_id, cold_request = rt._next_request_identity()
        request_start_perf = time.perf_counter()
        request_started_at = rt.now_iso()
        try:
            payload = await req.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        if not isinstance(payload, dict): raise HTTPException(400, "Invalid payload")
        msgs = payload.get("messages")
        model = str(payload.get("model") or "").strip()
        if not model:
            raise HTTPException(400, "Missing model")
        norm_messages = normalize_messages(msgs)
        if not norm_messages:
            raise HTTPException(400, "Missing messages")
        user_msg = ""
        for m in reversed(norm_messages):
            if m.get("role") == "user": user_msg = str(m.get("content", "") or ""); break
        if not user_msg.strip(): raise HTTPException(400, "No user message found")

        stream = bool(payload.get("stream", False))
        retrieval_enabled = _coerce_bool(payload.get("retrieval_enabled"), default=rt.retrieval_enabled_default)
        chat_id = payload.get("chat_id")
        title = str(payload.get("title") or user_msg[:48] or "New Chat")[:120]
        warns: List[Dict[str, Any]] = []
        user_role = "user"
        preferred_language = "es"
        response_language = "es"
        analytics_user: Optional[Dict[str, Any]] = None
        summary_base: Dict[str, Any] = {
            "request_id": request_id,
            "request_started_at": request_started_at,
            "request_finished_at": None,
            "_request_start_perf": request_start_perf,
            "stage_trace": [],
            "model_name": model,
            "stream": stream,
            "retrieval_enabled": retrieval_enabled,
            "cold_request": cold_request,
            "raw_user_message": user_msg,
            "normalized_user_message": user_msg.strip(),
            "request_base_url": rt._request_base_url(req),
            "base_messages": rt._clone_data(norm_messages),
            "resource_start": rt._resource_snapshot(),
            "warnings": warns,
        }
        rt._trace_diagnostics(summary_base, request_start_perf, "request_received")
        rt._trace_diagnostics(summary_base, request_start_perf, "normalized_messages_prepared", message_count=len(norm_messages))

        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            user_role = str(u["role"] or "user")
            analytics_user = {
                "id": u["id"],
                "username": u["username"],
                "role": u["role"],
                "preferred_language": u["preferred_language"],
            }
            try:
                preferred_language = str(u["preferred_language"] or "es").strip().lower()
            except (KeyError, IndexError):
                preferred_language = "es"
            # Detect query language; fall back to user's preferred language
            detected_lang = rt._detect_query_language(user_msg)
            response_language = detected_lang if detected_lang in ("en", "es") else preferred_language
            # For ambiguous queries (short, no clear language signal), use preference
            lang_words = set(user_msg.lower().split())
            _SPANISH_SIGNAL = {"que", "quien", "como", "donde", "cuando", "por", "cual", "es", "la", "el", "los", "las", "del", "una", "en"}
            _ENGLISH_SIGNAL = {"what", "who", "how", "where", "when", "which", "the", "is", "are", "was", "does", "did"}
            has_spanish = bool(lang_words & _SPANISH_SIGNAL)
            has_english = bool(lang_words & _ENGLISH_SIGNAL)
            if not has_spanish and not has_english:
                response_language = preferred_language
            summary_base["user_role"] = user_role
            summary_base["user_id"] = str(u["id"])
            summary_base["response_language"] = response_language
            rt.ensure_ai_send_access(c, u)
            rt.check_ai_request_rate(c, u, rt.ip(req), req.url.path)
            rt.check_prompt_length(c, u, rt.ip(req), req.url.path, user_msg)
            rt.check_concurrent_generations(c, u, rt.ip(req), req.url.path, request_id)
            rt.arm_ai_cooldown_if_needed(c, u)
            if chat_id:
                ch = get_chat(c, u, str(chat_id))
            else:
                rt.ensure_user_dirs(u["id"]); cid = rt.uid(); fp = rt.chat_rel(u["id"], cid)
                o = {"id": cid, "title": title, "messages": [], "created_at": rt.now_iso(), "updated_at": rt.now_iso(), "version": 1}
                init_size = len(json.dumps(o, ensure_ascii=False).encode("utf-8"))
                warns.extend(rt.chat_limits(c, u, req, init_size, create=True, scope="ai"))
                rt.ensure_capacity(init_size, "chat_create", c)
                b = write_payload(c, u, req, rt.safe_path(fp, u["id"]), o, "chat_create")
                c.execute("INSERT INTO chats(id,user_id,title,created_at,updated_at,last_accessed_at,token_count_estimate,file_path,size_bytes,is_deleted,deleted_at,deleted_by_user,is_guest_owned,is_saved,folder_id) VALUES(?,?,?,?,?,?,0,?,?,0,NULL,0,?,0,NULL)", (cid, u["id"], title, rt.now_iso(), rt.now_iso(), rt.now_iso(), fp, int(b), 1 if u["role"] == "guest" else 0))
                rt.rate_add(c, u["id"], "chat_create", 0); rt.rate_add(c, u["id"], "chat_write", int(b)); chat_id = cid
                ch = c.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
                rt.record_analytics_event(c, event_type="chat_sessions_created", surface="chat", user=u, metadata={"chat_id": cid, "source": "completion"})
            b1, tk1, ww = append_chat(c, ch, "user", user_msg, u, req, "chat_append_user", scope="ai")
            warns.extend(ww)
            c.execute("UPDATE chats SET updated_at=?, last_accessed_at=?, size_bytes=?, token_count_estimate=? WHERE id=?", (rt.now_iso(), rt.now_iso(), int(b1), int(tk1), ch["id"]))
            rt.rate_add(c, u["id"], "chat_write", int(b1)); rt.recalc_storage(c, ch["user_id"])
            rt.record_analytics_event(c, event_type="chat_completion_requested", surface="chat", user=u, metadata={"chat_id": str(chat_id), "model": model, "retrieval_enabled": retrieval_enabled, "response_language": response_language})
            rt.record_analytics_event(c, event_type="chat_messages_sent", surface="chat", user=u, metadata={"chat_id": str(chat_id), "message_chars": len(user_msg), "response_language": response_language})
        summary_base["chat_id"] = chat_id
        rt._trace_diagnostics(summary_base, request_start_perf, "conversation_history_loaded", chat_id=chat_id)

        async def persist_ai(ai_text: str, summary: Dict[str, Any]) -> List[Dict[str, Any]]:
            add_warnings: List[Dict[str, Any]] = []
            if not ai_text:
                return add_warnings
            with rt.tx() as c:
                u2 = rt.req_user(c, req, write=True)
                ch2 = get_chat(c, u2, str(chat_id))
                b2, tk2, ww2 = append_chat(
                    c,
                    ch2,
                    "assistant",
                    ai_text,
                    u2,
                    req,
                    "chat_append_ai",
                    scope="ai",
                    citations=summary.get("citations"),
                )
                add_warnings.extend(ww2)
                c.execute("UPDATE chats SET updated_at=?, last_accessed_at=?, size_bytes=?, token_count_estimate=? WHERE id=?", (rt.now_iso(), rt.now_iso(), int(b2), int(tk2), ch2["id"]))
                rt.rate_add(c, u2["id"], "chat_write", int(b2)); rt.recalc_storage(c, ch2["user_id"])
                citations = summary.get("citations") or []
                rt.record_analytics_event(
                    c,
                    event_type="chat_completion_succeeded",
                    surface="chat",
                    user=u2,
                    metadata={
                        "chat_id": str(chat_id),
                        "model": model,
                        "response_language": response_language,
                        "completion_tokens": int(summary.get("completion_tokens") or 0),
                        "citation_count": len(citations),
                    },
                )
                if int(summary.get("completion_tokens") or 0) > 0:
                    rt.record_analytics_event(
                        c,
                        event_type="chat_completion_tokens_emitted",
                        surface="chat",
                        user=u2,
                        value=int(summary.get("completion_tokens") or 0),
                        metadata={"chat_id": str(chat_id), "model": model},
                    )
                if citations:
                    rt.record_analytics_event(
                        c,
                        event_type="chat_citations_emitted",
                        surface="chat",
                        user=u2,
                        value=len(citations),
                        metadata={"chat_id": str(chat_id), "model": model},
                    )
            rt._trace_diagnostics(summary, request_start_perf, "response_persisted_completed")
            return add_warnings

        if stream:
            async def sse() -> AsyncGenerator[str, None]:
                summary: Dict[str, Any] = dict(summary_base)
                yield f"event: meta\ndata: {json.dumps({'chat_id': chat_id, 'request_id': request_id}, ensure_ascii=False)}\n\n"
                try:
                    async for part in stream_model_text(
                        model,
                        norm_messages,
                        summary,
                        retrieval_enabled,
                        response_language=response_language,
                        user_language=preferred_language,
                        request_base_url=summary.get("request_base_url"),
                    ):
                        yield f"event: delta\ndata: {json.dumps({'delta': part}, ensure_ascii=False)}\n\n"
                except asyncio.CancelledError:
                    summary["request_finished_at"] = rt.now_iso()
                    if analytics_user is not None:
                        with rt.tx() as analytics_conn:
                            rt.record_analytics_event(
                                analytics_conn,
                                event_type="chat_completion_stopped",
                                surface="chat",
                                user=analytics_user,
                                metadata={"chat_id": str(chat_id), "model": model, "source": "disconnect"},
                            )
                    rt.remove_active_generation(request_id)
                    return
                except HTTPException as e:
                    summary["failed_stage"] = summary.get("current_stage") or "generation"
                    summary["request_error"] = str(e.detail)
                    summary["request_finished_at"] = rt.now_iso()
                    summary["warnings"] = list(warns)
                    if analytics_user is not None:
                        with rt.tx() as analytics_conn:
                            rt.record_analytics_event(
                                analytics_conn,
                                event_type="chat_completion_failed",
                                surface="chat",
                                user=analytics_user,
                                metadata={"chat_id": str(chat_id), "model": model, "detail": str(e.detail), "status_code": int(e.status_code)},
                            )
                    if user_role == "admin":
                        metrics = rt.build_admin_metrics(summary)
                        diagnostics = rt.build_admin_diagnostics(summary)
                        yield f"event: diagnostics\ndata: {json.dumps({'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                        yield f"event: error\ndata: {json.dumps({'detail': e.detail, 'metrics': metrics, 'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                        yield f"event: done\ndata: {json.dumps({'chat_id': chat_id, 'request_id': request_id, 'metrics': metrics, 'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                    else:
                        yield f"event: error\ndata: {json.dumps({'detail': e.detail}, ensure_ascii=False)}\n\n"
                        yield "event: done\ndata: {}\n\n"
                    rt.remove_active_generation(request_id)
                    return
                except Exception as e:
                    detail = f'llama proxy error: {type(e).__name__}: {e}'
                    logger.error("chat completion unexpected error request_id=%s: %s", request_id, detail, exc_info=True)
                    summary["failed_stage"] = summary.get("current_stage") or "generation"
                    summary["request_error"] = detail
                    summary["request_finished_at"] = rt.now_iso()
                    summary["warnings"] = list(warns)
                    if analytics_user is not None:
                        with rt.tx() as analytics_conn:
                            rt.record_analytics_event(
                                analytics_conn,
                                event_type="chat_completion_failed",
                                surface="chat",
                                user=analytics_user,
                                metadata={"chat_id": str(chat_id), "model": model, "detail": detail},
                            )
                    if user_role == "admin":
                        metrics = rt.build_admin_metrics(summary)
                        diagnostics = rt.build_admin_diagnostics(summary)
                        yield f"event: diagnostics\ndata: {json.dumps({'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                        yield f"event: error\ndata: {json.dumps({'detail': detail, 'metrics': metrics, 'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                        yield f"event: done\ndata: {json.dumps({'chat_id': chat_id, 'request_id': request_id, 'metrics': metrics, 'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                    else:
                        yield f"event: error\ndata: {json.dumps({'detail': 'AI service temporarily unavailable'}, ensure_ascii=False)}\n\n"
                        yield "event: done\ndata: {}\n\n"
                    rt.remove_active_generation(request_id)
                    return
                warns_local = list(warns)
                warns_local.extend(await persist_ai(str(summary.get("text", "")), summary))
                sw = rt.storage_warning()
                if sw: warns_local.append(sw)
                summary["warnings"] = warns_local
                summary["request_finished_at"] = summary.get("request_finished_at") or rt.now_iso()
                retrieval_warnings = rt.build_user_retrieval_warnings(summary)
                if retrieval_warnings:
                    yield f"event: retrieval_warning\ndata: {json.dumps({'warnings': retrieval_warnings}, ensure_ascii=False)}\n\n"
                metrics = None
                diagnostics = None
                if user_role == "admin":
                    metrics = rt.build_admin_metrics(summary)
                    diagnostics = rt.build_admin_diagnostics(summary)
                    yield f"event: diagnostics\ndata: {json.dumps({'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                done_payload = {
                    "chat_id": chat_id,
                    "request_id": request_id,
                    "citations": summary.get("citations") or [],
                    "warnings": warns_local,
                    "retrieval_warnings": retrieval_warnings,
                    "finish_reason": summary.get("finish_reason"),
                    "continuation_count": summary.get("continuation_count", 0),
                    "continuation_limit_hit": summary.get("continuation_limit_hit", False),
                }
                if metrics is not None:
                    done_payload["metrics"] = metrics
                if diagnostics is not None:
                    done_payload["diagnostics"] = diagnostics
                yield f"event: done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"
                rt.remove_active_generation(request_id)
            return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        summary: Dict[str, Any] = dict(summary_base)
        ai_parts: List[str] = []
        try:
            try:
                async for p in stream_model_text(
                    model,
                    norm_messages,
                    summary,
                    retrieval_enabled,
                    response_language=response_language,
                    user_language=preferred_language,
                    request_base_url=summary.get("request_base_url"),
                ):
                    ai_parts.append(p)
            except HTTPException as e:
                summary["failed_stage"] = summary.get("current_stage") or "generation"
                summary["request_error"] = str(e.detail)
                summary["request_finished_at"] = rt.now_iso()
                summary["warnings"] = list(warns)
                if analytics_user is not None:
                    with rt.tx() as analytics_conn:
                        rt.record_analytics_event(
                            analytics_conn,
                            event_type="chat_completion_failed",
                            surface="chat",
                            user=analytics_user,
                            metadata={"chat_id": str(chat_id), "model": model, "detail": str(e.detail), "status_code": int(e.status_code)},
                        )
                if user_role == "admin":
                    return JSONResponse({
                        "detail": e.detail,
                        "chat_id": chat_id,
                        "admin_metrics": rt.build_admin_metrics(summary),
                        "admin_diagnostics": rt.build_admin_diagnostics(summary),
                    }, status_code=e.status_code)
                raise
            except Exception as e:
                detail = f"llama proxy error: {type(e).__name__}: {e}"
                logger.error("chat completion unexpected error request_id=%s: %s", request_id, detail, exc_info=True)
                summary["failed_stage"] = summary.get("current_stage") or "generation"
                summary["request_error"] = detail
                summary["request_finished_at"] = rt.now_iso()
                summary["warnings"] = list(warns)
                if analytics_user is not None:
                    with rt.tx() as analytics_conn:
                        rt.record_analytics_event(
                            analytics_conn,
                            event_type="chat_completion_failed",
                            surface="chat",
                            user=analytics_user,
                            metadata={"chat_id": str(chat_id), "model": model, "detail": detail},
                        )
                if user_role == "admin":
                    return JSONResponse({
                        "detail": detail,
                        "chat_id": chat_id,
                        "admin_metrics": rt.build_admin_metrics(summary),
                        "admin_diagnostics": rt.build_admin_diagnostics(summary),
                    }, status_code=502)
                raise HTTPException(502, "AI service temporarily unavailable")
            ai_text = "".join(ai_parts)

            warns.extend(await persist_ai(ai_text, summary))
            sw = rt.storage_warning()
            if sw: warns.append(sw)
            summary["warnings"] = warns
            summary["request_finished_at"] = summary.get("request_finished_at") or rt.now_iso()
            citations = summary.get("citations") or []

            retrieval_warnings = rt.build_user_retrieval_warnings(summary)
            out: Dict[str, Any] = {
                "id": rt.uid(),
                "object": "chat.completion",
                "chat_id": chat_id,
                "request_id": request_id,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ai_text, "citations": citations}, "finish_reason": summary.get("finish_reason", "stop")}],
                "citations": citations,
                "warnings": warns,
                "retrieval_warnings": retrieval_warnings,
            }
            if user_role == "admin":
                out["admin_metrics"] = rt.build_admin_metrics(summary)
                out["admin_diagnostics"] = rt.build_admin_diagnostics(summary)
            return JSONResponse(out)
        finally:
            rt.remove_active_generation(request_id)
