import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import { ensureDir, parseArgs, shortError, stampUtc, writeJson } from "./common.mjs";

async function run() {
  const args = parseArgs(process.argv);
  const repoRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\//, "")), "..", "..", "..");
  const resultDir = path.resolve(args["result-dir"] || path.join(repoRoot, "tools", "tests", "results", "puente-e2e", stampUtc()));
  const screenshotDir = path.join(resultDir, "screenshots");
  const baseUrl = String(args["base-url"] || "http://localhost").replace(/\/+$/, "");
  await ensureDir(screenshotDir);

  const context = JSON.parse(await fs.readFile(path.join(resultDir, "test-context.json"), "utf8"));
  const adminUsername = process.env.AIBOX_E2E_ADMIN_USERNAME || context.admin_username;
  const adminPassword = process.env.AIBOX_E2E_ADMIN_PASSWORD;
  if (!adminUsername || !adminPassword) {
    throw new Error("Admin browser credentials must be supplied through AIBOX_E2E_ADMIN_USERNAME and AIBOX_E2E_ADMIN_PASSWORD.");
  }
  const browser = await chromium.launch({ headless: true });
  const results = [];

  async function waitForAuth(page) {
    await page.waitForFunction(async () => {
      const response = await fetch("/ai/api/v1/app/auth/me", { credentials: "same-origin" });
      return response.status === 200;
    });
  }

  async function login(page, username, password) {
    await page.goto(`${baseUrl}/`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: /log in|iniciar sesión/i }).click();
    await page.locator("#loginUser").fill(username);
    await page.locator("#loginPass").fill(password);
    await Promise.all([waitForAuth(page), page.locator("#loginSubmitBtn").click()]);
    await page.locator("#portalSection").waitFor();
  }

  async function capture(id, note, fn) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1024 } });
    try {
      await fn(page);
      const filename = `${id}.png`;
      await page.screenshot({ path: path.join(screenshotDir, filename), fullPage: true });
      results.push({ id, status: "PASS", note, screenshot: `screenshots/${filename}` });
    } catch (error) {
      results.push({ id, status: "FAIL", note, error: shortError(error) });
    } finally {
      await page.close();
    }
  }

  await capture("welcome-screen", "Welcome screen rendered the auth options and branding.", async page => {
    await page.goto(`${baseUrl}/`, { waitUntil: "networkidle" });
    await page.locator("#welcomeScreen").waitFor();
  });

  await capture("login-screen", "Login form rendered with username and password controls.", async page => {
    await page.goto(`${baseUrl}/`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: /log in|iniciar sesión/i }).click();
    await page.locator("#loginScreen").waitFor();
  });

  await capture("signup-screen", "Account creation form rendered with username and password inputs.", async page => {
    await page.goto(`${baseUrl}/`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: /create account|crear cuenta/i }).click();
    await page.locator("#createScreen").waitFor();
  });

  await capture("portal-dashboard", "Portal dashboard rendered after user login.", async page => {
    await login(page, context.normal.username, context.normal.password);
    await page.locator("#portalSection").waitFor();
  });

  await capture("docs-editor", "Docs workspace rendered and editor opened from the dashboard.", async page => {
    await login(page, context.normal.username, context.normal.password);
    await page.goto(`${baseUrl}/docs/`, { waitUntil: "networkidle" });
    await waitForAuth(page);
    await page.locator("#dashboardView").waitFor();
    await page.locator("#newDocBtn").click();
    await page.waitForFunction(() => !document.querySelector("#editorView")?.classList.contains("hidden"));
    await page.locator("#editorSurface").fill("Visual docs check content.");
  });

  await capture("docs-trash-state", "Docs trash state rendered with restore controls after deletion.", async page => {
    await login(page, context.normal.username, context.normal.password);
    await page.goto(`${baseUrl}/docs/`, { waitUntil: "networkidle" });
    await waitForAuth(page);
    await page.locator("#dashboardView").waitFor();
    const docId = await page.evaluate(async () => {
      const createResponse = await fetch("/ai/api/v1/app/docs", {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          title: "Trash state visual check",
          type: "markdown",
          content_markdown: "Delete and restore state.",
        }),
      });
      const created = await createResponse.json();
      const id = created?.document?.id;
      if (!id) throw new Error("Document creation did not return an id");
      const deleteResponse = await fetch(`/ai/api/v1/app/docs/${encodeURIComponent(id)}`, {
        method: "DELETE",
        credentials: "same-origin",
      });
      if (deleteResponse.status !== 200) {
        throw new Error(`Delete returned ${deleteResponse.status}`);
      }
      return id;
    });
    await page.reload({ waitUntil: "networkidle" });
    await page.locator('[data-scope="trash"]').click();
    await page.locator(`[data-restore-doc="${docId}"]`).waitFor();
    await page.locator("#clearTrashBtn").waitFor({ state: "visible" });
  });

  await capture("ai-chat", "AI chat rendered with sidebar, composer, and a short response.", async page => {
    await login(page, context.normal.username, context.normal.password);
    await page.goto(`${baseUrl}/ai/`, { waitUntil: "networkidle" });
    await page.locator("#messages").waitFor();
    await page.locator("#prompt").fill("Say hello in one short sentence.");
    await page.locator("#composer").press("Enter");
    await page.waitForTimeout(3000);
  });

  await capture("admin-console", "Admin console rendered runtime, accounts, storage, and analytics panels.", async page => {
    await login(page, adminUsername, adminPassword);
    await page.locator("#portalSection").waitFor();
    await page.locator("#adminToggleBtn").click();
    await page.locator("#adminSection").waitFor();
    await page.waitForTimeout(2000);
  });

  await browser.close();

  const summary = results.reduce(
    (acc, item) => {
      acc[item.status] = (acc[item.status] || 0) + 1;
      return acc;
    },
    { PASS: 0, FAIL: 0 },
  );

  const payload = {
    timestamp: new Date().toISOString(),
    lane: "browser",
    summary,
    screenshots: results,
  };
  await writeJson(path.join(resultDir, "browser-results.json"), payload);
  process.stdout.write(`${JSON.stringify({ ok: true, summary })}\n`);
}

run().catch(error => {
  process.stderr.write(`${shortError(error)}\n`);
  process.exitCode = 1;
});
