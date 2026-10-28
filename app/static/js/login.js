// The sign-in page asks for the email first and then for the emailed code. The
// magic link in the same email works too and lands on ?next= directly.

import { detailText } from "./api.js";
import { flashBox } from "./ui.js";

// Same-site paths only ("/p/abc" yes, "//evil.example" and "https://…" no).
// The server checks it again.
const NEXT = (() => {
  const n = new URLSearchParams(location.search).get("next") || "";
  return n.startsWith("/") && !n.startsWith("//") && !n.includes("\\") ? n.slice(0, 500) : "/";
})();

// Inside the desktop app its preload script exposes window.meertexDesktop. The
// emailed link is then a meertex:// deep link, so the session lands in the app
// and not in the system browser (electron/README.md). The code works anywhere.
const CLIENT = window.meertexDesktop ? "desktop" : "web";

const $ = (id) => document.getElementById(id);
const stepEmail = $("step-email");
const stepCode = $("step-code");
const emailForm = $("email-form");
const email = $("email");
const emailSubmit = $("email-submit");
const codeForm = $("code-form");
const code = $("code");
const codeSubmit = $("code-submit");

function message(which, text, kind = "error") {
  const host = $(`message-${which}`);
  host.replaceChildren();
  if (text) host.append(flashBox(text, kind));
}

// The sign-in routes are not behind a session, so they get their own small
// fetch instead of the app's wrapper, which would bounce a 401 back here.
async function post(url, body) {
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error("Could not reach the server. Check your connection and try again.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(detailText(data.detail, res.status));
  return data;
}

async function requestCode(address) {
  await post("/api/auth/login", { email: address, next: NEXT, client: CLIENT });
  $("sent-to").textContent = address;
  stepEmail.hidden = true;
  stepCode.hidden = false;
  codeForm.reset();
  code.focus();
}

emailForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const address = email.value.trim();
  if (!address || !email.checkValidity()) {
    message("email", "Enter the email address you sign in with.");
    email.focus();
    return;
  }
  emailSubmit.disabled = true;
  emailSubmit.textContent = "Sending…";
  message("email", "");
  try {
    await requestCode(address);
  } catch (err) {
    message("email", err.message);
  } finally {
    emailSubmit.disabled = false;
    emailSubmit.textContent = "Continue";
  }
});

code.addEventListener("input", () => {
  code.value = code.value.toUpperCase().replace(/\s+/g, "");
});

codeForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const value = code.value.trim().toUpperCase();
  if (!value) {
    message("code", "Enter the code from the email.");
    code.focus();
    return;
  }
  codeSubmit.disabled = true;
  codeSubmit.textContent = "Checking…";
  message("code", "");
  try {
    await post("/api/auth/verify-code", { email: email.value.trim(), code: value });
    codeSubmit.textContent = "Signed in";
    location.replace(NEXT);
  } catch (err) {
    message("code", err.message);
    code.select();
    codeSubmit.disabled = false;
    codeSubmit.textContent = "Sign in";
  }
});

$("resend").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    await requestCode(email.value.trim());
    message("code", "We sent a new code. The old one no longer works.", "success");
  } catch (err) {
    message("code", err.message);
  } finally {
    btn.disabled = false;
  }
});

$("other-email").addEventListener("click", () => {
  stepCode.hidden = true;
  stepEmail.hidden = false;
  message("code", "");
  email.focus();
  email.select();
});

// Already signed in in another tab? Straight on.
fetch("/api/auth/me", { credentials: "same-origin" })
  .then((r) => { if (r.ok) location.replace(NEXT); })
  .catch(() => {});
