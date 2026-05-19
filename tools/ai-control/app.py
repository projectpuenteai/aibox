"""Control-plane service for the active llama.cpp-based AI stack.

This service watches the `llama` container defined in `stack/docker-compose.yaml`,
exposes admin and health endpoints, and mounts the broader storage/chat API from
`app_storage.py`.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import docker
import httpx
from docker.errors import DockerException, NotFound
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

LLAMA_CONTAINER_NAME = os.getenv("LLAMA_CONTAINER_NAME", "aibox-llama")
LLAMA_HEALTH_URL = os.getenv("LLAMA_HEALTH_URL", "http://llama:2020/health")
LLAMA_BASE_URL = os.getenv("LLAMA_BASE_URL", "http://llama:2020")
STATE_PATH = os.getenv("STATE_PATH", "/state/control_state.json")
RECONCILE_SECONDS = max(1, int(os.getenv("RECONCILE_SECONDS", "5")))
STACK_DEFAULT_DESIRED = os.getenv("STACK_DEFAULT_DESIRED", "1") == "1"
RESET_OVERRIDE_ON_START = os.getenv("RESET_OVERRIDE_ON_START", "1") == "1"
RUNTIME_CONTROL_ENABLED = os.getenv("RUNTIME_CONTROL_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


app = FastAPI(title="AI Control", version="1.0")
logger = logging.getLogger("aibox.ai_control")


_state_lock = threading.Lock()
_service_meta_lock = threading.Lock()
_state: Dict[str, Any] = {
    "override_mode": "auto",  # auto | forced_on | forced_off
    "last_action": "init",
    "last_error": None,
    "updated_at": None,
    "last_heartbeat_utc": None,
}

_service_meta: Dict[str, Optional[str]] = {
    "startup_started_at": None,
    "startup_completed_at": None,
    "startup_error": None,
    "shutdown_started_at": None,
}
_service_ready = threading.Event()

_docker_client = None
_storage_runtime = None

# Per-action debounce: separate timers per action so alternating start/stop
# clicks can't all be eaten by a single shared 5-second window. Each action
# only debounces against its own previous invocation.
_docker_action_ts: Dict[str, float] = {"start": 0.0, "stop": 0.0, "restart": 0.0}
_docker_intended_state: Optional[str] = None  # "running" | "stopped" | None
_docker_debounce_seconds = 5.0


def _should_skip_docker_action(action: str, intended_state: Optional[str]) -> bool:
    """Return True when the action should be debounced.

    Skips if the same action fired within the last `_docker_debounce_seconds`
    AND the intended-state already matches what's stored. This lets a real
    user-driven flip (start→stop→start) get through while still suppressing
    button-mashing of the same action.
    """
    global _docker_intended_state
    now = time.time()
    last_ts = _docker_action_ts.get(action, 0.0)
    if (now - last_ts) < _docker_debounce_seconds and _docker_intended_state == intended_state:
        return True
    _docker_action_ts[action] = now
    if intended_state is not None:
        _docker_intended_state = intended_state
    return False


class TogglePayload(BaseModel):
    """Compatibility payload for routes that only turn AI on or off."""

    enabled: bool


def _utc_now() -> str:
    """Return the current UTC time in ISO format for state and health payloads."""
    return datetime.now(timezone.utc).isoformat()


def _ensure_state_dir() -> None:
    """Create the directory that stores the persisted control-plane state file."""
    directory = os.path.dirname(STATE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)


def _save_state() -> None:
    """Write the in-memory override state to the JSON file mounted at `/state`."""
    _ensure_state_dir()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(_state, f, indent=2)


def _load_state() -> None:
    """Load the persisted override state or create a default state file if missing."""
    global _state
    _ensure_state_dir()
    if not os.path.isfile(STATE_PATH):
        _state["updated_at"] = _utc_now()
        _save_state()
        return

    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            _state.update(raw)
    except Exception:
        logger.warning("Failed to load state from %s", STATE_PATH, exc_info=True)
        _state["last_error"] = "state_load_failed"



_RUNTIME_DISABLED_MSG = (
    "Docker runtime control is disabled. Set RUNTIME_CONTROL_ENABLED=true and "
    "use the runtime-control compose override to enable it."
)


def _docker() -> Optional[docker.DockerClient]:
    """Return the Docker client, or None when runtime control is disabled.

    Returns None (instead of raising) when RUNTIME_CONTROL_ENABLED is false so
    callers can short-circuit cleanly without catching exceptions for an
    expected, deterministic configuration state. Docker connectivity failures
    still raise DockerException as before.
    """
    global _docker_client
    if not RUNTIME_CONTROL_ENABLED:
        return None
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def _describe_docker_error(exc: Exception) -> str:
    """Collapse Docker client exceptions into a short user-facing message."""
    return f"{type(exc).__name__}: {exc}"


def _lookup_llama_container():
    """Return the configured llama container plus any Docker access error."""
    client = _docker()
    if client is None:
        return None, _RUNTIME_DISABLED_MSG
    try:
        return client.containers.get(LLAMA_CONTAINER_NAME), None
    except NotFound:
        return None, None
    except DockerException as exc:
        return None, _describe_docker_error(exc)


def _get_llama_container():
    """Return the configured llama container object or `None` when it is unavailable."""
    container, _docker_error = _lookup_llama_container()
    return container


def _llama_reachable(timeout_s: float = 2.0) -> bool:
    """Check whether the llama HTTP health endpoint responds at all."""
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.get(LLAMA_HEALTH_URL)
            return response.status_code < 500
    except Exception:
        return False


def _gpu_attachment(container) -> Dict[str, Any]:
    """Inspect Docker metadata to see whether GPU devices were attached to llama."""
    if container is None:
        return {
            "gpu_attached": False,
            "gpu_device_requests": [],
        }

    attrs = getattr(container, "attrs", {}) or {}
    host_config = attrs.get("HostConfig", {})
    device_requests = host_config.get("DeviceRequests") or []

    return {
        "gpu_attached": bool(device_requests),
        "gpu_device_requests": device_requests,
    }


def _llama_status_snapshot() -> Dict[str, Any]:
    """Combine Docker state, HTTP reachability, and GPU info into one status payload."""
    container, docker_error = _lookup_llama_container()
    docker_access = "ok" if docker_error is None else "unavailable"
    exists = container is not None

    running = False
    status = "not_found" if docker_error is None else "docker_unavailable"
    container_health = None
    if container is not None:
        try:
            container.reload()
        except DockerException:
            pass
        attrs = container.attrs or {}
        state = attrs.get("State", {})
        status = state.get("Status", "unknown")
        running = bool(state.get("Running", False))
        container_health = (state.get("Health") or {}).get("Status")

    reachable = _llama_reachable()
    if container is None and docker_error and reachable:
        # When Docker socket access is unavailable but the llama health endpoint
        # still answers, treat the runtime as healthy-but-unverified instead of
        # incorrectly reporting "container not found".
        exists = True
        running = True
        status = "running_unverified"
        container_health = "unknown"
    gpu_info = _gpu_attachment(container)
    gpu_attached = gpu_info["gpu_attached"]
    gpu_status = "ok" if gpu_attached else "degraded_no_gpu_attachment"
    if container is None and docker_error and reachable:
        gpu_attached = True
        gpu_status = "unverified_docker_unavailable"

    if not exists:
        llama_state = "not_found"
    elif running:
        llama_state = "running"
    else:
        llama_state = "stopped"

    health = bool(running and reachable)

    return {
        "container_name": LLAMA_CONTAINER_NAME,
        "exists": exists,
        "container_status": status,
        "container_health": container_health,
        "docker_access": docker_access,
        "docker_error": docker_error,
        "llama_state": llama_state,
        "llama_reachable": reachable,
        "health": health,
        "gpu_attached": gpu_attached,
        "gpu_device_requests": gpu_info["gpu_device_requests"],
        "gpu_status": gpu_status,
        "health_url": LLAMA_HEALTH_URL,
    }


def _container_status(name: str) -> Dict[str, Any]:
    """Return a compact Docker status for non-llama stack services."""
    client = _docker()
    if client is None:
        return {
            "container_name": name,
            "exists": None,
            "running": None,
            "status": "docker_unavailable",
            "health": None,
            "ok": False,
            "docker_error": _RUNTIME_DISABLED_MSG,
        }
    try:
        container = client.containers.get(name)
        container.reload()
    except NotFound:
        return {"container_name": name, "exists": False, "running": False, "status": "not_found", "health": None, "ok": False}
    except DockerException as exc:
        return {
            "container_name": name,
            "exists": None,
            "running": None,
            "status": "docker_unavailable",
            "health": None,
            "ok": False,
            "docker_error": _describe_docker_error(exc),
        }

    attrs = container.attrs or {}
    state = attrs.get("State", {}) or {}
    status = state.get("Status", "unknown")
    health = (state.get("Health") or {}).get("Status")
    running = bool(state.get("Running", False))
    ok = bool(running and (health in (None, "healthy")))
    return {
        "container_name": name,
        "exists": True,
        "running": running,
        "status": status,
        "health": health,
        "ok": ok,
    }


def _stack_services_snapshot() -> Dict[str, Any]:
    """Expose admin diagnostics for all user-facing compose services."""
    service_names = {
        "dns": "aibox-dns",
        "caddy": "aibox-caddy",
        "kiwix_en": "aibox-kiwix-en",
        "kiwix_es": "aibox-kiwix-es",
        "kolibri": "aibox-kolibri",
    }
    services = {key: _container_status(name) for key, name in service_names.items()}
    chat_status = _container_status("aibox-chat")
    if chat_status.get("status") == "not_found":
        chat_status = {
            **chat_status,
            "ok": True,
            "optional": True,
            "status": "disabled_by_profile",
        }
    services["chat"] = chat_status
    return {
        "ok": all(bool(item.get("ok")) for item in services.values()),
        "services": services,
    }


def _update_service_meta(**updates: Optional[str]) -> None:
    """Update startup and shutdown bookkeeping used by readiness endpoints."""
    with _service_meta_lock:
        _service_meta.update(updates)


def _service_meta_snapshot() -> Dict[str, Optional[str]]:
    """Return a safe copy of startup and shutdown service metadata."""
    with _service_meta_lock:
        return dict(_service_meta)


def _set_state(override_mode: Optional[str] = None, action: Optional[str] = None, error: Optional[str] = None) -> None:
    """Update the desired runtime mode and persist it to disk."""
    with _state_lock:
        if override_mode is not None:
            _state["override_mode"] = override_mode
        if action is not None:
            _state["last_action"] = action
        _state["last_error"] = error
        _state["updated_at"] = _utc_now()
        _save_state()

def _normalize_override_on_start() -> None:
    """Optionally reset forced runtime overrides back to `auto` during startup."""
    if not RESET_OVERRIDE_ON_START:
        return

    with _state_lock:
        mode = _state.get("override_mode", "auto")
        if mode == "auto":
            return
        _state["override_mode"] = "auto"
        _state["last_action"] = "startup_reset_override"
        _state["last_error"] = None
        _state["updated_at"] = _utc_now()
        _save_state()

def _start_llama() -> None:
    """Start the llama container through the Docker API."""
    if _should_skip_docker_action("start", "running"):
        return
    container = _get_llama_container()
    if container is None:
        raise RuntimeError(f"Llama container '{LLAMA_CONTAINER_NAME}' not found")
    container.start()


def _stop_llama() -> None:
    """Stop the llama container if it currently exists."""
    if _should_skip_docker_action("stop", "stopped"):
        return
    container = _get_llama_container()
    if container is None:
        return
    container.stop(timeout=20)


def _restart_llama() -> None:
    """Restart the llama container through the Docker API."""
    # Restart is a stop→start cycle; treat it as targeting the "running" state
    # so a subsequent rapid start does nothing, but a rapid stop still flips.
    if _should_skip_docker_action("restart", "running"):
        return
    container = _get_llama_container()
    if container is None:
        raise RuntimeError(f"Llama container '{LLAMA_CONTAINER_NAME}' not found")
    container.restart(timeout=20)


def _reconcile_once() -> None:
    """Apply the desired override mode to the actual Docker container state.

    This is the core control-plane loop. It compares the persisted admin intent
    with the observed Docker state and then starts or stops the llama container
    so the runtime eventually matches the API-level setting.
    """
    with _state_lock:
        mode = _state.get("override_mode", "auto")

    snapshot = _llama_status_snapshot()
    running = snapshot["llama_state"] == "running"

    try:
        if mode == "forced_off" and running:
            _stop_llama()
        elif mode == "forced_on" and not running:
            _start_llama()
        elif mode == "auto" and STACK_DEFAULT_DESIRED and not running:
            _start_llama()
        _set_state(error=None)
    except Exception as exc:
        _set_state(error=f"{type(exc).__name__}: {exc}")



def _background_reconcile_loop() -> None:
    """Continuously re-run reconciliation so container drift gets corrected."""
    while True:
        try:
            _reconcile_once()
            with _state_lock:
                _state["last_heartbeat_utc"] = _utc_now()
                _save_state()
        except Exception:
            logger.exception("reconcile loop unexpected error")
        time.sleep(RECONCILE_SECONDS)


def _storage_status_snapshot() -> Dict[str, Any]:
    """Ask the mounted storage runtime for its readiness snapshot when available."""
    runtime = globals().get("_storage_runtime")
    if runtime is None or not hasattr(runtime, "rag_status_snapshot"):
        return {"available": False, "ready": True}
    try:
        snapshot = dict(runtime.rag_status_snapshot() or {})
    except Exception as exc:
        logger.error("storage status snapshot failed: %s", exc)
        return {
            "available": True,
            "ready": False,
            "startup_rag_ok": False,
            "startup_rag_error": f"{type(exc).__name__}: {exc}",
        }
    snapshot["available"] = True
    snapshot["ready"] = bool(snapshot.get("ready", snapshot.get("startup_rag_ok")))
    return snapshot


def _status_payload() -> Dict[str, Any]:
    """Build the combined health payload exposed by `/health`, `/ready`, and admin routes."""
    snapshot = _llama_status_snapshot()
    stack_snapshot = _stack_services_snapshot()
    storage_snapshot = _storage_status_snapshot()
    service_meta = _service_meta_snapshot()
    service_ready = _service_ready.is_set()
    with _state_lock:
        mode = _state.get("override_mode", "auto")
        last_action = _state.get("last_action")
        last_error = _state.get("last_error")
        updated_at = _state.get("updated_at")
        heartbeat = _state.get("last_heartbeat_utc")

    startup_error = service_meta.get("startup_error")
    shutdown_started_at = service_meta.get("shutdown_started_at")
    if startup_error:
        service_state = "error"
    elif shutdown_started_at and not service_ready:
        service_state = "stopping"
    elif service_ready:
        service_state = "running"
    else:
        service_state = "starting"
    desired = "on" if mode in ("auto", "forced_on") else "off"
    runtime_expected = mode in ("auto", "forced_on")
    llama_ready = (not runtime_expected) or bool(snapshot["health"])
    storage_ready = bool(storage_snapshot.get("ready", True))
    readiness_ok = bool(service_ready and not startup_error and llama_ready and storage_ready)
    docker_access = snapshot.get("docker_access")
    docker_unavailable = docker_access == "unavailable"
    # portal_ok is a diagnostic flag: FastAPI process is up and storage runtime
    # mounted. The portal loading overlay watches the stricter readiness_ok
    # (llama healthy + RAG smoke test passed) so users see a meaningful loading
    # state during warm-up. portal_ok stays in the payload for admin tooling
    # that distinguishes "FastAPI up, llama warming" from "FastAPI down".
    portal_ok = bool(service_ready and not startup_error and storage_snapshot.get("available", True))
    status_reason = "ok"
    if startup_error:
        status_reason = "startup_failed"
    elif shutdown_started_at and not service_ready:
        status_reason = "shutdown_in_progress"
    elif not service_ready:
        status_reason = "startup_in_progress"
    elif mode == "forced_off":
        status_reason = "manual_forced_off"
    elif docker_unavailable and runtime_expected and snapshot["health"]:
        status_reason = "docker_unavailable_but_reachable"
    elif docker_unavailable and runtime_expected and not snapshot["health"]:
        status_reason = "docker_unavailable_and_llama_unreachable"
    elif not snapshot["exists"]:
        status_reason = "llama_container_not_found"
    elif not snapshot["gpu_attached"] and not docker_unavailable:
        status_reason = "gpu_not_attached"
    elif runtime_expected and not snapshot["health"]:
        status_reason = "llama_unhealthy"
    elif not storage_ready:
        status_reason = str(storage_snapshot.get("startup_rag_error") or "startup_rag_unready")

    return {
        "service_state": service_state,
        "runtime_control_enabled": RUNTIME_CONTROL_ENABLED,
        "override_mode": mode,
        "desired_state": desired,
        "runtime_expected": runtime_expected,
        "status_reason": status_reason,
        "last_action": last_action,
        "last_error": last_error,
        "updated_at": updated_at,
        "last_heartbeat_utc": heartbeat,
        "service_ready": service_ready,
        "startup_started_at": service_meta.get("startup_started_at"),
        "startup_completed_at": service_meta.get("startup_completed_at"),
        "startup_error": startup_error,
        "shutdown_started_at": shutdown_started_at,
        "llama": snapshot,
        "stack_services": stack_snapshot,
        "llama_state": snapshot["llama_state"],
        "health": snapshot["health"],
        "gpu_status": snapshot["gpu_status"],
        "storage": storage_snapshot,
        "startup_rag_ok": storage_snapshot.get("startup_rag_ok"),
        "readiness_ok": readiness_ok,
        "portal_ok": portal_ok,
        "liveness_ok": True,
    }


def _require_runtime_admin(req: Request, write: bool = False) -> None:
    """Require an authenticated admin session for runtime-control routes."""
    runtime = globals().get("_storage_runtime")
    if runtime is None or not hasattr(runtime, "tx") or not hasattr(runtime, "req_user"):
        raise HTTPException(status_code=503, detail="Storage runtime unavailable")
    with runtime.tx() as c:
        runtime.req_user(c, req, admin=True, write=write)
    if write and not RUNTIME_CONTROL_ENABLED:
        raise HTTPException(status_code=503, detail="Runtime control is disabled on this deployment")

@app.on_event("startup")
def startup() -> None:
    """Load persisted state, reconcile once, and start the background monitor loop."""
    logger.info("ai-control startup begin")
    _service_ready.clear()
    _update_service_meta(
        startup_started_at=_utc_now(),
        startup_completed_at=None,
        startup_error=None,
        shutdown_started_at=None,
    )
    try:
        _load_state()
        _normalize_override_on_start()
        if RUNTIME_CONTROL_ENABLED:
            # Both calls below are safe to run repeatedly: Docker's container
            # start/stop API is a no-op when the target is already in the
            # desired state, and `_reconcile_once` itself is idempotent (it
            # reads the persisted intent and only acts when reality diverges).
            # So the initial sync here and the background loop below cannot
            # double-fire actions.
            try:
                _reconcile_once()
            except Exception:
                logger.exception("ai-control reconcile during startup failed")
            t = threading.Thread(target=_background_reconcile_loop, daemon=True, name="ai-control-reconcile")
            t.start()
        else:
            logger.info("runtime control disabled; skipping Docker reconcile loop")
    except Exception as exc:
        _update_service_meta(startup_error=f"{type(exc).__name__}: {exc}")
        logger.exception("ai-control startup failed")
        raise
    _service_ready.set()
    _update_service_meta(startup_completed_at=_utc_now())
    logger.info("ai-control startup complete")


@app.on_event("shutdown")
def shutdown() -> None:
    """Mark the service as stopping so readiness endpoints report shutdown cleanly."""
    _service_ready.clear()
    _update_service_meta(shutdown_started_at=_utc_now())
    logger.warning("ai-control shutdown begin")


def _public_startup_progress(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build a non-admin-safe startup progress block for /health consumers.

    Exposes only a phase name and a coarse percent so the portal loading
    overlay can show "loading Spanish index" instead of an opaque spinner.
    Internal error strings stay on the admin /v1/admin/health surface.
    """
    storage = payload.get("storage") or {}
    llama_state = payload.get("llama_state") or {}
    service_state = payload.get("service_state")
    readiness_ok = bool(payload.get("readiness_ok"))
    if readiness_ok:
        return {"phase": "ready", "percent": 100}
    if service_state == "starting":
        if not payload.get("portal_ok"):
            return {"phase": "starting", "percent": 10}
        # storage runtime mounted but RAG smoke test still running
        if not storage.get("collection_loaded"):
            return {"phase": "loading_index", "percent": 35}
        if storage.get("collection_loaded") and not storage.get("startup_rag_ok"):
            return {"phase": "validating_rag", "percent": 60}
        if not llama_state.get("ready"):
            return {"phase": "waiting_for_llama", "percent": 80}
        return {"phase": "finalizing", "percent": 90}
    if service_state == "stopping":
        return {"phase": "stopping", "percent": 0}
    if service_state == "error":
        return {"phase": "error", "percent": 0}
    return {"phase": service_state or "unknown", "percent": 50}


