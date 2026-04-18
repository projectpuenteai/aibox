import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/app")

from app_storage import StorageRuntime


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def insert_doc(rt, conn, user_id, title, deleted=False, deleted_at=None, starred=False, age_days=0):
    doc_id = rt.uid()
    base_time = datetime.now(timezone.utc) - timedelta(days=age_days)
    payload = {
        "id": doc_id,
        "title": title,
        "content_markdown": f"# {title}",
        "created_at": iso(base_time),
        "updated_at": iso(base_time),
        "version": 1,
    }
    file_rel = rt.doc_rel(user_id, doc_id)
    if deleted:
        file_rel = rt.trash_rel(user_id, "docs", doc_id)
    file_path = rt.safe_path(file_rel, user_id)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    size_bytes = rt.write_json_atomic(file_path, payload)
    conn.execute(
        """
        INSERT INTO documents(
            id,user_id,title,type,created_at,updated_at,last_accessed_at,size_bytes,file_path,
            is_starred,is_deleted,deleted_at,deleted_by_user,is_guest_owned
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            doc_id,
            user_id,
            title,
            "markdown",
            iso(base_time),
            iso(base_time),
            iso(base_time),
            int(size_bytes),
            file_rel,
            1 if starred else 0,
            1 if deleted else 0,
            deleted_at,
            1 if deleted else 0,
            0,
        ),
    )
    return doc_id


def insert_chat(rt, conn, user_id, title, deleted=False, deleted_at=None, saved=False, age_days=0):
    chat_id = rt.uid()
    base_time = datetime.now(timezone.utc) - timedelta(days=age_days)
    payload = {
        "id": chat_id,
        "title": title,
        "messages": [{"role": "user", "content": title}],
        "created_at": iso(base_time),
        "updated_at": iso(base_time),
        "version": 1,
    }
    file_rel = rt.chat_rel(user_id, chat_id)
    if deleted:
        file_rel = rt.trash_rel(user_id, "chats", chat_id)
    file_path = rt.safe_path(file_rel, user_id)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    size_bytes = rt.write_json_atomic(file_path, payload)
    conn.execute(
        """
        INSERT INTO chats(
            id,user_id,title,created_at,updated_at,last_accessed_at,token_count_estimate,file_path,size_bytes,
            is_deleted,deleted_at,deleted_by_user,is_guest_owned,is_saved,folder_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            chat_id,
            user_id,
            title,
            iso(base_time),
            iso(base_time),
            iso(base_time),
            16,
            file_rel,
            int(size_bytes),
            1 if deleted else 0,
            deleted_at,
            1 if deleted else 0,
            0,
            1 if saved else 0,
            None,
        ),
    )
    return chat_id


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: retention_clone_check.py <clone-root> <result-json>")

    clone_root = Path(sys.argv[1])
    result_json = Path(sys.argv[2])

    os.environ["APP_DATA_ROOT"] = str(clone_root)
    os.environ["APP_DB_PATH"] = str(clone_root / "db" / "app.db")

    rt = StorageRuntime(os.getenv("LLAMA_BASE_URL", "http://llama:2020"))
    rt.ensure_dirs()
    rt.init_db()
    rt.seed_admin()

    now = datetime.now(timezone.utc)
    old_guest_cutoff = now - timedelta(days=rt.guest_retention_days + 2)
    old_trash_cutoff = now - timedelta(days=rt.trash_retention_days + 2)

    with rt.tx() as conn:
        guest_id = rt.uid()
        retained_id = rt.uid()
        pressure_id = rt.uid()
        for uid_, username, role, created_at, last_active_at in (
            (guest_id, "clone-guest-expire", "guest", iso(old_guest_cutoff), iso(old_guest_cutoff)),
            (retained_id, "clone-retained", "user", rt.now_iso(), rt.now_iso()),
            (pressure_id, "clone-pressure", "user", rt.now_iso(), rt.now_iso()),
        ):
            rt.ensure_user_dirs(uid_)
            conn.execute(
                """
                INSERT OR REPLACE INTO users(
                    id,username,username_norm,password_hash,role,created_at,last_login_at,last_active_at,
                    storage_bytes_used,preferred_language,preferred_theme,is_deleted
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)
                """,
                (
                    uid_,
                    username,
                    rt.nuser(username),
                    rt._ph.hash("temp-pass"),
                    role,
                    created_at,
                    created_at,
                    last_active_at,
                    0,
                    "en",
                    "light",
                ),
            )
            rt.ensure_restrictions_row(conn, uid_)

        insert_doc(rt, conn, guest_id, "guest-expire-doc", age_days=rt.guest_retention_days + 5)
        insert_chat(rt, conn, guest_id, "guest-expire-chat", age_days=rt.guest_retention_days + 5)

        old_deleted_doc = insert_doc(rt, conn, retained_id, "old-deleted-doc", deleted=True, deleted_at=iso(old_trash_cutoff))
        old_deleted_chat = insert_chat(rt, conn, retained_id, "old-deleted-chat", deleted=True, deleted_at=iso(old_trash_cutoff))
        old_unprotected_doc = insert_doc(rt, conn, pressure_id, "old-unstarred-doc", age_days=rt.doc_retention_days + 5)
        old_protected_doc = insert_doc(rt, conn, pressure_id, "old-starred-doc", starred=True, age_days=rt.doc_retention_days + 5)
        old_unprotected_chat = insert_chat(rt, conn, pressure_id, "old-unsaved-chat", saved=False, age_days=rt.chat_retention_days + 5)
        old_protected_chat = insert_chat(rt, conn, pressure_id, "old-saved-chat", saved=True, age_days=rt.chat_retention_days + 5)

        for user_id in (guest_id, retained_id, pressure_id):
            rt.recalc_storage(conn, user_id)

    cleanup = rt.run_cleanup("isolated_test", required=rt.disk()["free_bytes"])

    with rt.tx() as conn:
        guest_exists = conn.execute("SELECT 1 FROM users WHERE id=?", (guest_id,)).fetchone() is not None
        trash_doc_exists = conn.execute("SELECT 1 FROM documents WHERE id=?", (old_deleted_doc,)).fetchone() is not None
        trash_chat_exists = conn.execute("SELECT 1 FROM chats WHERE id=?", (old_deleted_chat,)).fetchone() is not None
        old_doc_exists = conn.execute("SELECT 1 FROM documents WHERE id=?", (old_unprotected_doc,)).fetchone() is not None
        old_starred_exists = conn.execute("SELECT 1 FROM documents WHERE id=?", (old_protected_doc,)).fetchone() is not None
        old_chat_exists = conn.execute("SELECT 1 FROM chats WHERE id=?", (old_unprotected_chat,)).fetchone() is not None
        old_saved_exists = conn.execute("SELECT 1 FROM chats WHERE id=?", (old_protected_chat,)).fetchone() is not None
        cleanup_events = int(conn.execute("SELECT COUNT(*) AS c FROM cleanup_events").fetchone()["c"])

    payload = {
        "timestamp": rt.now_iso(),
        "lane": "retention-clone",
        "cleanup": cleanup,
        "checks": [
            {"id": "clone-trash-doc-purge", "name": "Old deleted docs are hard deleted", "status": "PASS" if not trash_doc_exists else "FAIL"},
            {"id": "clone-trash-chat-purge", "name": "Old deleted chats are hard deleted", "status": "PASS" if not trash_chat_exists else "FAIL"},
            {"id": "clone-guest-expire", "name": "Inactive guest accounts are deleted", "status": "PASS" if not guest_exists else "FAIL"},
            {"id": "clone-pressure-doc-purge", "name": "Old unstarred docs are deleted under cleanup pressure", "status": "PASS" if not old_doc_exists else "FAIL"},
            {"id": "clone-pressure-doc-protect", "name": "Starred docs survive cleanup pressure", "status": "PASS" if old_starred_exists else "FAIL"},
            {"id": "clone-pressure-chat-purge", "name": "Old unsaved chats are deleted under cleanup pressure", "status": "PASS" if not old_chat_exists else "FAIL"},
            {"id": "clone-pressure-chat-protect", "name": "Saved chats survive cleanup pressure", "status": "PASS" if old_saved_exists else "FAIL"},
            {"id": "clone-cleanup-event", "name": "Cleanup run records an event", "status": "PASS" if cleanup_events > 0 else "FAIL"},
        ],
    }
    result_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
