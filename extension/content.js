// ArenaX 에이전트 하단 바 (content script v3 — 임무 관리)
// - 활성화(agentEnabled) 상태이면 우측에 런처(🤖) 표시, 드래그로 위아래 이동 가능
// - 런처 클릭 → 질문창 열기, 바 닫기 → 런처 복귀
// - 임무는 storage(agentTasks)에 저장 → 모든 탭이 같은 임무 목록을 공유
// - 각 임무 카드에 취소/수정/중단/재개 버튼 표시
// - 런처 최소화 상태에서 임무 완료/오류/중단 시 말풍선 알림 표시
// - "에이전트 종료"는 agentEnabled를 false로 설정해 모든 탭에서 숨김
(() => {
  if (window.__arenaxAgentInjected) return;
  window.__arenaxAgentInjected = true;

  // ---- Shadow DOM 컨테이너 ----
  const host = document.createElement("div");
  host.id = "__arenax_agent_host";
  Object.assign(host.style, {
    position: "fixed", left: "0", right: "0", bottom: "0",
    width: "100%", zIndex: "2147483647", pointerEvents: "none",
  });
  const shadow = host.attachShadow({ mode: "open" });

  // ---- CSS ----
  const CSS = `
  :host { all: initial; }
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Malgun Gothic", sans-serif; }

  /* 런처 버튼 */
  .launcher {
    pointer-events: auto; position: fixed; right: 16px;
    width: 52px; height: 52px; border-radius: 50%; border: 0; cursor: grab;
    background: #7c3aed; color: #fff; font-size: 22px; line-height: 1;
    box-shadow: 0 6px 20px rgba(124,58,237,0.45);
    display: none; touch-action: none; user-select: none;
  }
  .launcher.show { display: block; }
  .launcher:active { cursor: grabbing; }
  .launcher:hover { background: #6d28d9; }

  /* 말풍선 — 런처 위에 표시 */
  .bubble {
    --bg: #1f2937;
    pointer-events: none; position: fixed; right: 74px;
    background: var(--bg); color: #fff;
    padding: 6px 12px; border-radius: 10px;
    font-size: 12px; font-weight: 600;
    white-space: nowrap; max-width: 230px;
    overflow: hidden; text-overflow: ellipsis;
    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    display: none;
  }
  .bubble.show { display: block; }
  .bubble::after {
    content: ''; position: absolute; right: -7px; top: 50%; transform: translateY(-50%);
    border: 7px solid transparent; border-left-color: var(--bg); border-right-width: 0;
  }
  .bubble.success { --bg: #065f46; }
  .bubble.error   { --bg: #7f1d1d; }
  .bubble.handoff { --bg: #78350f; }
  .bubble.cancelled { --bg: #374151; }

  /* 하단 바 */
  .wrap { pointer-events: none; display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 0 12px 12px; }
  .bar {
    pointer-events: auto; width: 100%; max-width: 940px;
    background: #fff; color: #111827;
    border: 1px solid #e5e7eb; border-radius: 16px 16px 12px 12px;
    box-shadow: 0 -6px 30px rgba(0,0,0,0.18);
    display: flex; flex-direction: column; overflow: hidden;
  }
  .bar[hidden] { display: none; }

  /* 헤더 */
  .head { display: flex; align-items: center; gap: 8px; padding: 10px 14px; border-bottom: 1px solid #f1f1f4; background: #faf5ff; }
  .title { font-size: 14px; font-weight: 800; color: #6d28d9; flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .head-btns { display: flex; gap: 6px; flex: 0 0 auto; }
  .icon-btn { border: 1px solid #e5e7eb; background: #fff; border-radius: 8px; cursor: pointer; font-size: 12px; padding: 4px 9px; color: #6b7280; white-space: nowrap; }
  .icon-btn:hover { background: #f3f4f6; }
  .end-btn { border: 1px solid #fecaca; background: #fef2f2; color: #b91c1c; border-radius: 8px; cursor: pointer; font-size: 12px; font-weight: 700; padding: 4px 10px; white-space: nowrap; }
  .end-btn:hover { background: #fee2e2; }

  /* 서버 설정 */
  .settings { display: none; padding: 8px 14px; border-bottom: 1px solid #f1f1f4; gap: 6px; }
  .settings.open { display: flex; }
  .settings input { flex: 1; padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 8px; font-size: 12px; }
  .settings button { padding: 6px 12px; border: 0; background: #7c3aed; color: #fff; border-radius: 8px; font-size: 12px; font-weight: 700; cursor: pointer; }

  /* 임무 목록 */
  .tasks { max-height: 310px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; padding: 10px 14px; }
  .tasks:empty { display: none; }
  .empty { font-size: 13px; color: #9ca3af; padding: 4px 0; }

  /* 임무 카드 */
  .task-card { border: 1px solid #e5e7eb; border-radius: 10px; overflow: hidden; }
  .task-card[data-status="running"] { border-left: 3px solid #3b82f6; }
  .task-card[data-status="paused"]  { border-left: 3px solid #f59e0b; }
  .task-card[data-status="success"] { border-left: 3px solid #10b981; }
  .task-card[data-status="error"]   { border-left: 3px solid #ef4444; }
  .task-card[data-status="handoff"] { border-left: 3px solid #f59e0b; }
  .task-card[data-status="cancelled"] { border-left: 3px solid #9ca3af; }

  .task-head { display: flex; align-items: flex-start; gap: 8px; padding: 8px 10px 4px; }
  .task-text { flex: 1 1 auto; font-size: 13px; font-weight: 700; color: #111827; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .task-badge { flex: 0 0 auto; font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 20px; white-space: nowrap; }
  .task-badge.running  { background: #dbeafe; color: #1d4ed8; }
  .task-badge.paused   { background: #fef3c7; color: #b45309; }
  .task-badge.success  { background: #d1fae5; color: #065f46; }
  .task-badge.error    { background: #fee2e2; color: #b91c1c; }
  .task-badge.handoff  { background: #fef3c7; color: #b45309; }
  .task-badge.cancelled { background: #f3f4f6; color: #6b7280; }

  .task-latest { font-size: 12px; line-height: 1.5; color: #4b5563; padding: 0 10px 4px; overflow-wrap: anywhere; white-space: pre-wrap; }
  .task-result { font-size: 12px; line-height: 1.5; padding: 4px 10px 6px; overflow-wrap: anywhere; white-space: pre-wrap; font-weight: 600; }
  .task-result.success  { color: #047857; }
  .task-result.error    { color: #b91c1c; }
  .task-result.handoff  { color: #b45309; }
  .task-result.cancelled { color: #6b7280; }

  /* 임무 버튼 */
  .task-btns { display: flex; gap: 5px; padding: 4px 10px 8px; flex-wrap: wrap; }
  .tbtn { border: 1px solid #e5e7eb; background: #f9fafb; border-radius: 6px; cursor: pointer; font-size: 11px; font-weight: 700; padding: 3px 9px; white-space: nowrap; }
  .tbtn:hover { filter: brightness(0.95); }
  .tbtn.pause-btn  { border-color: #fbbf24; color: #b45309; }
  .tbtn.resume-btn { border-color: #34d399; background: #ecfdf5; color: #065f46; }
  .tbtn.cancel-btn { border-color: #fca5a5; color: #b91c1c; }
  .tbtn.edit-btn   { border-color: #93c5fd; color: #1d4ed8; }

  /* 확인 요청 */
  .confirm { display: none; padding: 10px 14px; border-top: 1px solid #f1f1f4; background: #fffbeb; flex-direction: column; gap: 8px; }
  .confirm.open { display: flex; }
  .confirm-msg { font-size: 13px; color: #92400e; line-height: 1.5; white-space: pre-wrap; }
  .confirm-btns { display: flex; gap: 8px; }
  .confirm-btns button { flex: 1; padding: 8px; border: 0; border-radius: 8px; font-size: 13px; font-weight: 700; cursor: pointer; }
  .approve-btn2 { background: #16a34a; color: #fff; }
  .reject-btn2  { background: #e5e7eb; color: #374151; }

  /* 입력 */
  .input-row { display: flex; gap: 8px; padding: 10px 14px; border-top: 1px solid #f1f1f4; align-items: flex-end; }
  .input-row textarea {
    flex: 1; resize: none; overflow: hidden; min-height: 40px; max-height: 120px;
    padding: 10px 12px; border: 1px solid #d1d5db; border-radius: 10px;
    font-size: 14px; line-height: 1.5; color: #111827; background: #fff;
  }
  .input-row textarea:focus { outline: none; border-color: #7c3aed; box-shadow: 0 0 0 3px rgba(124,58,237,0.15); }
  .input-row textarea:disabled { background: #f3f4f6; color: #9ca3af; }
  .send-btn { align-self: stretch; min-width: 64px; border: 0; border-radius: 10px; background: #7c3aed; color: #fff; font-size: 14px; font-weight: 800; cursor: pointer; }
  .send-btn:hover:not(:disabled) { background: #6d28d9; }
  .send-btn:disabled { background: #c4b5fd; cursor: not-allowed; }
  `;

  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(CSS);
    shadow.adoptedStyleSheets = [sheet];
  } catch (e) {
    const s = document.createElement("style");
    s.textContent = CSS;
    shadow.appendChild(s);
  }

  // ---- HTML ----
  const root = document.createElement("div");
  root.innerHTML = `
    <button class="launcher" id="ax-launcher" title="ArenaX 에이전트 (드래그로 이동)">🤖</button>
    <div class="bubble" id="ax-bubble"></div>
    <div class="wrap">
      <div class="bar" id="ax-bar" hidden>
        <div class="head">
          <span class="title">🤖 ArenaX 에이전트</span>
          <div class="head-btns">
            <button class="icon-btn" id="ax-clear" title="완료된 임무 삭제">기록 삭제</button>
            <button class="icon-btn" id="ax-gear"  title="서버 설정">설정</button>
            <button class="icon-btn" id="ax-min"   title="최소화">─</button>
            <button class="end-btn"  id="ax-end"   title="에이전트 종료">에이전트 종료</button>
          </div>
        </div>
        <div class="settings" id="ax-settings">
          <input id="ax-server" type="text" placeholder="https://arenax-4812.onrender.com" />
          <button id="ax-save">저장</button>
        </div>
        <div class="tasks" id="ax-tasks"></div>
        <div class="confirm" id="ax-confirm">
          <div class="confirm-msg" id="ax-confirm-msg"></div>
          <div class="confirm-btns">
            <button class="approve-btn2" id="ax-approve">승인하고 진행</button>
            <button class="reject-btn2"  id="ax-reject">취소</button>
          </div>
        </div>
        <div class="input-row">
          <textarea id="ax-input" rows="1" placeholder="이 화면에서 할 일을 지시하세요. 예: 검색창에 '서울 날씨' 입력 후 검색"></textarea>
          <button class="send-btn" id="ax-send">전송</button>
        </div>
      </div>
    </div>
  `;
  shadow.appendChild(root);

  // ---- 엘리먼트 refs ----
  const $ = id => shadow.getElementById(id);
  const launcher    = $("ax-launcher");
  const bubble      = $("ax-bubble");
  const bar         = $("ax-bar");
  const clearBtn    = $("ax-clear");
  const gearBtn     = $("ax-gear");
  const minBtn      = $("ax-min");
  const endBtn      = $("ax-end");
  const settings    = $("ax-settings");
  const serverInput = $("ax-server");
  const saveBtn     = $("ax-save");
  const tasksEl     = $("ax-tasks");
  const confirmBox  = $("ax-confirm");
  const confirmMsg  = $("ax-confirm-msg");
  const approveBtn  = $("ax-approve");
  const rejectBtn   = $("ax-reject");
  const input       = $("ax-input");
  const sendBtnEl   = $("ax-send");

  document.documentElement.appendChild(host);

  // ---- 상태 ----
  let enabled       = false;
  let pendingConfirm = null;
  let launcherBottom = 96;
  let bubbleTimer   = null;

  // ---- 임무 상태 레이블 ----
  const STATUS = {
    running:   { label: "● 실행중",  cls: "running"   },
    paused:    { label: "⏸ 중단됨", cls: "paused"    },
    success:   { label: "✅ 완료",   cls: "success"   },
    error:     { label: "❌ 오류",   cls: "error"     },
    handoff:   { label: "🟡 전달",   cls: "handoff"   },
    cancelled: { label: "🛑 취소됨", cls: "cancelled" },
  };

  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // ---- 표시 상태 ----
  function render() {
    launcher.classList.toggle("show", enabled && bar.hidden);
  }
  function showBar(show) {
    bar.hidden = !show;
    render();
    if (show) input.focus();
  }
  function applyEnabled(v) {
    enabled = v;
    if (!v) bar.hidden = true;
    render();
  }
  async function setEnabled(v) {
    try { await chrome.storage.local.set({ agentEnabled: v }); } catch {}
    applyEnabled(v);
  }
  function applyLauncherPos() {
    launcher.style.bottom = launcherBottom + "px";
    bubble.style.bottom   = (launcherBottom + 58) + "px";
  }

  // ---- 말풍선 ----
  const BUBBLE_KIND_MAP = {
    success: "success", error: "error", handoff: "handoff",
    cancelled: "cancelled", paused: "paused",
  };
  function showBubble(text, kind) {
    const cls = BUBBLE_KIND_MAP[kind] || "";
    bubble.textContent = text;
    bubble.className   = "bubble show" + (cls ? " " + cls : "");
    bubble.style.display = "block";
    clearTimeout(bubbleTimer);
    bubbleTimer = setTimeout(() => { bubble.className = "bubble"; }, 5000);
  }

  // ---- 임무 렌더링 ----
  function renderTasks(tasks) {
    tasksEl.innerHTML = "";
    if (!tasks || tasks.length === 0) {
      tasksEl.innerHTML = '<div class="empty">아직 임무가 없습니다. 아래에 지시사항을 입력하세요.</div>';
      return;
    }
    for (const task of tasks) {
      const s = STATUS[task.status] || STATUS.error;
      const card = document.createElement("div");
      card.className = "task-card";
      card.dataset.id     = task.id;
      card.dataset.status = task.status;

      // 최신 단계 (실행/중단 중만 표시)
      const latestStep = task.steps && task.steps.length > 0 ? task.steps[task.steps.length - 1] : null;
      const showLatest = (task.status === "running" || task.status === "paused") && latestStep;
      const latestHtml = showLatest
        ? `<div class="task-latest">${esc(latestStep.text)}</div>`
        : "";

      // 결과 (완료/오류/취소 등)
      const resultHtml = task.result
        ? `<div class="task-result ${esc(s.cls)}">${esc(task.result)}</div>`
        : "";

      // 버튼
      let btnsHtml = "";
      if (task.status === "running") {
        btnsHtml = `
          <button class="tbtn pause-btn"  data-id="${esc(task.id)}">중단</button>
          <button class="tbtn cancel-btn" data-id="${esc(task.id)}">취소</button>`;
      } else if (task.status === "paused") {
        btnsHtml = `
          <button class="tbtn resume-btn" data-id="${esc(task.id)}">재개</button>
          <button class="tbtn edit-btn"   data-id="${esc(task.id)}" data-text="${esc(task.text)}">수정</button>
          <button class="tbtn cancel-btn" data-id="${esc(task.id)}">취소</button>`;
      } else {
        btnsHtml = `
          <button class="tbtn edit-btn" data-id="${esc(task.id)}" data-text="${esc(task.text)}">수정</button>`;
      }

      card.innerHTML = `
        <div class="task-head">
          <span class="task-text" title="${esc(task.text)}">${esc(task.text)}</span>
          <span class="task-badge ${esc(s.cls)}">${esc(s.label)}</span>
        </div>
        ${latestHtml}
        ${resultHtml}
        <div class="task-btns">${btnsHtml}</div>
      `;
      tasksEl.appendChild(card);
    }
    tasksEl.scrollTop = 0; // 최신 임무(상단)로 스크롤
  }

  // ---- 임무 버튼 위임 ----
  tasksEl.addEventListener("click", async e => {
    const btn = e.target.closest("button[data-id]");
    if (!btn) return;
    const taskId = btn.dataset.id;

    if (btn.classList.contains("pause-btn")) {
      try { await chrome.runtime.sendMessage({ type: "PAUSE_TASK", taskId }); } catch {}
    } else if (btn.classList.contains("resume-btn")) {
      try { await chrome.runtime.sendMessage({ type: "RESUME_TASK", taskId }); } catch {}
    } else if (btn.classList.contains("cancel-btn")) {
      try { await chrome.runtime.sendMessage({ type: "CANCEL_TASK", taskId }); } catch {}
    } else if (btn.classList.contains("edit-btn")) {
      // 이전 임무 텍스트를 입력창에 채움
      input.value = btn.dataset.text || "";
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 120) + "px";
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  });

  // ---- 임무 제출 (fire-and-forget) ----
  function submitTask() {
    const task = input.value.trim();
    if (!task) return;
    input.value = "";
    input.style.height = "auto";
    input.focus();
    // 즉시 입력창 비우고 전송 — 완료는 storage.onChanged로 받음
    chrome.runtime.sendMessage({ type: "RUN_TASK", task }, () => void chrome.runtime.lastError);
  }

  // ---- 에이전트 종료 ----
  async function endAgent() {
    try { await chrome.runtime.sendMessage({ type: "STOP_TASK" }); } catch {}
    await setEnabled(false);
  }

  // ---- 런처 드래그 (위아래) ----
  let drag = null;
  launcher.addEventListener("pointerdown", e => {
    drag = { y: e.clientY, b: launcherBottom, moved: false };
    try { launcher.setPointerCapture(e.pointerId); } catch {}
  });
  launcher.addEventListener("pointermove", e => {
    if (!drag) return;
    const dy = drag.y - e.clientY; // 위로 끌면 bottom 증가
    if (Math.abs(dy) > 4) drag.moved = true;
    launcherBottom = Math.max(12, Math.min(window.innerHeight - 64, drag.b + dy));
    applyLauncherPos();
  });
  function endDrag(e) {
    if (!drag) return;
    try { launcher.releasePointerCapture(e.pointerId); } catch {}
    const moved = drag.moved;
    drag = null;
    if (moved) {
      try { chrome.storage.local.set({ launcherBottom }); } catch {}
    } else {
      showBar(true); // 클릭 → 질문창 열기
    }
  }
  launcher.addEventListener("pointerup",     endDrag);
  launcher.addEventListener("pointercancel", () => { drag = null; });

  // ---- 헤더 버튼 ----
  minBtn.addEventListener("click", () => showBar(false));
  endBtn.addEventListener("click", endAgent);
  gearBtn.addEventListener("click", () => settings.classList.toggle("open"));
  clearBtn.addEventListener("click", async () => {
    try {
      const { agentTasks = [] } = await chrome.storage.local.get("agentTasks");
      const kept = agentTasks.filter(t => t.status === "running" || t.status === "paused");
      await chrome.storage.local.set({ agentTasks: kept });
    } catch {}
  });

  // ---- 서버 설정 ----
  saveBtn.addEventListener("click", async () => {
    try {
      const res = await chrome.runtime.sendMessage({ type: "SET_SERVER_URL", serverUrl: serverInput.value });
      if (res?.serverUrl) serverInput.value = res.serverUrl;
      saveBtn.textContent = "저장됨";
      setTimeout(() => (saveBtn.textContent = "저장"), 1400);
    } catch {
      saveBtn.textContent = "실패";
      setTimeout(() => (saveBtn.textContent = "저장"), 1400);
    }
  });

  // ---- 입력 ----
  sendBtnEl.addEventListener("click", submitTask);
  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitTask(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  });

  // ---- 확인 요청 ----
  function resolveConfirm(approved) {
    confirmBox.classList.remove("open");
    if (pendingConfirm) {
      try { pendingConfirm({ approved }); } catch {}
      pendingConfirm = null;
    }
  }
  approveBtn.addEventListener("click", () => resolveConfirm(true));
  rejectBtn.addEventListener("click",  () => resolveConfirm(false));

  // ---- 백그라운드 메시지 (AGENT_CONFIRM) ----
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "AGENT_CONFIRM") {
      showBar(true);
      confirmMsg.textContent =
        "[확인 필요] " + (msg.message || "중요한 동작") +
        "\n동작: "   + (msg.action    || "") +
        "\n이유: "   + (msg.reasoning || "");
      confirmBox.classList.add("open");
      pendingConfirm = sendResponse;
      return true; // async response
    }
  });

  // ---- 사이트 ↔ 확장 브리지 (window.postMessage) ----
  window.addEventListener("message", event => {
    if (event.source !== window) return;
    const d = event.data;
    if (!d || d.__arenax !== "agent") return;
    if (d.type === "PING") {
      window.postMessage({ __arenax: "agent", type: "READY" }, "*");
    } else if (d.type === "OPEN") {
      window.postMessage({ __arenax: "agent", type: "READY" }, "*");
      setEnabled(true);
      showBar(true);
    } else if (d.type === "CLOSE") {
      endAgent();
    }
  });
  window.postMessage({ __arenax: "agent", type: "READY" }, "*");

  // ---- storage.onChanged — 실시간 동기화 ----
  let prevNotifT = 0;
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.agentEnabled)
      applyEnabled(!!changes.agentEnabled.newValue);
    if (changes.agentTasks)
      renderTasks(changes.agentTasks.newValue || []);
    if (changes.launcherBottom && typeof changes.launcherBottom.newValue === "number") {
      launcherBottom = changes.launcherBottom.newValue;
      applyLauncherPos();
    }
    if (changes.latestNotification) {
      const n = changes.latestNotification.newValue;
      if (n && n.t && n.t !== prevNotifT) {
        prevNotifT = n.t;
        if (bar.hidden) showBubble(n.text, n.kind);
      }
    }
  });

  // ---- 초기화 ----
  chrome.storage.local
    .get(["agentEnabled", "serverUrl", "agentTasks", "launcherBottom"])
    .then(s => {
      if (s.serverUrl) serverInput.value = s.serverUrl;
      if (typeof s.launcherBottom === "number") launcherBottom = s.launcherBottom;
      applyLauncherPos();
      renderTasks(s.agentTasks || []);
      applyEnabled(!!s.agentEnabled);
    })
    .catch(() => {});
})();