@app.get("/health")
def health_public() -> Dict[str, Any]:
    """Minimal liveness check for Docker HEALTHCHECK — no internal details exposed."""
    payload = _status_payload()
    ok = bool(payload.get("readiness_ok"))
    result = {
        "ok": ok,
        "readiness_ok": ok,
        "startup_progress": _public_startup_progress(payload),
    }
    if ok:
        return result
    return JSONResponse(status_code=503, content=result)


@app.get("/ready")
def ready_public() -> Dict[str, Any]:
    """Alias of /health for compatibility probes — returns minimal public payload."""
    return health_public()


@app.get("/live")
def live_public() -> Dict[str, Any]:
    """Process liveness — always 200 while the ASGI process is running."""
    return {"ok": True, "liveness_ok": True}


@app.get("/v1/admin/health")
def health(req: Request) -> Dict[str, Any]:
    """Full readiness payload for authenticated admin clients."""
    _require_runtime_admin(req)
    payload = _status_payload()
    ok = bool(payload.get("readiness_ok"))
    payload["ok"] = ok
    if ok:
        return payload
    return JSONResponse(status_code=503, content=payload)


@app.get("/v1/admin/ready")
def ready(req: Request) -> Dict[str, Any]:
    """Alias `/v1/admin/ready` to the full readiness payload."""
    return health(req)


