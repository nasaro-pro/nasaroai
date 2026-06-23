// ArenaX 에이전트 하단 바 (content script)
// 어떤 사이트에서든 화면 하단에 요청창을 띄운다. UI만 담당하고, 실제 작업은
// background.js가 chrome.debugger로 수행한다. 사이트의 CSS/JS와 충돌하지 않도록
// Shadow DOM + adoptedStyleSheets(생성형 스타일시트, CSP 안전)로 완전히 격리한다.
(() => {
  if (window.__arenaxAgentInjected) return;
  window.__arenaxAgentInjected = true;

  // ----------------------- 호스트 + Shadow DOM -----------------------
  const host = document.createElement("div");
  host.id = "__arenax_agent_host";
  // 인라인 스타일은 호스트 위치 지정용 최소한만(점유 영역 0). 실제 UI 스타일은 shadow 안.
  host.style.position = "fixed";
  host.style.left = "0";
  host.style.right = "0";
  host.style.bottom = "0";
  host.style.zIndex = "2147483647";
  host.style.width = "100%";
  host.style.pointerEvents = "none";

  const shadow = host.attachShadow({ mode: "open" });

  const CSS = `
  :host { all: initial; }
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Malgun Gothic", sans-serif; }
  .wrap { pointer-events: none; display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 0 12px 12px; }
  .launcher {
    pointer-events: auto; align-self: flex-end; margin: 0 14px 14px 0;
    width: 52px; height: 52px; border-radius: 50%; border: 0; cursor: pointer;
    background: #7c3aed; color: #fff; font-size: 22px; line-height: 1;
    box-shadow: 0 6px 20px rgba(124,58,237,0.45);
  }
  .launcher:hover { background: #6d28d9; }
  .bar {
    pointer-events: auto; width: 100%; max-width: 920px;
    background: #ffffff; color: #111827;
    border: 1px solid #e5e7eb; border-radius: 16px 16px 12px 12px;
    box-shadow: 0 -6px 30px rgba(0,0,0,0.18);
    display: flex; flex-direction: column; overflow: hidden;
  }
  .bar[hidden] { display: none; }
  .head { display: flex; align-items: center; justify-content: space-between; padding: 10px 14px; border-bottom: 1px solid #f1f1f4; background: #faf5ff; }
  .title { font-size: 14px; font-weight: 800; color: #6d28d9; }
  .head-btns { display: flex; gap: 6px; }
  .icon-btn { border: 0; background: #ffffff; border: 1px solid #e5e7eb; border-radius: 8px; cursor: pointer; font-size: 13px; padding: 4px 8px; color: #6b7280; }
  .icon-btn:hover { background: #f3f4f6; }
  .settings { display: none; padding: 8px 14px; border-bottom: 1px solid #f1f1f4; gap: 6px; }
  .settings.open { display: flex; }
  .settings input { flex: 1; padding: 7px 10px; border: 1px solid #d1d5db; border-radius: 8px; font-size: 12px; }
  .settings button { padding: 7px 12px; border: 0; background: #7c3aed; color: #fff; border-radius: 8px; font-size: 12px; font-weight: 700; cursor: pointer; }
  .log { max-height: 200px; overflow-y: auto; padding: 10px 14px; display: flex; flex-direction: column; gap: 6px; }
  .log:empty { display: none; }
  .line { font-size: 13px; line-height: 1.5; padding: 7px 10px; border-radius: 8px; background: #f9fafb; border-left: 3px solid #d1d5db; white-space: pre-wrap; word-break: break-word; }
  .line.task { background: #f5f3ff; border-left-color: #7c3aed; font-weight: 700; }
  .line.step { background: #eff6ff; border-left-color: #3b82f6; color: #1d4ed8; }
  .line.success { background: #ecfdf5; border-left-color: #10b981; color: #047857; font-weight: 700; }
  .line.error { background: #fef2f2; border-left-color: #ef4444; color: #b91c1c; font-weight: 700; }
  .line.handoff { background: #fffbeb; border-left-color: #f59e0b; color: #b45309; font-weight: 700; }
  .line.info { color: #6b7280; }
  .confirm { display: none; padding: 10px 14px; border-top: 1px solid #f1f1f4; background: #fffbeb; flex-direction: column; gap: 8px; }
  .confirm.open { display: flex; }
  .confirm-msg { font-size: 13px; color: #92400e; line-height: 1.5; }
  .confirm-btns { display: flex; gap: 8px; }
  .confirm-btns button { flex: 1; padding: 8px; border: 0; border-radius: 8px; font-size: 13px; font-weight: 700; cursor: pointer; }
  .approve { background: #16a34a; color: #fff; }
  .reject { background: #e5e7eb; color: #374151; }
  .input-row { display: flex; gap: 8px; padding: 10px 14px; border-top: 1px solid #f1f1f4; align-items: flex-end; }
  .input-row textarea {
    flex: 1; resize: none; overflow: hidden; min-height: 40px; max-height: 120px;
    padding: 10px 12px; border: 1px solid #d1d5db; border-radius: 10px; font-size: 14px; line-height: 1.5; color: #111827; background: #fff;
  }
  .input-row textarea:focus { outline: none; border-color: #7c3aed; box-shadow: 0 0 0 3px rgba(124,58,237,0.15); }
  .input-row textarea:disabled { background: #f3f4f6; color: #9ca3af; }
  .send { align-self: stretch; min-width: 64px; border: 0; border-radius: 10px; background: #7c3aed; color: #fff; font-size: 14px; font-weight: 800; cursor: pointer; }
  .send:hover:not(:disabled) { background: #6d28d9; }
  .send:disabled { background: #c4b5fd; cursor: not-allowed; }
  `;

  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(CSS);
    shadow.adoptedStyleSheets = [sheet];
  } catch (e) {
    // 구형 폴백: <style> 주입 (대부분 환경은 위 경로를 사용)
    const styleEl = document.createElement("style");
    styleEl.textContent = CSS;
    shadow.appendChild(styleEl);
  }

  shadow.innerHTML += `
    <div class="wrap">
      <button class="launcher" id="ax-launcher" title="ArenaX 에이전트">🤖</button>
      <div class="bar" id="ax-bar" hidden>
        <div class="head">
          <span class="title">🤖 ArenaX 에이전트</span>
          <div class="head-btns">
            <button class="icon-btn" id="ax-gear" title="서버 설정">설정</button>
            <button class="icon-btn" id="ax-close" title="닫기">✕</button>
          </div>
        </div>
        <div class="settings" id="ax-settings">
          <input id="ax-server" type="text" placeholder="https://arenax-4812.onrender.com" />
          <button id="ax-save">저장</button>
        </div>
        <div class="log" id="ax-log"></div>
        <div class="confirm" id="ax-confirm">
          <div class="confirm-msg" id="ax-confirm-msg"></div>
          <div class="confirm-btns">
            <button class="approve" id="ax-approve">승인하고 진행</button>
            <button class="reject" id="ax-reject">취소</button>
          </div>
        </div>
        <div class="input-row">
          <textarea id="ax-input" rows="1" placeholder="이 화면에서 할 일을 지시하세요. 예: 검색창에 '서울 날씨' 입력하고 검색"></textarea>
          <button class="send" id="ax-send">전송</button>
        </div>
      </div>
    </div>
  `;

  const $ = (id) => shadow.getElementById(id);
  const launcher = $("ax-launcher");
  const bar = $("ax-bar");
  const gearBtn = $("ax-gear");
  const closeBtn = $("ax-close");
  const settings = $("ax-settings");
  const serverInput = $("ax-server");
  const saveBtn = $("ax-save");
  const logEl = $("ax-log");
  const confirmBox = $("ax-confirm");
  const confirmMsg = $("ax-confirm-msg");
  const approveBtn = $("ax-approve");
  const rejectBtn = $("ax-reject");
  const input = $("ax-input");
  const sendBtn = $("ax-send");

  document.documentElement.appendChild(host);

  let running = false;
  let pendingConfirm = null; // sendResponse 보관용

  function showBar(show) {
    bar.hidden = !show;
    launcher.style.display = show ? "none" : "block";
    if (show) input.focus();
  }
  function toggleBar() {
    showBar(bar.hidden);
  }

  function addLine(text, kind) {
    const line = document.createElement("div");
    line.className = "line " + (kind || "info");
    line.textContent = text;
    logEl.appendChild(line);
    logEl.scrollTop = logEl.scrollHeight;
    return line;
  }

  async function run() {
    if (running) return;
    const task = input.value.trim();
    if (!task) return;
    running = true;
    sendBtn.disabled = true;
    input.disabled = true;
    addLine("🧑 " + task, "task");
    input.value = "";
    input.style.height = "auto";
    const pending = addLine("⏳ 시작하는 중...", "step");

    try {
      const result = await chrome.runtime.sendMessage({ type: "RUN_TASK", task });
      pending.remove();
      const kind = result && result.kind ? result.kind : "error";
      const text = (result && result.finalText) || "결과를 받지 못했습니다.";
      addLine(text, kind);
    } catch (e) {
      pending.remove();
      addLine("❌ " + (e && e.message ? e.message : "통신 오류"), "error");
    } finally {
      running = false;
      sendBtn.disabled = false;
      input.disabled = false;
      input.focus();
    }
  }

  // ----------------------- 이벤트 -----------------------
  launcher.addEventListener("click", () => showBar(true));
  closeBtn.addEventListener("click", () => showBar(false));
  gearBtn.addEventListener("click", () => settings.classList.toggle("open"));
  sendBtn.addEventListener("click", run);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      run();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  });

  saveBtn.addEventListener("click", async () => {
    try {
      const res = await chrome.runtime.sendMessage({ type: "SET_SERVER_URL", serverUrl: serverInput.value });
      if (res && res.serverUrl) {
        serverInput.value = res.serverUrl;
        saveBtn.textContent = "저장됨";
        setTimeout(() => (saveBtn.textContent = "저장"), 1200);
      }
    } catch (e) {
      saveBtn.textContent = "실패";
      setTimeout(() => (saveBtn.textContent = "저장"), 1200);
    }
  });

  function resolveConfirm(approved) {
    confirmBox.classList.remove("open");
    if (pendingConfirm) {
      try {
        pendingConfirm({ approved });
      } catch (e) {
        /* ignore */
      }
      pendingConfirm = null;
    }
  }
  approveBtn.addEventListener("click", () => resolveConfirm(true));
  rejectBtn.addEventListener("click", () => resolveConfirm(false));

  // ----------------------- 백그라운드 메시지 수신 -----------------------
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "TOGGLE_BAR") {
      toggleBar();
      sendResponse && sendResponse({ ok: true });
      return;
    }
    if (message.type === "AGENT_PROGRESS") {
      showBar(true);
      addLine(message.text, message.kind);
      return;
    }
    if (message.type === "AGENT_CONFIRM") {
      showBar(true);
      confirmMsg.textContent =
        "[확인 필요] " +
        (message.message || "중요한 동작") +
        "\n동작: " +
        (message.action || "") +
        "\n이유: " +
        (message.reasoning || "");
      confirmBox.classList.add("open");
      pendingConfirm = sendResponse;
      return true; // 사용자가 버튼을 누를 때까지 응답을 보류
    }
  });

  // 초기 서버 주소 표시
  chrome.runtime
    .sendMessage({ type: "GET_SERVER_URL" })
    .then((res) => {
      if (res && res.serverUrl) serverInput.value = res.serverUrl;
    })
    .catch(() => {});
})();
