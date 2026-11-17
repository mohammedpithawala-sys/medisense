(() => {
  "use strict";

  const STORAGE_KEY = "medisense.session";
  const MAX_REPORT = Number(document.body.dataset.maxReport);
  const MAX_ANSWER = Number(document.body.dataset.maxAnswer);
  const $ = (id) => document.getElementById(id);

  const panels = {
    input: $("panel-input"),
    wait: $("panel-wait"),
    questions: $("panel-questions"),
    result: $("panel-result"),
    delete: $("panel-delete"),
    error: $("panel-error"),
  };
  const stepper = $("stepper");
  const stepItems = Array.from(stepper.querySelectorAll("li"));

  let profile = null;          // who is signed in
  let sessionId = null;        // the report being worked on
  let pollTimer = null;
  let pollDelay = 2000;
  let pollFailures = 0;
  let questionsShownFor = null;

  // ---------- helpers ----------
  // step: 1-3 shows the progress bar; 0 hides it (delete and error screens)
  function showPanel(name, step = 0) {
    Object.entries(panels).forEach(([key, el]) => { el.hidden = key !== name; });
    stepper.hidden = !step;
    stepItems.forEach((li) => {
      const n = Number(li.dataset.step);
      li.classList.toggle("done", n < step);
      if (n === step) li.setAttribute("aria-current", "step");
      else li.removeAttribute("aria-current");
    });
    const heading = panels[name].querySelector("h1, h2");
    if (heading) heading.focus();
  }

  // Sign-in lives on its own page. `notice` is one of the codes that page understands.
  function goToLogin(notice) {
    stopPolling();
    try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* storage unavailable */ }
    window.location.replace(notice ? `/login?msg=${notice}` : "/login");
  }

  function detailToText(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length) {
      // FastAPI validation errors: [{loc, msg, ...}]
      return detail.map((d) => String(d.msg).replace(/^Value error, /, "")).join(" ");
    }
    return "Something went wrong. Please try again.";
  }

  // A 401 anywhere (except the one start-up check) means the login ran out.
  async function api(path, options = {}) {
    let res;
    try {
      res = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      });
    } catch {
      const err = new Error("Can't reach the server. Check your connection and try again.");
      err.network = true;
      throw err;
    }
    let body = null;
    try { body = await res.json(); } catch { /* empty or non-JSON body (e.g. 204) */ }
    if (!res.ok) {
      const err = new Error(detailToText(body && body.detail));
      err.status = res.status;
      if (res.status === 401 && !options.expect401) {
        err.handled = true;
        goToLogin("expired");
      }
      throw err;
    }
    return body;
  }

  function setBusy(button, busy) { button.disabled = busy; }

  function setFieldError(el, message) {
    el.textContent = message || "";
    el.hidden = !message;
  }

  function showError(message) {
    stopPolling();
    $("error-text").textContent = message;
    showPanel("error", 0);
  }

  // ---------- account state ----------
  function clearWork() {
    stopPolling();
    sessionId = null;
    questionsShownFor = null;
    try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* storage unavailable */ }
    $("report").value = "";
    $("questions").replaceChildren();
    updateCount();
    setBusy($("btn-analyze"), false);
    setBusy($("btn-answers"), false);
  }

  function enterApp(p) {
    profile = p;
    $("user-name").textContent = p.name;
    $("userbar").hidden = false;
    const n = p.earlier_reports;
    $("input-lede").textContent = n > 0
      ? `Hello, ${p.name}. We'll also look at your ${n} earlier report${n === 1 ? "" : "s"}.`
      : `Hello, ${p.name}. We'll review it, ask you three follow-up questions, then prepare a PDF report.`;
  }

  // ---------- sign out / delete my data ----------
  $("btn-logout").addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST" }); } catch { /* leaving anyway */ }
    goToLogin("signed_out");
  });

  $("btn-open-delete").addEventListener("click", () => {
    stopPolling();
    $("delete-password").value = "";
    setFieldError($("delete-error"), "");
    showPanel("delete", 0);
  });
  $("btn-delete-cancel").addEventListener("click", () => {
    if (sessionId) resumeWork(); else showPanel("input", 1);
  });

  $("form-delete").addEventListener("submit", async (event) => {
    event.preventDefault();
    const err = $("delete-error");
    const password = $("delete-password").value;
    if (!password) { setFieldError(err, "Type your password to confirm."); return; }
    setBusy($("btn-delete"), true);
    try {
      await api("/api/auth/delete-account", { method: "POST", body: JSON.stringify({ password }) });
      goToLogin("deleted");
    } catch (e) {
      if (!e.handled) setFieldError(err, e.message);
    } finally {
      setBusy($("btn-delete"), false);
    }
  });

  // ---------- polling ----------
  function stopPolling() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  }

  function startPolling() {
    stopPolling();
    pollDelay = 2000;
    pollFailures = 0;
    pollTimer = setTimeout(tick, pollDelay);
  }

  async function tick() {
    if (!sessionId) return;
    try {
      const view = await api(`/api/sessions/${sessionId}`);
      pollFailures = 0;
      render(view);
      if (view.status === "analyzing" || view.status === "processing") {
        pollDelay = Math.min(pollDelay * 1.2, 5000);
        pollTimer = setTimeout(tick, pollDelay);
      }
    } catch (err) {
      if (err.handled) return;
      if (err.status === 404) {
        clearWork();
        showError("That report is no longer available. Please start again.");
        return;
      }
      pollFailures += 1;
      if (pollFailures >= 5) { showError(err.message); return; }
      pollTimer = setTimeout(tick, 4000);
    }
  }

  // ---------- rendering ----------
  function showPanelOnce(name, step) {
    if (panels[name].hidden) showPanel(name, step);
  }

  function render(view) {
    switch (view.status) {
      case "analyzing":
        $("h-wait").textContent = "Analyzing your report";
        $("wait-text").textContent = "This usually takes a minute or two. Keep this page open.";
        showPanelOnce("wait", 1);
        break;
      case "awaiting_answers":
        buildQuestions(view);
        showPanelOnce("questions", 2);
        break;
      case "processing":
        $("h-wait").textContent = "Preparing your report";
        $("wait-text").textContent = "Reviewing your answers and building the PDF. This usually takes a minute or two.";
        showPanelOnce("wait", 2);
        break;
      case "complete": {
        stopPolling();
        $("download").href = view.download_url;
        const box = $("summary-box");
        $("summary-text").textContent = view.summary || "";
        box.hidden = !view.summary;
        const score = $("score");
        if (view.score != null) {
          score.textContent = `Self-check score: ${view.score} out of 10.`;
          score.hidden = false;
        } else {
          score.hidden = true;
        }
        showPanelOnce("result", 3);
        break;
      }
      case "failed":
        showError(view.error || "Something went wrong. Please try again.");
        break;
    }
  }

  function buildQuestions(view) {
    if (questionsShownFor === view.session_id) return;
    questionsShownFor = view.session_id;
    const note = $("history-note");
    if (view.history_count > 0) {
      const n = view.history_count;
      note.textContent = `We also looked at your ${n} earlier report${n === 1 ? "" : "s"}.`;
      note.hidden = false;
    } else {
      note.hidden = true;
    }
    const container = $("questions");
    container.replaceChildren();
    view.questions.forEach((text, i) => {
      const wrap = document.createElement("div");
      wrap.className = "q";
      const label = document.createElement("label");
      label.htmlFor = `answer-${i}`;
      label.textContent = text;                 // textContent: model output is untrusted
      const area = document.createElement("textarea");
      area.id = `answer-${i}`;
      area.rows = 3;
      area.maxLength = MAX_ANSWER;
      area.required = true;
      wrap.append(label, area);
      container.append(wrap);
    });
    setFieldError($("answers-error"), "");
    setBusy($("btn-answers"), false);
  }

  // ---------- report form ----------
  function updateCount() {
    const len = $("report").value.length;
    $("report-count").textContent = `${len.toLocaleString()} / ${MAX_REPORT.toLocaleString()} characters`;
  }
  $("report").addEventListener("input", updateCount);

  $("form-report").addEventListener("submit", async (event) => {
    event.preventDefault();
    const field = $("report");
    const text = field.value.trim();
    setFieldError($("report-error"), "");
    field.removeAttribute("aria-invalid");
    if (text.length < 10) {
      field.setAttribute("aria-invalid", "true");
      setFieldError($("report-error"), "Add a few more details so there's enough to review.");
      field.focus();
      return;
    }
    if (text.length > MAX_REPORT) {
      field.setAttribute("aria-invalid", "true");
      setFieldError($("report-error"), `Please shorten this to ${MAX_REPORT.toLocaleString()} characters or fewer.`);
      field.focus();
      return;
    }
    setBusy($("btn-analyze"), true);
    try {
      const created = await api("/api/sessions", { method: "POST", body: JSON.stringify({ report: text }) });
      sessionId = created.session_id;
      try { sessionStorage.setItem(STORAGE_KEY, sessionId); } catch { /* ignore */ }
      showPanel("wait", 1);
      startPolling();
    } catch (err) {
      if (!err.handled) setFieldError($("report-error"), err.message);
    } finally {
      setBusy($("btn-analyze"), false);
    }
  });

  $("form-answers").addEventListener("submit", async (event) => {
    event.preventDefault();
    const fields = Array.from($("questions").querySelectorAll("textarea"));
    const answers = fields.map((f) => f.value.trim());
    let firstEmpty = null;
    fields.forEach((f, i) => {
      const empty = answers[i] === "";
      if (empty) f.setAttribute("aria-invalid", "true"); else f.removeAttribute("aria-invalid");
      if (empty && !firstEmpty) firstEmpty = f;
    });
    if (firstEmpty) {
      setFieldError($("answers-error"), "Answer all three questions to continue.");
      firstEmpty.focus();
      return;
    }
    setFieldError($("answers-error"), "");
    setBusy($("btn-answers"), true);
    try {
      await api(`/api/sessions/${sessionId}/answers`, { method: "POST", body: JSON.stringify({ answers }) });
      showPanel("wait", 2);
      $("h-wait").textContent = "Preparing your report";
      $("wait-text").textContent = "Reviewing your answers and building the PDF. This usually takes a minute or two.";
      startPolling();
    } catch (err) {
      if (!err.handled) {
        setFieldError($("answers-error"), err.message);
        setBusy($("btn-answers"), false);
      }
    }
  });

  function startNewReport() {
    clearWork();
    showPanel("input", 1);
  }
  $("btn-restart").addEventListener("click", startNewReport);
  $("btn-error-restart").addEventListener("click", startNewReport);

  // ---------- resume after sign-in or a refresh ----------
  async function resumeWork() {
    let saved = null;
    try { saved = sessionStorage.getItem(STORAGE_KEY); } catch { /* ignore */ }
    if (!saved) { showPanel("input", 1); return; }
    sessionId = saved;
    try {
      const view = await api(`/api/sessions/${sessionId}`);
      questionsShownFor = null;
      Object.values(panels).forEach((el) => { el.hidden = true; });  // let render() choose the panel
      render(view);
      if (view.status === "analyzing" || view.status === "processing") startPolling();
    } catch (err) {
      if (err.handled) return;
      clearWork();
      showPanel("input", 1);
    }
  }

  async function boot() {
    updateCount();
    try {
      const p = await api("/api/auth/me", { expect401: true });
      enterApp(p);
      await resumeWork();
    } catch (err) {
      if (err.status === 401) goToLogin();
      else showError(err.message);
    }
  }
  boot();
})();
