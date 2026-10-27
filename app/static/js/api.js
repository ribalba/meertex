// The one way the web app talks to the server. Every call goes through
// request(), so a lost session always ends on the sign-in page and every error
// carries a sentence a person can read.

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

// `detail` is a string, an object with `message` (meertex's own refusals), or
// FastAPI's list of validation errors.
export function detailText(detail, status) {
  if (detail === null || detail === undefined || detail === "") {
    return status ? `The server answered with an error (${status}).` : "Something went wrong.";
  }
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (typeof d === "string") return d;
        const loc = (d.loc || []).filter((p) => p !== "body" && p !== "query").join(".");
        return loc ? `${d.msg} (${loc})` : d.msg;
      })
      .join(" ");
  }
  if (typeof detail === "object") return detail.message || detail.msg || detail.error || JSON.stringify(detail);
  return String(detail);
}

export function toLogin() {
  const next = location.pathname + location.search;
  location.assign(`/login?next=${encodeURIComponent(next)}`);
}

export function withQuery(path, query) {
  if (!query) return path;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === null || v === undefined || v === "") continue;
    qs.set(k, String(v));
  }
  const s = qs.toString();
  return s ? `${path}${path.includes("?") ? "&" : "?"}${s}` : path;
}

export async function request(method, path, { body, query, raw = false, signal, contentType, headers = {} } = {}) {
  const url = withQuery(path, query);
  const init = { method, credentials: "same-origin", headers: { Accept: "application/json", ...headers }, signal };
  if (body instanceof Blob || body instanceof ArrayBuffer) {
    init.body = body;
    init.headers["Content-Type"] = contentType || body.type || "application/octet-stream";
  } else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, init);
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError("Could not reach the server. Check your connection and try again.", 0, null);
  }
  if (res.status === 401) {
    toLogin();
    throw new ApiError("Please sign in again.", 401, null);
  }
  if (!res.ok) {
    let payload = null;
    try {
      payload = await res.json();
    } catch {
      // Not JSON, for example a proxy's error page.
    }
    throw new ApiError(detailText(payload?.detail, res.status), res.status, payload?.detail ?? null);
  }
  if (raw) return res;
  if (res.status === 204) return null;
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res.text();
}

export const api = {
  get: (path, opts) => request("GET", path, opts),
  post: (path, body, opts) => request("POST", path, { ...opts, body: body === undefined ? {} : body }),
  patch: (path, body, opts) => request("PATCH", path, { ...opts, body }),
  put: (path, body, opts) => request("PUT", path, { ...opts, body }),
  del: (path, opts) => request("DELETE", path, opts),
};

export const enc = encodeURIComponent;

// A file path inside a project, one encoded segment per directory.
export function encPath(path) {
  return String(path).split("/").map(encodeURIComponent).join("/");
}

// API paths for one project. They use the project's id, not its name, because
// the id never changes and is never ambiguous between two owners.
export function projectUrl(id, rest = "") {
  return `/api/projects/${enc(id)}${rest}`;
}
