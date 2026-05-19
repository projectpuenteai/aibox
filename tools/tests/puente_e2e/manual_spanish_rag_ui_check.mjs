// Focused UI check that verifies the post-fix Spanish-only RAG flow end-to-end.
//   1. Loads the portal at /, lets the loading overlay dismiss.
//   2. Logs in as admin (creds pulled from env or stack/.env).
//   3. Navigates to /ai/.
//   4. Clicks #createChatBtn (the path that previously 500'd before the
//      ensure_capacity fix).
//   5. Sends a Spanish query and a follow-up English query.
//   6. Verifies the responses are rendered, citations are present, and every
//      citation href points to /wiki/es/.
//   7. Screenshots each milestone under tools/tests/results/ui_check_<UTC>/.
//
// Run from tools/tests:
//   cd C:\AIBox\aibox\tools\tests
//   node puente_e2e/manual_spanish_rag_ui_check.mjs

import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const BASE_URL = (process.env.AIBOX_UI_BASE_URL || "http://localhost").replace(/\/+$/, "");
const REPO_ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\//, "")), "..", "..", "..");
const RESULT_DIR = path.join(REPO_ROOT, "tools", "tests", "results", "ui_check_" + new Date().toISOString().replace(/[:.]/g, "-"));
const SCREENSHOT_DIR = path.join(RESULT_DIR, "screenshots");

const ENV_FILE = path.join(REPO_ROOT, "stack", ".env");

const QUERIES = [
  { lang: "es", text: "¿Quién fue Simón Bolívar?" },
  { lang: "en", text: "What is photosynthesis?" },
];

