import fs from "node:fs/promises";
import path from "node:path";

export function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith("--")) continue;
    const key = arg.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      out[key] = true;
      continue;
    }
    out[key] = next;
    i += 1;
  }
  return out;
}

export async function ensureDir(target) {
  await fs.mkdir(target, { recursive: true });
  return target;
}

export async function writeJson(target, value) {
  await ensureDir(path.dirname(target));
  await fs.writeFile(target, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

export async function writeText(target, value) {
  await ensureDir(path.dirname(target));
  await fs.writeFile(target, value, "utf8");
}

export async function readEnvFile(envPath) {
  const raw = await fs.readFile(envPath, "utf8");
  const out = {};
  for (const line of raw.split(/\r?\n/)) {
    if (!line || line.trim().startsWith("#")) continue;
    const idx = line.indexOf("=");
    if (idx < 0) continue;
    const key = line.slice(0, idx).trim();
    const value = line.slice(idx + 1).trim();
    out[key] = value;
  }
  return out;
}

export function stampUtc(date = new Date()) {
  const iso = date.toISOString();
  return iso.replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

export function summarizeChecks(checks) {
  const summary = { PASS: 0, FAIL: 0, WARN: 0, SKIP: 0, NA: 0 };
  for (const check of checks) {
    const key = String(check.status || "NA").toUpperCase();
    if (!(key in summary)) summary[key] = 0;
    summary[key] += 1;
  }
  return summary;
}

export class SessionClient {
  constructor(baseUrl) {
    this.baseUrl = String(baseUrl || "").replace(/\/+$/, "");
    this.cookies = new Map();
  }

  cookieHeader() {
    if (!this.cookies.size) return "";
    return Array.from(this.cookies.entries())
      .map(([key, value]) => `${key}=${value}`)
      .join("; ");
  }

  updateCookies(response) {
    const values =
      typeof response.headers.getSetCookie === "function"
        ? response.headers.getSetCookie()
        : (response.headers.get("set-cookie") ? [response.headers.get("set-cookie")] : []);
    for (const raw of values) {
      if (!raw) continue;
      const first = String(raw).split(";")[0];
      const idx = first.indexOf("=");
      if (idx < 1) continue;
      const key = first.slice(0, idx).trim();
      const value = first.slice(idx + 1).trim();
      if (!value) {
        this.cookies.delete(key);
      } else {
        this.cookies.set(key, value);
      }
    }
  }

  async request(route, options = {}) {
    const url = route.startsWith("http") ? route : `${this.baseUrl}${route}`;
    const headers = new Headers(options.headers || {});
    const cookie = this.cookieHeader();
    if (cookie) headers.set("cookie", cookie);
    let body = options.body;
    if (
      body &&
      typeof body === "object" &&
      !(body instanceof Uint8Array) &&
      !(body instanceof ArrayBuffer) &&
      !(body instanceof String)
    ) {
      headers.set("content-type", "application/json");
      body = JSON.stringify(body);
    }
    const response = await fetch(url, {
      method: options.method || "GET",
      headers,
      body,
      redirect: "manual",
    });
    this.updateCookies(response);
    return response;
  }

  async json(route, options = {}) {
    const response = await this.request(route, options);
    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = { raw: text };
      }
    }
    return { response, data, text };
  }
}

export function makeCheck(id, name, lane, status, details = {}) {
  return {
    ...details,
    id,
    name,
    lane,
    status,
    timestamp: new Date().toISOString(),
  };
}

export function makeBug(id, title, severity, details = {}) {
  return {
    id,
    title,
    severity,
    timestamp: new Date().toISOString(),
    ...details,
  };
}

export function shortError(error) {
  if (!error) return "Unknown error";
  if (error instanceof Error) return `${error.name}: ${error.message}`;
  return String(error);
}
