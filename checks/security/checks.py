"""Security suite checks (8.x): auth load, secret hygiene, key rotation, sessions, image pinning."""
from __future__ import annotations

import os
import re
from pathlib import Path

from aibox.checks.harness.base import Check, CheckResult, register


# 8.1 — Auth load + lockout (stub) -------------------------------------------

@register(
    suite="security", id="8.1", name="auth_load_lockout",
    status="stub",
    description="100 bad-password/sec for 30s against a TEST user (not a real one). "
                "Confirms lockout fires and other endpoints stay responsive. "
                "Stubbed to avoid locking out real accounts.",
    destructive=True,
)
class AuthLoadLockout(Check):
    def run(self, ctx) -> CheckResult:
        ctx.metric("required_setup", "test user with disposable credentials in storage.db")
        return CheckResult(
            outcome="stub",
            summary="would hammer /v1/app/auth/login as TEST_USER with wrong passwords; "
                    "first build the test-user provisioning helper",
        )


# 8.2 — Secret hygiene --------------------------------------------------------

# Intentionally LOOSE regexes — false positives are OK, false negatives are not.
SUSPECT_PATTERNS = [
    (re.compile(r"\$argon2id\$[A-Za-z0-9$+/=]+"), "argon2id_hash_in_log"),
    (re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*[A-Za-z0-9/+=]{20,}"), "aws_secret"),
    (re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.]{20,}"), "api_key_like"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "private_key"),
    (re.compile(r"AIBOX_(STORAGE_KEY|ADMIN_PASSWORD|SESSION_PEPPER)\s*=\s*\S+"), "env_secret_value"),
]

SCAN_REL = [
    "aibox/logs",
    "aibox/backend-data/logs",
    "aibox/stack/logs",
]

ALLOWED_NAMES_WITH_SECRETS = {".env", ".env.example"}


@register(
    suite="security", id="8.2", name="secret_hygiene",
    status="real",
    description="Greps log files for things that look like secrets. False positives allowed.",
)
class SecretHygiene(Check):
    def run(self, ctx) -> CheckResult:
        hits = []
        scanned_files = 0
        scanned_bytes = 0
        for rel in SCAN_REL:
            base = ctx.repo_root / rel
            if not base.exists():
                continue
            for root, _dirs, files in os.walk(base):
                for f in files:
                    fp = Path(root) / f
                    if fp.name in ALLOWED_NAMES_WITH_SECRETS:
                        continue
                    try:
                        size = fp.stat().st_size
                        if size > 50 * 1024 * 1024:
                            continue
                        scanned_files += 1
                        scanned_bytes += size
                        text = fp.read_text(encoding="utf-8", errors="ignore")
                    except (OSError, UnicodeError):
                        continue
                    for pattern, tag in SUSPECT_PATTERNS:
                        if pattern.search(text):
                            hits.append((fp.relative_to(ctx.repo_root).as_posix(), tag))
                            break
        ctx.metric("scanned_files", scanned_files)
        ctx.metric("scanned_bytes", scanned_bytes, unit="B")
        ctx.metric("suspect_hits", len(hits))
        for path, tag in hits[:10]:
            ctx.metric("hit", path, tag=tag)
        if not hits:
            return CheckResult(outcome="ok",
                               summary=f"{scanned_files} files scanned, no suspect strings")
        return CheckResult(
            outcome="fail",
            summary=f"{len(hits)} suspect strings (e.g., {hits[0][1]} in {hits[0][0]})",
        )


# 8.3 — Storage-key rotation (stub) ------------------------------------------

@register(
    suite="security", id="8.3", name="storage_key_rotation",
    status="stub",
    description="Snapshot backend-data, rotate AIBOX_STORAGE_KEY, re-encrypt, "
                "verify decryption with new key. Stubbed pending a documented "
                "rotation procedure in ai-control.",
    destructive=True,
)
class StorageKeyRotation(Check):
    def run(self, ctx) -> CheckResult:
        ctx.metric("required_input", "AIBOX_STORAGE_KEY_NEW (new key candidate)")
        return CheckResult(
            outcome="stub",
            summary="needs a rotate_storage_key() helper exposed by ai-control before "
                    "this can run safely",
        )


# 8.4 — Session isolation (stub) ---------------------------------------------

@register(
    suite="security", id="8.4", name="session_isolation",
    status="stub",
    description="Playwright-driven end-to-end test: session expiry, refresh, replay-after-expiry, "
                "cross-user data isolation. Needs Playwright installed and a test-user fixture.",
)
class SessionIsolation(Check):
    def run(self, ctx) -> CheckResult:
        ctx.metric("required_deps", "playwright + chromium")
        return CheckResult(
            outcome="stub",
            summary="add Playwright to requirements.txt and write the e2e suite under checks/security/e2e/",
        )


# 8.5 — Image digest pinning -------------------------------------------------

@register(
    suite="security", id="8.5", name="image_digest_pin",
    status="real",
    description="Check that compose-file image references include @sha256:<digest>. "
                "Reports unpinned services.",
    requires=("cmd:docker",),
)
class ImageDigestPin(Check):
    def run(self, ctx) -> CheckResult:
        compose = ctx.repo_root / "aibox" / "stack" / "docker-compose.yaml"
        if not compose.exists():
            return CheckResult(outcome="skipped",
                               summary=f"no compose file at {compose.relative_to(ctx.repo_root)}")
        text = compose.read_text(encoding="utf-8", errors="ignore")
        pinned = unpinned = 0
        unpinned_names = []
        for line in text.splitlines():
            s = line.strip()
            if not s.startswith("image:"):
                continue
            value = s.split(":", 1)[1].strip().strip("'\"")
            if "@sha256:" in value:
                pinned += 1
            else:
                unpinned += 1
                unpinned_names.append(value)
        ctx.metric("pinned_images", pinned)
        ctx.metric("unpinned_images", unpinned)
        for name in unpinned_names[:20]:
            ctx.metric("unpinned", name)
        if pinned + unpinned == 0:
            return CheckResult(outcome="skipped", summary="no image: lines found in compose file")
        outcome = "fail" if unpinned > 0 else "ok"
        return CheckResult(
            outcome=outcome,
            summary=f"{pinned} pinned, {unpinned} unpinned" +
                    (f" (e.g., {unpinned_names[0]})" if unpinned_names else ""),
        )
