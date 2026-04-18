import path from "node:path";
import {
  SessionClient,
  ensureDir,
  makeBug,
  makeCheck,
  parseArgs,
  readEnvFile,
  shortError,
  stampUtc,
  summarizeChecks,
  writeJson,
} from "./common.mjs";

const MODEL_NAME = "qwen2.5-7b-instruct-q4_0";

function expectStatus(actual, allowed) {
  const list = Array.isArray(allowed) ? allowed : [allowed];
  if (!list.includes(actual)) {
    throw new Error(`Expected ${list.join(" or ")}, got ${actual}`);
  }
}

async function sleep(ms) {
  await new Promise(resolve => setTimeout(resolve, ms));
}

async function waitFor(fn, label, timeoutMs = 180000) {
  const started = Date.now();
  let last = null;
  while (Date.now() - started < timeoutMs) {
    try {
      const value = await fn();
      if (value) return value;
    } catch (error) {
      last = error;
    }
    await sleep(2000);
  }
  throw new Error(`Timed out waiting for ${label}${last ? `: ${shortError(last)}` : ""}`);
}

async function run() {
  const args = parseArgs(process.argv);
  const repoRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\//, "")), "..", "..", "..");
  const env = await readEnvFile(path.join(repoRoot, "stack", ".env"));
  const timestamp = stampUtc();
  const resultDir = path.resolve(args["result-dir"] || path.join(repoRoot, "tools", "tests", "results", "puente-e2e", timestamp));
  await ensureDir(resultDir);
  const baseUrl = String(args["base-url"] || "http://localhost").replace(/\/+$/, "");

  const checks = [];
  const bugs = [];
  const admin = new SessionClient(baseUrl);
  const anon = new SessionClient(baseUrl);
  const accounts = {
    normal: { username: `puente_user_${timestamp.slice(-6).toLowerCase()}`, password: `Puente!${timestamp.slice(-6)}`, role: "user", lang: "en", session: new SessionClient(baseUrl), id: null },
    guest: { username: `puente_guest_${timestamp.slice(-6).toLowerCase()}`, password: `Guest!${timestamp.slice(-6)}`, role: "guest", lang: "es", session: new SessionClient(baseUrl), id: null },
    lockout: { username: `puente_lock_${timestamp.slice(-6).toLowerCase()}`, password: `Lock!${timestamp.slice(-6)}`, role: "user", lang: "en", session: new SessionClient(baseUrl), id: null },
    deleteMe: { username: `puente_delete_${timestamp.slice(-6).toLowerCase()}`, password: `Delete!${timestamp.slice(-6)}`, role: "user", lang: "en", session: new SessionClient(baseUrl), id: null },
    limits: { username: `puente_limit_${timestamp.slice(-6).toLowerCase()}`, password: `Limit!${timestamp.slice(-6)}`, role: "user", lang: "en", session: new SessionClient(baseUrl), id: null },
    saved: { username: `puente_saved_${timestamp.slice(-6).toLowerCase()}`, password: `Saved!${timestamp.slice(-6)}`, role: "user", lang: "en", session: new SessionClient(baseUrl), id: null },
    spam: { username: `puente_spam_${timestamp.slice(-6).toLowerCase()}`, password: `Spam!${timestamp.slice(-6)}`, role: "user", lang: "en", session: new SessionClient(baseUrl), id: null },
  };

  const noteIds = {};

  async function record(id, name, fn) {
    try {
      const detail = await fn();
      checks.push(makeCheck(id, name, "live", "PASS", detail || {}));
    } catch (error) {
      checks.push(makeCheck(id, name, "live", "FAIL", { error: shortError(error) }));
    }
  }

  async function signup(acct) {
    const { response, data } = await anon.json("/ai/api/v1/app/auth/signup", {
      method: "POST",
      body: {
        username: acct.username,
        password: acct.password,
        role: acct.role,
        preferred_language: acct.lang,
        preferred_theme: "light",
      },
    });
    expectStatus(response.status, 200);
    acct.id = data?.user?.id || null;
  }

  async function login(session, username, password, status = 200) {
    const { response, data } = await session.json("/ai/api/v1/app/auth/login", {
      method: "POST",
      body: { username, password },
    });
    expectStatus(response.status, status);
    return data;
  }

  async function authMe(session, status = 200) {
    const { response, data } = await session.json("/ai/api/v1/app/auth/me");
    expectStatus(response.status, status);
    return data;
  }

  await record("runtime-health", "Public runtime health/status endpoints respond", async () => {
    for (const route of ["/ai/api/health", "/ai/api/ready"]) {
      const res = await anon.json(route);
      expectStatus(res.response.status, [200, 503]);
    }
    const live = await anon.json("/ai/api/live");
    expectStatus(live.response.status, 200);
    const status = await anon.json("/ai/api/status");
    expectStatus(status.response.status, 200);
    return { runtime_state: status.data?.llama_state || null, status_reason: status.data?.status_reason || null };
  });

  await record("admin-login", "Admin login works", async () => {
    await login(admin, env.ADMIN_USERNAME, env.ADMIN_DEFAULT_PASSWORD);
    const me = await authMe(admin);
    if (me?.user?.role !== "admin") throw new Error("Admin session did not resolve to admin");
    noteIds.adminId = me.user.id;
    return { admin_user: me.user.username };
  });

  await record("runtime-anon-mutate", "Anonymous runtime mutation is rejected", async () => {
    const probe = await anon.json("/ai/api/v1/admin/runtime/start", { method: "POST" });
    if (probe.response.status === 200) {
      bugs.push(
        makeBug("BUG-runtime-admin-auth", "Runtime control POST routes are unauthenticated", "high", {
          area: "tools/ai-control/app.py",
          repro: "POST /ai/api/v1/admin/runtime/start without an admin session cookie",
          expected: "401 or 403",
          actual: "200 OK and runtime state changed",
          evidence: { override_mode: probe.data?.override_mode || null, last_action: probe.data?.last_action || null },
        }),
      );
      const revert = await admin.json("/ai/api/v1/admin/runtime/clear-override", { method: "POST" });
      expectStatus(revert.response.status, 200);
      throw new Error("Anonymous runtime mutation unexpectedly succeeded");
    }
    expectStatus(probe.response.status, [401, 403]);
    return { http_status: probe.response.status };
  });

  await record("create-test-accounts", "Primary test accounts can be created", async () => {
    for (const acct of Object.values(accounts)) await signup(acct);
    const dupe = await anon.json("/ai/api/v1/app/auth/signup", {
      method: "POST",
      body: { username: accounts.normal.username, password: accounts.normal.password },
    });
    expectStatus(dupe.response.status, 409);
    return { created: Object.keys(accounts).length };
  });

  await record("login-me-logout", "Normal login, auth/me, and logout work", async () => {
    await login(accounts.normal.session, accounts.normal.username, accounts.normal.password);
    const me = await authMe(accounts.normal.session);
    const logout = await accounts.normal.session.json("/ai/api/v1/app/auth/logout", { method: "POST" });
    expectStatus(logout.response.status, 200);
    await authMe(accounts.normal.session, 401);
    return { username: me.user.username, role: me.user.role };
  });

  await record("guest-preferences", "Guest accounts remain guest and force light theme", async () => {
    await login(accounts.guest.session, accounts.guest.username, accounts.guest.password);
    const pref = await accounts.guest.session.json("/ai/api/v1/app/auth/preferences", {
      method: "POST",
      body: { preferred_language: "es", preferred_theme: "dark" },
    });
    expectStatus(pref.response.status, 200);
    const me = await authMe(accounts.guest.session);
    if (me.user.role !== "guest") throw new Error("Expected guest role");
    if (me.user.preferred_theme !== "light") throw new Error("Guest theme should be forced to light");
    return { language: me.user.preferred_language, theme: me.user.preferred_theme };
  });

  await record("login-lockout", "Repeated invalid logins trigger lockout", async () => {
    let blockedStatus = null;
    for (let i = 0; i < 6; i += 1) {
      const attempt = await accounts.lockout.session.json("/ai/api/v1/app/auth/login", {
        method: "POST",
        body: { username: accounts.lockout.username, password: "wrong-pass" },
      });
      expectStatus(attempt.response.status, [401, 429]);
      if (attempt.response.status === 429) {
        blockedStatus = attempt.response.status;
        break;
      }
    }
    if (blockedStatus == null) {
      const blocked = await accounts.lockout.session.json("/ai/api/v1/app/auth/login", {
        method: "POST",
        body: { username: accounts.lockout.username, password: "wrong-pass" },
      });
      expectStatus(blocked.response.status, 429);
      blockedStatus = blocked.response.status;
    }
    return { blocked_status: blockedStatus };
  });

  await record("admin-users", "Admin users endpoint lists created test accounts", async () => {
    const { response, data } = await admin.json("/ai/api/v1/app/admin/users");
    expectStatus(response.status, 200);
    const names = new Set((data?.users || []).map(row => row.username));
    for (const acct of Object.values(accounts)) {
      if (!names.has(acct.username)) throw new Error(`Missing ${acct.username} in admin listing`);
    }
    return { listed_users: data.users.length };
  });

  await record("admin-reset-lock-unlock-delete", "Admin reset, lock, unlock, self-delete protection, and user deletion work", async () => {
    const resetPassword = `${accounts.normal.password}_2`;
    let res = await admin.json(`/ai/api/v1/app/admin/users/${accounts.normal.id}/reset-password`, {
      method: "POST",
      body: { password: resetPassword },
    });
    expectStatus(res.response.status, 200);
    accounts.normal.password = resetPassword;
    await login(accounts.normal.session, accounts.normal.username, accounts.normal.password);
    res = await admin.json(`/ai/api/v1/app/admin/users/${accounts.normal.id}/lock`, {
      method: "POST",
      body: { reason: "e2e lock", duration_minutes: 5, permanent: false },
    });
    expectStatus(res.response.status, 200);
    const lockedLogin = await anon.json("/ai/api/v1/app/auth/login", {
      method: "POST",
      body: { username: accounts.normal.username, password: accounts.normal.password },
    });
    expectStatus(lockedLogin.response.status, 423);
    res = await admin.json(`/ai/api/v1/app/admin/users/${accounts.normal.id}/unlock`, {
      method: "POST",
      body: { reason: "e2e unlock" },
    });
    expectStatus(res.response.status, 200);
    const selfDelete = await admin.json(`/ai/api/v1/app/admin/users/${noteIds.adminId}/delete`, { method: "POST" });
    expectStatus(selfDelete.response.status, 409);
    const deleteUser = await admin.json(`/ai/api/v1/app/admin/users/${accounts.deleteMe.id}/delete`, { method: "POST" });
    expectStatus(deleteUser.response.status, 200);
    const deletedLogin = await anon.json("/ai/api/v1/app/auth/login", {
      method: "POST",
      body: { username: accounts.deleteMe.username, password: accounts.deleteMe.password },
    });
    expectStatus(deletedLogin.response.status, 401);
    return { deleted_user: accounts.deleteMe.username };
  });

  await record("docs-crud-and-trash", "Docs create/update/star/delete/restore/trash-clear work", async () => {
    await login(accounts.normal.session, accounts.normal.username, accounts.normal.password);
    const create = await accounts.normal.session.json("/ai/api/v1/app/docs", {
      method: "POST",
      body: { title: "E2E Primary Doc", type: "markdown", content_markdown: "# Hello" },
    });
    expectStatus(create.response.status, 200);
    noteIds.docId = create.data.document.id;
    let op = await accounts.normal.session.json(`/ai/api/v1/app/docs/${noteIds.docId}`, {
      method: "PATCH",
      body: { title: "E2E Primary Doc Updated", content_markdown: "# Updated" },
    });
    expectStatus(op.response.status, 200);
    op = await accounts.normal.session.json(`/ai/api/v1/app/docs/${noteIds.docId}/star`, {
      method: "POST",
      body: { starred: true },
    });
    expectStatus(op.response.status, 200);
    op = await accounts.normal.session.json(`/ai/api/v1/app/docs/${noteIds.docId}`, { method: "DELETE" });
    expectStatus(op.response.status, 409);
    op = await accounts.normal.session.json(`/ai/api/v1/app/docs/${noteIds.docId}/star`, {
      method: "POST",
      body: { starred: false },
    });
    expectStatus(op.response.status, 200);
    op = await accounts.normal.session.json(`/ai/api/v1/app/docs/${noteIds.docId}`, { method: "DELETE" });
    expectStatus(op.response.status, 200);
    op = await accounts.normal.session.json(`/ai/api/v1/app/docs/${noteIds.docId}/restore`, { method: "POST" });
    expectStatus(op.response.status, 200);
    const trashDoc = await accounts.normal.session.json("/ai/api/v1/app/docs", {
      method: "POST",
      body: { title: "E2E Trash Doc", type: "markdown", content_markdown: "trash me" },
    });
    expectStatus(trashDoc.response.status, 200);
    noteIds.trashDocId = trashDoc.data.document.id;
    op = await accounts.normal.session.json(`/ai/api/v1/app/docs/${noteIds.trashDocId}`, { method: "DELETE" });
    expectStatus(op.response.status, 200);
    op = await accounts.normal.session.json("/ai/api/v1/app/docs/trash/clear", { method: "POST" });
    expectStatus(op.response.status, 200);
    return { restored_doc_id: noteIds.docId, cleared_count: op.data?.deleted_count ?? null };
  });

  await record("docs-limits", "Max docs and repeated docs offenses trigger blocking", async () => {
    await login(accounts.limits.session, accounts.limits.username, accounts.limits.password);
    let maxBlocked = false;
    for (let i = 0; i < 160; i += 1) {
      const res = await accounts.limits.session.json("/ai/api/v1/app/docs", {
        method: "POST",
        body: { title: `limit-doc-${i}`, type: "markdown", content_markdown: "x" },
      });
      if (res.response.status === 200) continue;
      expectStatus(res.response.status, 429);
      maxBlocked = true;
      break;
    }
    if (!maxBlocked) throw new Error("Max docs limit did not trigger");
    const oversized = "x".repeat(300000);
    let res = await accounts.limits.session.json("/ai/api/v1/app/docs", {
      method: "POST",
      body: { title: "oversized-one", type: "markdown", content_markdown: oversized },
    });
    expectStatus(res.response.status, 429);
    res = await accounts.limits.session.json("/ai/api/v1/app/docs", {
      method: "POST",
      body: { title: "oversized-two", type: "markdown", content_markdown: oversized },
    });
    expectStatus(res.response.status, 429);
    res = await accounts.limits.session.json("/ai/api/v1/app/docs", {
      method: "POST",
      body: { title: "blocked-now", type: "markdown", content_markdown: "hello" },
    });
    expectStatus(res.response.status, 429);
    return { docs_limit_blocked: true, docs_write_blocked: true };
  });

  await record("chat-saved-capacity", "Saved chat capacity and folder actions work", async () => {
    await login(accounts.saved.session, accounts.saved.username, accounts.saved.password);
    let res = await accounts.saved.session.json("/ai/api/v1/app/chat-folders", {
      method: "POST",
      body: { name: "Saved Folder" },
    });
    expectStatus(res.response.status, 200);
    const folderId = res.data.folder.id;
    for (let i = 0; i < 10; i += 1) {
      const chat = await accounts.saved.session.json("/ai/api/v1/app/chats", { method: "POST", body: { title: `saved-chat-${i}` } });
      expectStatus(chat.response.status, 200);
      const patch = await accounts.saved.session.json(`/ai/api/v1/app/chats/${chat.data.chat.id}`, {
        method: "PATCH",
        body: { is_saved: true, folder_id: folderId },
      });
      expectStatus(patch.response.status, 200);
    }
    const overflow = await accounts.saved.session.json("/ai/api/v1/app/chats", { method: "POST", body: { title: "saved-chat-overflow" } });
    expectStatus(overflow.response.status, 200);
    noteIds.savedOverflowChatId = overflow.data.chat.id;
    res = await accounts.saved.session.json(`/ai/api/v1/app/chats/${noteIds.savedOverflowChatId}`, {
      method: "PATCH",
      body: { is_saved: true },
    });
    expectStatus(res.response.status, 409);
    res = await accounts.saved.session.json(`/ai/api/v1/app/chat-folders/${folderId}`, {
      method: "PATCH",
      body: { name: "Saved Folder Renamed" },
    });
    expectStatus(res.response.status, 200);
    res = await accounts.saved.session.json(`/ai/api/v1/app/chat-folders/${folderId}`, { method: "DELETE" });
    expectStatus(res.response.status, 200);
    return { folder_deleted: true };
  });

  await record("chat-completion", "Chat completions persist messages and allow delete/restore", async () => {
    const res = await accounts.normal.session.json("/ai/api/v1/app/chat/completions", {
      method: "POST",
      body: {
        model: MODEL_NAME,
        messages: [{ role: "user", content: "What is photosynthesis?" }],
        stream: false,
        retrieval_enabled: true,
      },
    });
    expectStatus(res.response.status, 200);
    noteIds.chatId = res.data?.chat_id || res.data?.chat?.id;
    if (!noteIds.chatId) throw new Error("Chat completion did not return a chat id");
    let loaded = await accounts.normal.session.json(`/ai/api/v1/app/chats/${noteIds.chatId}`);
    expectStatus(loaded.response.status, 200);
    if ((loaded.data?.chat?.messages || []).length < 2) throw new Error("Expected persisted user and assistant messages");
    let op = await accounts.normal.session.json(`/ai/api/v1/app/chats/${noteIds.chatId}`, { method: "DELETE" });
    expectStatus(op.response.status, 200);
    op = await accounts.normal.session.json(`/ai/api/v1/app/chats/${noteIds.chatId}/restore`, { method: "POST" });
    expectStatus(op.response.status, 200);
    loaded = await accounts.normal.session.json(`/ai/api/v1/app/chats/${noteIds.chatId}`);
    expectStatus(loaded.response.status, 200);
    return {
      chat_id: noteIds.chatId,
      has_citations: Array.isArray((loaded.data?.chat?.messages || []).at(-1)?.citations) && (loaded.data.chat.messages.at(-1).citations || []).length > 0,
    };
  });

  await record("chat-abuse-controls", "Prompt guard and repeated heavy prompts escalate to AI block", async () => {
    await login(accounts.spam.session, accounts.spam.username, accounts.spam.password);
    let res = await accounts.spam.session.json("/ai/api/v1/app/chat/completions", {
      method: "POST",
      body: {
        model: MODEL_NAME,
        messages: [{ role: "user", content: "x".repeat(7101) }],
        stream: false,
        retrieval_enabled: false,
      },
    });
    expectStatus(res.response.status, 400);
    const heavy = "x".repeat(5500);
    let blockedCount = 0;
    for (let i = 0; i < 6; i += 1) {
      res = await accounts.spam.session.json("/ai/api/v1/app/chat/completions", {
        method: "POST",
        body: {
          model: MODEL_NAME,
          messages: [{ role: "user", content: heavy }],
          stream: false,
          retrieval_enabled: false,
        },
      });
      if (res.response.status === 429) blockedCount += 1;
      if (res.response.status !== 200 && res.response.status !== 429) {
        throw new Error(`Unexpected heavy-prompt status ${res.response.status}`);
      }
      if (blockedCount >= 3) break;
      await sleep(11000);
    }
    const me = await authMe(accounts.spam.session);
    if (!me.user.ai_send_blocked_until) throw new Error("AI send block was not escalated");
    return { blocked_count: blockedCount, ai_send_blocked_until: me.user.ai_send_blocked_until };
  });

  await record("admin-events-storage-analytics", "Admin security, storage, and analytics endpoints reflect activity", async () => {
    let res = await admin.json("/ai/api/v1/app/admin/security-events?limit=200");
    expectStatus(res.response.status, 200);
    const userEvents = res.data.events || [];
    res = await admin.json("/ai/api/v1/app/admin/users");
    expectStatus(res.response.status, 200);
    const spamRow = (res.data.users || []).find(row => row.username === accounts.spam.username);
    if (!spamRow || !(Number(spamRow.recent_flag_count || 0) > 0)) throw new Error("Spam user did not accumulate recent flags");
    for (const route of [
      "/ai/api/v1/app/admin/storage-insights",
      "/ai/api/v1/app/admin/analytics/summary",
      "/ai/api/v1/app/admin/analytics/timeseries",
    ]) {
      res = await admin.json(route);
      expectStatus(res.response.status, 200);
    }
    const exportRes = await admin.request("/ai/api/v1/app/admin/analytics/export?export_format=json");
    expectStatus(exportRes.status, 200);
    return { security_events: userEvents.length, spam_flags: spamRow.recent_flag_count };
  });

  await record("runtime-admin-actions", "Admin runtime stop/start/restart/clear-override work", async () => {
    let res = await admin.json("/ai/api/v1/admin/runtime/stop", { method: "POST" });
    expectStatus(res.response.status, 200);
    await waitFor(async () => {
      const status = await admin.json("/ai/api/v1/admin/status");
      return status.data?.llama_state === "stopped";
    }, "runtime stop");
    res = await admin.json("/ai/api/v1/admin/runtime/start", { method: "POST" });
    expectStatus(res.response.status, 200);
    await waitFor(async () => {
      const status = await admin.json("/ai/api/v1/admin/status");
      return status.data?.llama_state === "running" && status.data?.health === true;
    }, "runtime start");
    res = await admin.json("/ai/api/v1/admin/runtime/restart", { method: "POST" });
    expectStatus(res.response.status, 200);
    await waitFor(async () => {
      const status = await admin.json("/ai/api/v1/admin/status");
      return status.data?.llama_state === "running" && status.data?.health === true;
    }, "runtime restart");
    res = await admin.json("/ai/api/v1/admin/runtime/clear-override", { method: "POST" });
    expectStatus(res.response.status, 200);
    return { override_mode: res.data?.override_mode || null, llama_state: res.data?.llama_state || null };
  });

  await record("adjacent-smoke", "Wiki and learn shells are reachable", async () => {
    let res = await anon.request(`${baseUrl}/wiki/`);
    expectStatus(res.status, 200);
    res = await anon.request(`${baseUrl}/learn/`);
    expectStatus(res.status, [302, 308]);
    return { wiki_status: 200, learn_status: res.status };
  });

  const payload = {
    timestamp,
    lane: "live",
    baseUrl,
    checks,
    bugs,
    summary: summarizeChecks(checks),
    test_accounts: {
      admin_username: env.ADMIN_USERNAME,
      admin_password: env.ADMIN_DEFAULT_PASSWORD,
      normal: { username: accounts.normal.username, password: accounts.normal.password },
      guest: { username: accounts.guest.username, password: accounts.guest.password },
    },
    notable_ids: noteIds,
  };

  await writeJson(path.join(resultDir, "live-results.json"), payload);
  await writeJson(path.join(resultDir, "test-context.json"), payload.test_accounts);
  process.stdout.write(`${JSON.stringify({ ok: true, result_dir: resultDir, summary: payload.summary })}\n`);
}

run().catch(error => {
  process.stderr.write(`${shortError(error)}\n`);
  process.exitCode = 1;
});
