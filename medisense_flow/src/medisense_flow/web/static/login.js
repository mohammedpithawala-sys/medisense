(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const auth = $("auth");
  const SESSION_KEY = "medisense.session";

  // Short notices the app can ask this page to show (only these exact codes are accepted).
  const NOTICES = {
    signed_out: "You've been signed out.",
    expired: "Your session ended. Please sign in again.",
    deleted: "Your record, reports and summaries were deleted.",
  };

  const TEXT = {
    in: ["Welcome back", "Sign in with your Patient ID and password."],
    up: ["Create your account", "Create your patient record in under a minute."],
  };
  const LABEL = { "in-submit": "Sign in", "up-submit": "Create account" };

  // ---------- helpers ----------
  function detailToText(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length) {
      return detail.map((d) => String(d.msg).replace(/^Value error, /, "")).join(" ");
    }
    return "Something went wrong. Please try again.";
  }

  async function api(path, body) {
    let res;
    try {
      res = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch {
      throw new Error("Can't reach the server. Check your connection and try again.");
    }
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    if (!res.ok) throw new Error(detailToText(data && data.detail));
    return data;
  }

  function showError(id, message) {
    const el = $(id);
    el.textContent = message || "";
    el.hidden = !message;
  }

  function busy(buttonId, on) {
    const button = $(buttonId);
    button.disabled = on;
    if (on) {
      const spinner = document.createElement("span");
      spinner.className = "spin";
      spinner.setAttribute("aria-hidden", "true");
      button.replaceChildren(spinner, document.createTextNode("\u00a0Please wait"));
    } else {
      button.textContent = LABEL[buttonId];
    }
  }

  function setInvalid(input, invalid) {
    if (invalid) input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
  }

  function goToApp() {
    try { sessionStorage.removeItem(SESSION_KEY); } catch { /* storage unavailable */ }
    window.location.assign("/");
  }

  // ---------- Sign in / Create account switch ----------
  function setMode(mode) {
    const signIn = mode === "in";
    auth.dataset.m = mode;
    $("tab-in").setAttribute("aria-selected", String(signIn));
    $("tab-up").setAttribute("aria-selected", String(!signIn));
    $("form-in").hidden = !signIn;
    $("form-up").hidden = signIn;
    $("atitle").textContent = TEXT[mode][0];
    $("asub").textContent = TEXT[mode][1];
    showError("in-error", "");
    showError("up-error", "");
  }
  $("tab-in").addEventListener("click", () => setMode("in"));
  $("tab-up").addEventListener("click", () => setMode("up"));
  $("go-up").addEventListener("click", (e) => { e.preventDefault(); setMode("up"); });
  $("go-in").addEventListener("click", (e) => { e.preventDefault(); setMode("in"); });

  // ---------- Show / Hide password ----------
  document.querySelectorAll(".pwtoggle").forEach((button) => {
    button.addEventListener("click", () => {
      const input = $(button.dataset.target);
      const reveal = input.type === "password";
      input.type = reveal ? "text" : "password";
      button.textContent = reveal ? "Hide" : "Show";
      button.setAttribute("aria-pressed", String(reveal));
    });
  });

  // ---------- Sign in ----------
  $("form-in").addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = $("in-code");
    const password = $("in-password");
    showError("in-error", "");
    setInvalid(code, !code.value.trim());
    setInvalid(password, !password.value);
    if (!code.value.trim() || !password.value) {
      showError("in-error", "Enter your Patient ID and password.");
      (code.value.trim() ? password : code).focus();
      return;
    }
    busy("in-submit", true);
    try {
      await api("/api/auth/login", { patient_code: code.value.trim(), password: password.value });
      goToApp();
    } catch (err) {
      showError("in-error", err.message);
      busy("in-submit", false);
    }
  });

  // ---------- Create account ----------
  $("form-up").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = $("up-name");
    const password = $("up-password");
    const confirm = $("up-confirm");
    const consent = $("up-consent");
    [name, password, confirm, consent].forEach((el) => setInvalid(el, false));
    showError("up-error", "");

    const fail = (el, message) => { setInvalid(el, true); showError("up-error", message); el.focus(); };
    if (name.value.trim().length < 2) return fail(name, "Enter your full name.");
    if (password.value.length < 8) return fail(password, "Password must be at least 8 characters.");
    if (password.value !== confirm.value) return fail(confirm, "Passwords do not match.");
    if (!consent.checked) return fail(consent, "Please agree to the saving of your records to continue.");

    busy("up-submit", true);
    try {
      const profile = await api("/api/auth/register", {
        name: name.value.trim(), password: password.value, consent: true,
      });
      password.value = confirm.value = "";
      $("new-code").textContent = profile.patient_code;
      $("copy-note").textContent = "";
      $("form-card").hidden = true;
      $("code-card").hidden = false;
      $("code-title").focus();
    } catch (err) {
      showError("up-error", err.message);
    } finally {
      busy("up-submit", false);
    }
  });

  // ---------- Patient ID screen ----------
  $("btn-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("new-code").textContent);
      $("copy-note").textContent = "Copied. Keep it somewhere safe.";
    } catch {
      const range = document.createRange();
      range.selectNodeContents($("new-code"));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      $("copy-note").textContent = "Press Ctrl+C (or Cmd+C) to copy the selected ID.";
    }
  });
  $("btn-continue").addEventListener("click", goToApp);

  // ---------- start-up ----------
  const code = new URLSearchParams(window.location.search).get("msg");
  if (code && Object.prototype.hasOwnProperty.call(NOTICES, code)) {
    $("anotice").textContent = NOTICES[code];
    $("anotice").hidden = false;
  }
  if (window.location.search) {
    window.history.replaceState(null, "", window.location.pathname + window.location.hash);
  }
  setMode(window.location.hash === "#create" ? "up" : "in");
  window.addEventListener("hashchange", () => {
    if (window.location.hash === "#create") setMode("up");
    else if (window.location.hash === "#signin") setMode("in");
  });
})();
