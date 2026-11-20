(() => {
  "use strict";

  const STORAGE_KEY = "medisense.session";
  const MAX_REPORT = Number(document.body.dataset.maxReport);
  const MAX_UPLOAD_MB = Number(document.body.dataset.maxUploadMb);
  const MAX_ANSWER = Number(document.body.dataset.maxAnswer);
  const MAX_CHAT = Number(document.body.dataset.maxChat);
  const $ = (id) => document.getElementById(id);

  const panels = {
    input: $("panel-input"),
    wait: $("panel-wait"),
    questions: $("panel-questions"),
    result: $("panel-result"),
    chat: $("panel-chat"),
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
  let reportComplete = false;  // the Ask questions tab is enabled only when true
  let chatLoadedFor = null;    // which report's conversation is on screen
  let chatAbort = null;
  let chatBusy = false;
  let uploadName = null;       // the document whose text is in the report box
  let uploadPages = null;
  let uploadBusy = false;
  let uploadSeq = 0;           // bumped on reset so a late reply for an old upload is ignored

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
        // A FormData body needs the browser to set its own multipart Content-Type.
        headers: {
          ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
          ...(options.headers || {}),
        },
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
    resetChat();
    $("report").value = "";
    resetUpload();
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
        setChatEnabled(true);
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
  $("report").addEventListener("input", () => { updateCount(); refreshUploadStatus(); });

  // ---------- report document upload (PDF / DOCX) ----------
  // The file's text is extracted by the server and placed in the report box, where the patient can
  // check and edit it. "Analyze report" then sends it like any typed report, so there is one AI path.
  const fileInput = $("report-file");
  const dropzone = $("dropzone");

  function setUploadBusy(busy) {
    uploadBusy = busy;
    fileInput.disabled = busy;
    $("upload").classList.toggle("busy", busy);
    $("upload-spinner").hidden = !busy;
    setBusy($("btn-analyze"), busy);   // don't let a half-loaded report be submitted
  }

  function setUploadStatus(message) {
    $("upload-status-text").textContent = message || "";
    $("upload-status").hidden = !message;
  }

  // What the patient sees once a document is loaded; updated as they edit the text.
  function refreshUploadStatus() {
    if (!uploadName || uploadBusy) return;
    const pages = uploadPages ? `${uploadPages} page${uploadPages === 1 ? "" : "s"}, ` : "";
    const len = $("report").value.length;
    let message = `Loaded ${uploadName} (${pages}${len.toLocaleString()} characters). Check the text below, then choose Analyze report.`;
    if (len > MAX_REPORT) {
      message += ` It is longer than the ${MAX_REPORT.toLocaleString()}-character limit, so please shorten it first.`;
    }
    setUploadStatus(message);
  }

  function resetUpload() {
    uploadSeq += 1;
    uploadName = null;
    uploadPages = null;
    fileInput.value = "";
    setFieldError($("upload-error"), "");
    setUploadStatus("");
    setUploadBusy(false);
  }

  async function handleDocument(file) {
    setFieldError($("upload-error"), "");
    if (!/\.(pdf|docx)$/i.test(file.name)) {
      const legacy = /\.doc$/i.test(file.name);
      setFieldError($("upload-error"), legacy
        ? "Old Word (.doc) files aren't supported. Save it as .docx and try again."
        : "Only PDF (.pdf) and Word (.docx) files can be uploaded.");
      return;
    }
    if (file.size === 0) { setFieldError($("upload-error"), "This file is empty."); return; }
    if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
      setFieldError($("upload-error"), `This file is larger than ${MAX_UPLOAD_MB} MB.`);
      return;
    }
    const report = $("report");
    if (report.value.trim() && !window.confirm("Replace the text in the report box with the text from this document?")) {
      return;
    }

    const seq = ++uploadSeq;
    uploadName = null;
    setUploadStatus(`Reading ${file.name}…`);
    setUploadBusy(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const result = await api("/api/documents/extract", { method: "POST", body: form });
      if (seq !== uploadSeq) return;                 // the patient started over meanwhile
      report.value = result.text;
      report.removeAttribute("aria-invalid");
      setFieldError($("report-error"), "");
      updateCount();
      uploadName = file.name;
      uploadPages = result.pages;
      setUploadBusy(false);
      refreshUploadStatus();
      report.focus();
      report.setSelectionRange(0, 0);
      report.scrollTop = 0;
    } catch (err) {
      if (seq !== uploadSeq || err.handled) return;
      setUploadStatus("");
      setUploadBusy(false);
      setFieldError($("upload-error"), err.status === 413
        ? `This file is larger than ${MAX_UPLOAD_MB} MB.`
        : err.message);
    }
  }

  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = "";   // the File object stays valid; this lets the same file be chosen again
    if (file) handleDocument(file);
  });

  ["dragenter", "dragover"].forEach((type) => dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    if (!uploadBusy) dropzone.classList.add("dragover");
  }));
  ["dragleave", "dragend"].forEach((type) => dropzone.addEventListener(type, () => {
    dropzone.classList.remove("dragover");
  }));
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("dragover");
    if (uploadBusy) return;
    const files = event.dataTransfer ? event.dataTransfer.files : [];
    if (files.length > 1) {
      setFieldError($("upload-error"), "Please upload one document at a time.");
    } else if (files.length === 1) {
      handleDocument(files[0]);
    }
  });

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

  // ---------- chat about the report ----------
  const chatLog = $("chat-log");
  const chatInput = $("chat-input");
  const tabChat = $("tab-chat");
  const tabDownload = $("tab-download");

  function setChatEnabled(on) {
    reportComplete = on;
    tabChat.setAttribute("aria-disabled", String(!on));
    tabChat.title = on ? "" : "Available after your report is ready";
    tabDownload.disabled = !on;
  }

  function resetChat() {
    if (chatAbort) chatAbort.abort();
    chatAbort = null;
    chatBusy = false;
    chatLoadedFor = null;
    chatLog.replaceChildren();
    chatInput.value = "";
    setFieldError($("chat-error"), "");
    $("chat-starters").hidden = false;
    $("chat-left").textContent = "";
    setChatBusy(false);
    updateChatCount();
    setChatEnabled(false);
  }

  tabDownload.addEventListener("click", () => { if (reportComplete) showPanel("result", 3); });
  tabChat.addEventListener("click", () => {
    if (!reportComplete) return;
    showPanel("chat", 4);
    openChat();
  });

  // Plain text with a few safe extras (bold, bullet and numbered lists), built with DOM nodes,
  // never innerHTML: the model's output is untrusted.
  function addInline(parent, text) {
    text.split(/(\*\*[^*]+\*\*)/).forEach((part) => {
      if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) {
        const strong = document.createElement("strong");
        strong.textContent = part.slice(2, -2);
        parent.append(strong);
      } else if (part) {
        parent.append(document.createTextNode(part));
      }
    });
  }

  function renderRich(container, text) {
    container.replaceChildren();
    let list = null;
    for (const raw of text.split("\n")) {
      const line = raw.trimEnd();
      const bullet = line.match(/^\s*[-*\u2022]\s+(.*)$/);
      const number = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (bullet || number) {
        const tag = bullet ? "ul" : "ol";
        if (!list || list.tagName.toLowerCase() !== tag) {
          list = document.createElement(tag);
          container.append(list);
        }
        const li = document.createElement("li");
        addInline(li, (bullet || number)[1]);
        list.append(li);
      } else if (!line.trim()) {
        list = null;
      } else {
        list = null;
        const p = document.createElement("p");
        addInline(p, line.trim().replace(/^#{1,6}\s+/, ""));
        container.append(p);
      }
    }
  }

  function addMessage(who, text, { urgent = false } = {}) {
    const row = document.createElement("div");
    row.className = `msg msg-${who}${urgent ? " urgent" : ""}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    const label = document.createElement("span");
    label.className = "visually-hidden";
    label.textContent = who === "user" ? "You: " : "MediSense: ";
    const body = document.createElement("div");
    body.className = "bubble-body";
    bubble.append(label, body);
    row.append(bubble);
    chatLog.append(row);
    if (who === "user") body.textContent = text; else renderRich(body, text);
    return { row, body };
  }

  function showTyping(body) {
    const dots = document.createElement("span");
    dots.className = "typing";
    dots.setAttribute("aria-hidden", "true");
    dots.append(document.createElement("i"), document.createElement("i"), document.createElement("i"));
    const hidden = document.createElement("span");
    hidden.className = "visually-hidden";
    hidden.textContent = "MediSense is typing";
    body.replaceChildren(dots, hidden);
  }

  function addNote(body, text) {
    const note = document.createElement("p");
    note.className = "chat-note";
    note.textContent = text;
    body.append(note);
  }

  function scrollLog(force) {
    const nearBottom = chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight < 80;
    if (force || nearBottom) chatLog.scrollTop = chatLog.scrollHeight;
  }

  function setChatBusy(on) {
    $("chat-send").disabled = on;
    $("chat-stop").hidden = !on;
    $("chat-clear").disabled = on;
    document.querySelectorAll("#chat-starters .chip").forEach((chip) => { chip.disabled = on; });
    chatLog.setAttribute("aria-busy", String(on));
  }

  function updateChatCount() {
    $("chat-count").textContent = `${chatInput.value.length.toLocaleString()} / ${MAX_CHAT.toLocaleString()}`;
  }

  function showQuestionsLeft(n) {
    $("chat-left").textContent = n <= 5 ? `\u00b7 ${n} question${n === 1 ? "" : "s"} left for this report` : "";
  }

  async function openChat() {
    if (chatLoadedFor === sessionId) { chatInput.focus(); return; }
    chatLoadedFor = sessionId;
    try {
      const data = await api(`/api/reports/${sessionId}/chat`);
      chatLog.replaceChildren();
      data.messages.forEach((m) => {
        addMessage(m.role === "user" ? "user" : "bot", m.content, { urgent: m.kind === "emergency" });
      });
      $("chat-starters").hidden = data.messages.length > 0;
      showQuestionsLeft(data.questions_left);
      scrollLog(true);
    } catch (err) {
      chatLoadedFor = null;
      if (!err.handled) setFieldError($("chat-error"), err.message);
    }
    chatInput.focus();
  }

  function parseFrame(frame) {
    let event = "message";
    const dataLines = [];
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith(":")) continue;                       // comment / keep-alive
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    if (!dataLines.length) return null;
    try { return { event, data: JSON.parse(dataLines.join("\n")) }; } catch { return null; }
  }

  async function sendChat(raw) {
    const text = raw.trim();
    if (chatBusy || !sessionId || !text) return;
    setFieldError($("chat-error"), "");
    if (text.length > MAX_CHAT) {
      setFieldError($("chat-error"), `Please keep your question under ${MAX_CHAT.toLocaleString()} characters.`);
      return;
    }
    chatBusy = true;
    setChatBusy(true);
    $("chat-starters").hidden = true;
    chatInput.value = "";
    updateChatCount();
    const user = addMessage("user", text);
    const bot = addMessage("bot", "");
    showTyping(bot.body);
    scrollLog(true);

    const controller = new AbortController();
    chatAbort = controller;
    let answer = "";
    let accepted = false;     // true once the server has started answering
    let failed = "";
    let frameRender = false;
    const paint = () => {
      if (frameRender) return;
      frameRender = true;
      requestAnimationFrame(() => { frameRender = false; renderRich(bot.body, answer); scrollLog(false); });
    };

    try {
      const res = await fetch(`/api/reports/${sessionId}/chat`, {
        method: "POST",
        signal: controller.signal,
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ message: text }),
      });
      if (res.status === 401) { goToLogin("expired"); return; }
      if (!res.ok) {
        let body = null;
        try { body = await res.json(); } catch { /* not JSON */ }
        throw new Error(detailToText(body && body.detail));
      }
      accepted = true;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let cut;
        while ((cut = buffer.search(/\r?\n\r?\n/)) !== -1) {
          const frame = parseFrame(buffer.slice(0, cut));
          buffer = buffer.slice(cut).replace(/^\r?\n\r?\n/, "");
          if (!frame) continue;
          if (frame.event === "start" && frame.data.emergency) bot.row.classList.add("urgent");
          else if (frame.event === "token") { answer += frame.data.text; paint(); }
          else if (frame.event === "error") failed = frame.data.message;
          else if (frame.event === "done") showQuestionsLeft(frame.data.questions_left);
        }
      }
      renderRich(bot.body, answer);
      if (failed) {
        if (answer) addNote(bot.body, "The answer was cut short. " + failed);
        else { bot.body.textContent = failed; bot.row.classList.add("msg-error"); }
      }
    } catch (err) {
      if (err.name === "AbortError") {
        if (answer) { renderRich(bot.body, answer); addNote(bot.body, "Stopped."); }
        else bot.row.remove();
      } else if (!accepted) {
        // The server never took the question (limit, not ready, offline): undo and let them retry.
        user.row.remove();
        bot.row.remove();
        chatInput.value = text;
        updateChatCount();
        setFieldError($("chat-error"), err.message);
        if (!chatLog.children.length) $("chat-starters").hidden = false;
      } else {
        if (answer) { renderRich(bot.body, answer); addNote(bot.body, "The answer was cut short."); }
        else { bot.body.textContent = "Sorry, the connection was lost. Please try again."; bot.row.classList.add("msg-error"); }
      }
    } finally {
      chatBusy = false;
      chatAbort = null;
      setChatBusy(false);
      scrollLog(true);
      chatInput.focus();
    }
  }

  $("form-chat").addEventListener("submit", (event) => { event.preventDefault(); sendChat(chatInput.value); });
  chatInput.addEventListener("input", updateChatCount);
  chatInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("form-chat").requestSubmit();
    }
  });
  document.querySelectorAll("#chat-starters .chip").forEach((chip) => {
    chip.addEventListener("click", () => sendChat(chip.dataset.q));
  });
  $("chat-stop").addEventListener("click", () => { if (chatAbort) chatAbort.abort(); });
  $("chat-clear").addEventListener("click", async () => {
    if (chatBusy || !sessionId) return;
    if (!window.confirm("Clear this conversation? This can't be undone.")) return;
    try {
      await api(`/api/reports/${sessionId}/chat`, { method: "DELETE" });
      chatLog.replaceChildren();
      $("chat-starters").hidden = false;
      $("chat-left").textContent = "";
      setFieldError($("chat-error"), "");
    } catch (err) {
      if (!err.handled) setFieldError($("chat-error"), err.message);
    }
  });

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