@app.get("/v1/admin/live")
def live(req: Request) -> Dict[str, Any]:
    """Full liveness payload for authenticated admin clients."""
    _require_runtime_admin(req)
    payload = _status_payload()
    payload["ok"] = True
    return payload


@app.get("/v1/admin/status")
def status(req: Request) -> Dict[str, Any]:
    """Expose the full combined control-plane status snapshot — admin only."""
    _require_runtime_admin(req)
    return _status_payload()


@app.get("/v1/admin/ai-enabled")
def get_ai_enabled(req: Request) -> Dict[str, Any]:
    """Return the simplified enabled-state view used by admin clients."""
    _require_runtime_admin(req)
    payload = _status_payload()
    enabled = payload["override_mode"] != "forced_off" and bool(payload.get("health", False))
    return {
        "enabled": enabled,
        "override_mode": payload["override_mode"],
        "llama_state": payload["llama_state"],
        "health": payload["health"],
    }


@app.post("/v1/admin/ai-enabled")
def post_ai_enabled(body: TogglePayload, req: Request) -> Dict[str, Any]:
    """Flip AI on and off with one boolean."""
    _require_runtime_admin(req, write=True)
    try:
        if body.enabled:
            _set_state(override_mode="forced_on", action="compat_enable", error=None)
            _start_llama()
        else:
            _set_state(override_mode="forced_off", action="compat_disable", error=None)
            _stop_llama()
    except Exception as exc:
        _set_state(action="compat_error", error=f"{type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    return get_ai_enabled(req)


@app.post("/v1/admin/runtime/start")
def runtime_start(req: Request) -> Dict[str, Any]:
    """Force the llama runtime on and return the updated status payload."""
    _require_runtime_admin(req, write=True)
    try:
        _set_state(override_mode="forced_on", action="runtime_start", error=None)
        _start_llama()
    except Exception as exc:
        _set_state(action="runtime_start_error", error=f"{type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
    return _status_payload()


@app.post("/v1/admin/runtime/stop")
def runtime_stop(req: Request) -> Dict[str, Any]:
    """Force the llama runtime off and return the updated status payload."""
    _require_runtime_admin(req, write=True)
    try:
        _set_state(override_mode="forced_off", action="runtime_stop", error=None)
        _stop_llama()
    except Exception as exc:
        _set_state(action="runtime_stop_error", error=f"{type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
    return _status_payload()


@app.post("/v1/admin/runtime/restart")
def runtime_restart(req: Request) -> Dict[str, Any]:
    """Restart the llama container and return the updated combined status view."""
    _require_runtime_admin(req, write=True)
    try:
        _set_state(override_mode="forced_on", action="runtime_restart", error=None)
        _restart_llama()
    except Exception as exc:
        _set_state(action="runtime_restart_error", error=f"{type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
    return _status_payload()


@app.post("/v1/admin/runtime/clear-override")
def runtime_clear_override(req: Request) -> Dict[str, Any]:
    """Return runtime control to automatic mode and reconcile immediately."""
    _require_runtime_admin(req, write=True)
    _set_state(override_mode="auto", action="runtime_clear_override", error=None)
    # Reconcile immediately for responsive UX.
    try:
        _reconcile_once()
    except Exception as exc:
        _set_state(action="runtime_clear_override_error", error=f"{type(exc).__name__}: {exc}")
        return JSONResponse(status_code=500, content=_status_payload())
    return _status_payload()







# Mount the larger user-facing storage and chat API from `app_storage.py` so
# this service can expose both runtime controls and application routes together.
logger.info("mounting app storage")
try:
    from app_storage import mount_app_storage
    _storage_runtime = mount_app_storage(app, LLAMA_BASE_URL)
except Exception:
    logger.exception("app storage mount failed")
    raise
logger.info("app storage mounted")