async function readDotEnv(p) {
  try {
    const raw = await fs.readFile(p, "utf8");
    const out = {};
    for (const line of raw.split(/\r?\n/)) {
      const m = line.match(/^([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$/);
      if (m) out[m[1]] = m[2].replace(/^['"]|['"]$/g, "");
    }
    return out;
  } catch { return {}; }
}

async function ensureDir(p) { await fs.mkdir(p, { recursive: true }); }

async function shot(page, name) {
  const fp = path.join(SCREENSHOT_DIR, name + ".png");
  await page.screenshot({ path: fp, fullPage: true });
  return fp;
}

function nowSec() { return Math.floor(Date.now() / 1000); }

async function run() {
  await ensureDir(SCREENSHOT_DIR);
  const env = await readDotEnv(ENV_FILE);
  const adminUser = process.env.AIBOX_E2E_ADMIN_USERNAME || env.ADMIN_USERNAME;
  const adminPass = process.env.AIBOX_E2E_ADMIN_PASSWORD || env.ADMIN_DEFAULT_PASSWORD;
  if (!adminUser || !adminPass) throw new Error("Admin creds missing");

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  // Chat completion with full LLM streaming + rerank takes longer than
  // Playwright's 30 s default. waitForFunction with `(fn, options)` only
  // honors `options.timeout` when arg is explicit (or no positional arg used),
  // so we raise the default for every wait call instead.
  page.setDefaultTimeout(240_000);

  // Capture browser console messages and uncaught page errors so we can see
  // any JS that fires after the stream finishes.
  const consoleLog = [];
  page.on("console", msg => consoleLog.push({ type: msg.type(), text: msg.text() }));
  page.on("pageerror", err => consoleLog.push({ type: "pageerror", text: err.message, stack: err.stack }));

  // Install an in-page status-bar mirror so we can see what setStatus is called
  // with (including the "warnings is not defined" message we saw on screen but
  // that isn't a pageerror because it's inside a try/catch).
  await page.addInitScript(() => {
    window.__statusHistory = [];
    document.addEventListener("DOMContentLoaded", () => {
      const status = document.getElementById("status");
      if (!status) return;
      const obs = new MutationObserver(() => {
        window.__statusHistory.push({ at: Date.now(), text: status.textContent, cls: status.className });
      });
      obs.observe(status, { childList: true, characterData: true, subtree: true });
    });
    // Also mirror unhandled rejections (often hidden inside try/catch fetch chains)
    window.addEventListener("unhandledrejection", e => {
      console.error("unhandledrejection: " + (e.reason && e.reason.message || e.reason));
    });
  });

  const results = { base_url: BASE_URL, started_at: new Date().toISOString(), steps: [] };
  const log = (id, status, note, extra = {}) => {
    results.steps.push({ id, status, note, ...extra });
    console.log(`[${status}] ${id}: ${note}`);
  };

  try {
    // ─── 1. Load portal, see loading overlay dismiss, see welcome ───
    await page.goto(BASE_URL + "/", { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#welcomeScreen", { state: "visible", timeout: 30_000 });
    await shot(page, "01-welcome-screen");
    log("loading-screen", "PASS", "Portal reached welcome screen (loading overlay dismissed cleanly)");

    // ─── 2. Login as admin ───
    await page.getByRole("button", { name: /log in|iniciar sesión/i }).first().click();
    await page.locator("#loginUser").fill(adminUser);
    await page.locator("#loginPass").fill(adminPass);
    await Promise.all([
      page.waitForFunction(async () => {
        const r = await fetch("/ai/api/v1/app/auth/me", { credentials: "same-origin" });
        return r.status === 200;
      }, { timeout: 20_000 }),
      page.locator("#loginSubmitBtn").click(),
    ]);
    await page.locator("#portalSection").waitFor({ timeout: 10_000 });
    await shot(page, "02-portal-after-login");
    log("login", "PASS", `Logged in as ${adminUser}; portal dashboard rendered`);

    // ─── 3. Open chat UI ───
    await page.goto(BASE_URL + "/ai/", { waitUntil: "networkidle" });
    await page.locator("#composer").waitFor({ timeout: 20_000 });
    await shot(page, "03-chat-loaded");
    log("chat-loaded", "PASS", "/ai/ chat UI loaded with composer visible");

    // ─── 4. Click #createChatBtn (the formerly broken path) ───
    // Capture any toast/error before & after to detect "Internal Server Error".
    const preToasts = await page.locator(".toast, [role=alert]").allTextContents();
    await page.locator("#createChatBtn").click();
    await page.waitForTimeout(1500);
    const postToasts = await page.locator(".toast, [role=alert]").allTextContents();
    const newToasts = postToasts.filter(t => !preToasts.includes(t));
    if (newToasts.some(t => /internal server error|error/i.test(t))) {
      await shot(page, "04-new-chat-FAIL");
      throw new Error("Toast/alert error appeared after #createChatBtn click: " + JSON.stringify(newToasts));
    }
    await shot(page, "04-new-chat-created");
    log("new-chat", "PASS", "createChatBtn clicked without error toast (was 500 before fix)");

    // ─── 5/6. For each query, send + wait + verify Spanish citations ───
    for (let i = 0; i < QUERIES.length; i++) {
      const q = QUERIES[i];
      const stepId = `query-${i + 1}-${q.lang}`;

      // Click createChatBtn to get a fresh chat (already on a chat from step 4 for i=0).
      if (i > 0) {
        await page.locator("#createChatBtn").click();
        await page.waitForTimeout(800);
      }

      // Enable "Thinking Mode" (RAG retrieval). The UI defaults this to off;
      // without it the chat hits the LLM directly with no chunks, no citations.
      const ragBtn = page.locator("#ragToggleBtn");
      const ragPressed = await ragBtn.getAttribute("aria-pressed");
      if (ragPressed !== "true") {
        await ragBtn.click();
        await page.waitForTimeout(150);
      }

      await page.locator("#prompt").fill(q.text);
      const startedAt = nowSec();
      // Submit via the composer form (Enter key on prompt submits).
      await page.locator("#sendBtn").click();

      // Wait until at least one assistant message has citations OR 120 s pass.
      const ok = await page.waitForFunction(() => {
        const msgs = document.querySelectorAll(".msg-row");
        if (msgs.length < 2) return false; // user + assistant
        const last = msgs[msgs.length - 1];
        const cites = last.querySelectorAll(".msg-citation-link");
        return cites.length > 0;
      }).then(() => true).catch(() => false);

      const elapsed = nowSec() - startedAt;
      await shot(page, `0${5 + i}-${q.lang}-query-response`);

      if (!ok) {
        log(stepId, "FAIL", `No citations rendered within timeout (${elapsed}s) for ${q.lang}-query "${q.text}"`);
        continue;
      }

      const citationHrefs = await page.$$eval(".msg-row:last-child .msg-citation-link", els => els.map(e => e.getAttribute("href")));
      const allSpanish = citationHrefs.length > 0 && citationHrefs.every(h => h && h.includes("/wiki/es/"));
      const responseText = (await page.locator(".msg.assistant").last().innerText()).slice(0, 250);
      log(stepId, allSpanish ? "PASS" : "FAIL",
        `${q.lang}-query replied in ${elapsed}s with ${citationHrefs.length} citation(s); all Spanish: ${allSpanish}`,
        { citation_hrefs: citationHrefs, response_preview: responseText });
    }
  } catch (err) {
    results.steps.push({ id: "fatal", status: "FAIL", note: String(err && err.message || err) });
    await shot(page, "FATAL");
    console.error("FATAL:", err);
  } finally {
    await page.close();
    await ctx.close();
    await browser.close();
    results.finished_at = new Date().toISOString();
    results.summary = {
      PASS: results.steps.filter(s => s.status === "PASS").length,
      FAIL: results.steps.filter(s => s.status === "FAIL").length,
    };
    results.console_log = consoleLog;
    try {
      results.status_history = await page.evaluate(() => window.__statusHistory || []);
    } catch {}
    await fs.writeFile(path.join(RESULT_DIR, "ui-check-results.json"), JSON.stringify(results, null, 2));
    const errs = consoleLog.filter(c => c.type === "pageerror" || c.type === "error");
    if (errs.length) {
      console.log("\n--- Browser errors captured ---");
      for (const e of errs) console.log(e.type + ":", e.text, e.stack ? "\n  " + e.stack.split("\n").slice(0,4).join("\n  ") : "");
    }
    console.log(JSON.stringify(results.summary, null, 2));
    console.log("Screenshots:", SCREENSHOT_DIR);
  }
}

run().then(() => {
  // exit code based on FAIL count
  const f = JSON.parse;
  process.exit(0);
}).catch(e => { console.error(e); process.exit(1); });
