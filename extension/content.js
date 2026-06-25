// Nasaro AI 에이전트 하단 바 (content script v5)
// - 활성 임무(실행중/중단됨) 최대 5개, 기록(완료/오류 등)은 별도 섹션
// - 임무 목록 높이를 드래그로 조절 가능
// - 최신 임무가 아래에 표시 (push 순서)
// - 런처 말풍선: 3초 + X닫기
(() => {
  // 주입 중복 방지: 전역 가드 + DOM 호스트 중복 체크
  if (window.__nasaroaiAgentInjected) return;
  const existingHost = document.getElementById("__nasaroai_agent_host");
  if (existingHost?.shadowRoot?.getElementById("ax-launcher")) {
    window.__nasaroaiAgentInjected = true;
    return;
  }
  // 중간 실패로 남은 고아 호스트 정리 후 재주입
  if (existingHost) existingHost.remove();
  window.__nasaroaiAgentInjected = true;

  const host = document.createElement("div");
  host.id = "__nasaroai_agent_host";
  Object.assign(host.style, {
    position: "fixed", left: "0", right: "0", bottom: "0",
    width: "100%", zIndex: "2147483647", pointerEvents: "none",
  });
  const shadow = host.attachShadow({ mode: "open" });

  const CSS = `
  :host { all: initial; }
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Malgun Gothic", sans-serif; }

  /* AX 로고 텍스트 */
  .ax-logo {
    font-size: 15px; font-weight: 900; letter-spacing: -1px; line-height: 1; pointer-events: none;
    background: linear-gradient(135deg, #fff 20%, #d8b4fe 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .ax-logo-sm {
    font-size: 13px; font-weight: 900; letter-spacing: -1px; color: #7c3aed; pointer-events: none;
  }

  .launcher {
    pointer-events: auto; position: fixed; right: 16px;
    width: 52px; height: 52px; border-radius: 50%; border: 0; cursor: grab;
    background: linear-gradient(135deg, #7c3aed 0%, #5b21b6 100%); color: #fff;
    box-shadow: 0 6px 24px rgba(124,58,237,.5);
    display: none; touch-action: none; user-select: none; z-index: 1;
    flex-direction: column; gap: 1px;
  }
  .launcher.show { display: flex; align-items: center; justify-content: center; }
  .launcher:active { cursor: grabbing; }
  .launcher:hover { background: linear-gradient(135deg, #6d28d9 0%, #4c1d95 100%); }

  /* 활성 임무 표시 도트 */
  .launcher-dot {
    position: absolute; top: 5px; right: 5px;
    width: 11px; height: 11px; border-radius: 50%;
    background: #10b981; border: 2px solid #fff;
    display: none; pointer-events: none;
  }
  .launcher.has-active .launcher-dot { display: block; animation: dot-pulse 1.8s ease-in-out infinite; }
  @keyframes dot-pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50%       { transform: scale(1.35); opacity: 0.75; }
  }

  .bubble {
    --bg: #1f2937;
    pointer-events: auto; position: fixed; right: 76px;
    background: var(--bg); color: #fff;
    padding: 7px 10px 7px 13px; border-radius: 10px;
    font-size: 12px; font-weight: 600; max-width: 240px;
    display: none; align-items: center; gap: 6px;
    box-shadow: 0 4px 14px rgba(0,0,0,.3); z-index: 1;
  }
  .bubble.show { display: flex; }
  .bubble-text { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bubble-close { flex: 0 0 auto; background: transparent; border: 0; color: rgba(255,255,255,.75); cursor: pointer; font-size: 13px; line-height: 1; padding: 0; }
  .bubble-close:hover { color: #fff; }
  .bubble::after {
    content: ''; position: absolute; right: -7px; top: 50%; transform: translateY(-50%);
    border: 7px solid transparent; border-left-color: var(--bg); border-right-width: 0;
  }
  .bubble.success  { --bg: #065f46; }
  .bubble.error    { --bg: #7f1d1d; }
  .bubble.handoff  { --bg: #78350f; }
  .bubble.cancelled { --bg: #374151; }

  .wrap { display: contents; /* bar가 position:fixed이므로 wrap은 투명 컨테이너 */ }
  .bar {
    pointer-events: auto; position: fixed; /* applyBarPos()가 left/top 동적 설정 */
    background: #fff; color: #111827; z-index: 1;
    border: 1px solid #e5e7eb; border-radius: 18px;
    box-shadow: 0 8px 40px rgba(0,0,0,.18);
    display: flex; flex-direction: column; overflow: hidden;
    width: 560px;
    max-height: min(78vh, 760px);
    min-width: 420px;
    min-height: 360px;
    max-width: calc(100vw - 16px);
    resize: both;
  }
  .bar[hidden] { display: none !important; }

  /* 모바일: 전체 너비 바텀 시트 */
  @media (max-width: 640px) {
    .bar {
      left: 0 !important; right: 0 !important;
      bottom: 0 !important; top: auto !important;
      width: 100% !important; border-radius: 16px 16px 0 0;
      max-height: 90dvh; max-height: 90vh;
    }
  }

  /* ── 모바일 (≤ 640px) : 풀스크린 바텀시트 스타일 ── */
  @media (max-width: 640px) {
    .wrap { padding: 0; }
    .bar {
      max-width: 100%; border-radius: 16px 16px 0 0;
      max-height: 90dvh; max-height: 90vh;
    }
    .launcher { width: 48px; height: 48px; font-size: 20px; right: 12px; }
    .bubble { max-width: 180px; font-size: 11px; right: 68px; }
    /* iOS 폼 요소 줌 방지 (font-size < 16px → iOS 자동 줌) */
    .task-input { font-size: 16px !important; }
    .settings input { font-size: 16px !important; }
    /* 버튼 터치 영역 확장 */
    .tbtn { padding: 5px 11px; font-size: 12px; min-height: 30px; }
    .send-btn { min-width: 56px; font-size: 15px; }
    .icon-btn, .end-btn { padding: 6px 10px; font-size: 12px; min-height: 32px; }
    .min-btn { padding: 7px 14px; font-size: 14px; min-width: 60px; }
    /* 리사이즈 핸들 터치 영역 확장 */
    .resize-handle { height: 10px; }
    .resize-handle::before { height: 4px; width: 40px; }
    /* 태스크 높이 기본값을 모바일에 맞게 제한 */
    .tasks-wrap { max-height: 45vh; }
  }

  .head { display: flex; align-items: center; gap: 8px; padding: 12px 16px; border-bottom: 1px solid #f1f1f4; background: #faf5ff; flex-shrink: 0; cursor: move; }
  .title { font-size: 15px; font-weight: 800; color: #6d28d9; flex: 0 0 auto; }
  .task-counter { flex: 1 1 auto; min-width: 74px; font-size: 11px; font-weight: 700; color: #7c3aed; background: #ede9fe; border-radius: 20px; padding: 2px 9px; white-space: nowrap; display: inline-block; align-self: center; overflow: hidden; text-overflow: ellipsis; }
  .task-counter.full { background: #fecaca; color: #b91c1c; }
  .head-btns { display: flex; gap: 6px; flex: 0 0 auto; align-items: center; margin-left: auto; }
  .icon-btn { border: 1px solid #e5e7eb; background: #fff; border-radius: 8px; cursor: pointer; font-size: 12px; padding: 5px 11px; color: #6b7280; white-space: nowrap; }
  .icon-btn:hover { background: #f3f4f6; }
  /* 접기 버튼 — 눈에 띄게 크게 */
  .min-btn {
    border: 2px solid #c4b5fd; background: #ede9fe; color: #7c3aed;
    border-radius: 8px; cursor: pointer; font-size: 15px; font-weight: 900;
    padding: 6px 18px; white-space: nowrap; line-height: 1;
    min-width: 72px; text-align: center;
  }
  .min-btn:hover { background: #ddd6fe; border-color: #a78bfa; }
  .end-btn { border: 1px solid #fecaca; background: #fef2f2; color: #b91c1c; border-radius: 8px; cursor: pointer; font-size: 12px; font-weight: 700; padding: 5px 12px; white-space: nowrap; }
  .end-btn:hover { background: #fee2e2; }

  .settings { display: none; padding: 8px 14px; border-bottom: 1px solid #f1f1f4; gap: 6px; flex-shrink: 0; }
  .settings.open { display: flex; }
  .settings input { flex: 1; padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 8px; font-size: 12px; }
  .settings button { padding: 6px 12px; border: 0; background: #7c3aed; color: #fff; border-radius: 8px; font-size: 12px; font-weight: 700; cursor: pointer; }

  /* 높이 조절 핸들 */
  .resize-handle {
    pointer-events: auto; flex-shrink: 0;
    height: 6px; background: #f1f1f4; cursor: ns-resize;
    border-top: 1px solid #e5e7eb; border-bottom: 1px solid #e5e7eb;
    display: flex; align-items: center; justify-content: center;
  }
  .resize-handle::before { content: ''; width: 32px; height: 3px; border-radius: 2px; background: #d1d5db; }
  .resize-handle:hover, .resize-handle:hover::before { background: #c4b5fd; }

  /* 임무 영역 */
  .task-area { display: flex; flex: 1 1 auto; min-height: 140px; }
  .task-sidebar {
    width: 148px; flex: 0 0 148px; border-right: 1px solid #f1f1f4; background: #fcfcff;
    display: flex; flex-direction: column; min-height: 0;
  }
  .task-sidebar-head {
    padding: 8px 8px 6px; border-bottom: 1px solid #f3f4f6; display: flex; align-items: center; gap: 6px;
  }
  .task-sidebar-title { font-size: 11px; font-weight: 800; color: #6b7280; }
  .task-clear-btn {
    margin-left: auto; border: 1px solid #fecaca; background: #fff1f2; color: #b91c1c;
    border-radius: 6px; font-size: 10px; font-weight: 700; padding: 3px 6px; cursor: pointer;
  }
  .task-mini-list { overflow-y: auto; min-height: 0; padding: 6px; display: flex; flex-direction: column; gap: 4px; }
  .task-mini-item {
    display: flex; align-items: center; gap: 4px; border: 1px solid #e5e7eb; border-radius: 7px;
    background: #fff; padding: 4px 5px;
  }
  .task-mini-link {
    flex: 1 1 auto; min-width: 0; border: 0; background: transparent; text-align: left; cursor: pointer;
    font-size: 11px; color: #374151; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .task-mini-del {
    flex: 0 0 auto; border: 1px solid #e5e7eb; background: #f9fafb; color: #9ca3af;
    border-radius: 5px; font-size: 10px; font-weight: 700; padding: 2px 5px; cursor: pointer;
  }
  .task-mini-list::-webkit-scrollbar { width: 5px; }
  .task-mini-list::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 4px; }

  .tasks-wrap { overflow-y: auto; flex: 1 1 auto; min-width: 0; }
  .tasks-inner { display: flex; flex-direction: column; gap: 6px; padding: 10px 14px; }
  .section-label { font-size: 11px; font-weight: 700; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.05em; padding: 4px 0 2px; }
  .empty { font-size: 13px; color: #9ca3af; padding: 2px 0; }

  /* 임무 카드 */
  .task-card { border: 1px solid #e5e7eb; border-radius: 10px; overflow: hidden; background: #fff; }
  .task-card[data-status="running"]   { border-left: 3px solid #3b82f6; }
  .task-card[data-status="paused"]    { border-left: 3px solid #f59e0b; }
  .task-card[data-status="success"]   { border-left: 3px solid #10b981; }
  .task-card[data-status="error"]     { border-left: 3px solid #ef4444; }
  .task-card[data-status="handoff"]   { border-left: 3px solid #f59e0b; }
  .task-card[data-status="cancelled"] { border-left: 3px solid #9ca3af; }
  .task-card.archived { opacity: 0.8; }

  .task-head { display: flex; align-items: center; gap: 6px; padding: 7px 10px 3px; }
  .task-text { flex: 1 1 auto; font-size: 13px; font-weight: 700; color: #111827; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .task-badge { flex: 0 0 auto; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 20px; white-space: nowrap; }
  .task-badge.running   { background: #dbeafe; color: #1d4ed8; }
  .task-badge.paused    { background: #fef3c7; color: #b45309; }
  .task-badge.success   { background: #d1fae5; color: #065f46; }
  .task-badge.error     { background: #fee2e2; color: #b91c1c; }
  .task-badge.handoff   { background: #fef3c7; color: #b45309; }
  .task-badge.cancelled { background: #f3f4f6; color: #6b7280; }

  .task-site { font-size: 11px; color: #6b7280; padding: 0 10px 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .task-latest { font-size: 12px; color: #4b5563; padding: 0 10px 2px; overflow-wrap: anywhere; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .task-log {
    margin: 2px 10px 6px;
    padding: 7px 8px;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    background: #f9fafb;
    max-height: 130px;
    overflow-y: auto;
    font-size: 11px;
    line-height: 1.45;
    color: #4b5563;
    white-space: pre-wrap;
  }
  .task-log-line { padding: 1px 0; }
  .task-log::-webkit-scrollbar { width: 5px; }
  .task-log::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 4px; }
  .task-result { font-size: 12px; font-weight: 600; padding: 2px 10px 5px; overflow-wrap: anywhere; white-space: pre-wrap; }
  .task-result.success   { color: #047857; }
  .task-result.error     { color: #b91c1c; }
  .task-result.handoff   { color: #b45309; }
  .task-result.cancelled { color: #6b7280; }

  .task-btns { display: flex; flex-wrap: wrap; gap: 5px; padding: 3px 10px 7px; align-items: center; }
  .tbtn { border: 1px solid #e5e7eb; background: #f9fafb; border-radius: 6px; cursor: pointer; font-size: 11px; font-weight: 700; padding: 3px 9px; white-space: nowrap; }
  .tbtn:hover { filter: brightness(.93); }
  .tbtn.pause-btn   { border-color: #fbbf24; color: #b45309; }
  .tbtn.resume-btn  { border-color: #34d399; background: #ecfdf5; color: #065f46; }
  .tbtn.cancel-btn  { border-color: #fca5a5; color: #b91c1c; }
  .tbtn.edit-btn    { border-color: #93c5fd; color: #1d4ed8; }
  .tbtn.amend-btn   { border-color: #fbbf24; color: #b45309; }
  .amend-row { display:flex; gap:6px; padding:6px 0 2px; }
  .amend-row textarea { flex:1; font-size:12px; padding:5px 8px; border-radius:7px;
    border:1px solid #fbbf24; background:#1f2937; color:#f3f4f6; resize:none;
    min-height:44px; outline:none; font-family:inherit; }
  .amend-row textarea:focus { border-color:#f59e0b; }
  .amend-row .tbtn { white-space:nowrap; }
  .tbtn.delete-btn  { margin-left: auto; border-color: #d1d5db; color: #9ca3af; }

  .confirm { display: none; padding: 10px 14px; border-top: 1px solid #f1f1f4; background: #fffbeb; flex-direction: column; gap: 8px; flex-shrink: 0; }
  .confirm.open { display: flex; }
  .confirm-msg { font-size: 13px; color: #92400e; line-height: 1.5; white-space: pre-wrap; }
  .confirm-btns { display: flex; gap: 8px; }
  .confirm-btns button { flex: 1; padding: 8px; border: 0; border-radius: 8px; font-size: 13px; font-weight: 700; cursor: pointer; }
  .approve-btn { background: #16a34a; color: #fff; }
  .reject-btn  { background: #e5e7eb; color: #374151; }

  .input-row { display: flex; gap: 8px; padding: 10px 14px; border-top: 1px solid #f1f1f4; align-items: flex-end; flex-shrink: 0; }
  .task-input {
    flex: 1; resize: none; overflow: hidden; min-height: 40px; max-height: 120px;
    padding: 10px 12px; border: 1px solid #d1d5db; border-radius: 10px;
    font-size: 14px; line-height: 1.5; color: #111827; background: #fff;
  }
  .task-input:focus { outline: none; border-color: #7c3aed; box-shadow: 0 0 0 3px rgba(124,58,237,.15); }
  .task-input.full { border-color: #fca5a5; }
  .send-btn { align-self: stretch; min-width: 64px; border: 0; border-radius: 10px; background: #7c3aed; color: #fff; font-size: 14px; font-weight: 800; cursor: pointer; }
  .send-btn:hover:not(:disabled) { background: #6d28d9; }
  .send-btn:disabled { background: #c4b5fd; cursor: not-allowed; }
  `;

  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(CSS);
    shadow.adoptedStyleSheets = [sheet];
  } catch (e) {
    const s = document.createElement("style"); s.textContent = CSS; shadow.appendChild(s);
  }

  const root = document.createElement("div");
  root.innerHTML = `
    <button class="launcher" id="ax-launcher" title="Nasaro AI 에이전트 (드래그로 이동)"><span class="ax-logo" style="font-size:11px;letter-spacing:-.5px;">Nasaro</span><span class="launcher-dot"></span></button>
    <div class="bubble" id="ax-bubble">
      <span class="bubble-text" id="ax-bubble-text"></span>
      <button class="bubble-close" id="ax-bubble-close" title="닫기">✕</button>
    </div>
    <div class="wrap">
      <div class="bar" id="ax-bar" hidden>
        <div class="head">
          <span class="title"><span class="ax-logo-sm">Nasaro AI</span> 에이전트</span>
          <span class="task-counter" id="ax-counter">0/5 활성</span>
          <div class="head-btns">
            <button class="icon-btn" id="ax-gear" title="서버 설정">설정</button>
            <button class="min-btn"  id="ax-min"  title="질문창 닫기">▼ 접기</button>
            <button class="end-btn"  id="ax-end"  title="에이전트 종료">에이전트 종료</button>
          </div>
        </div>
        <div class="settings" id="ax-settings">
          <input id="ax-server" type="text" placeholder="https://nasaroai.onrender.com" />
          <button id="ax-save">저장</button>
        </div>
        <div class="task-area" id="ax-task-area">
          <aside class="task-sidebar">
            <div class="task-sidebar-head">
              <span class="task-sidebar-title">임무 목록</span>
              <button class="task-clear-btn" id="ax-clear-all" title="기록 전체 삭제">전체삭제</button>
            </div>
            <div class="task-mini-list" id="ax-mini-list"></div>
          </aside>
          <div class="tasks-wrap" id="ax-tasks-wrap">
            <div class="tasks-inner" id="ax-tasks"></div>
          </div>
        </div>
        <div class="resize-handle" id="ax-resize"></div>
        <div class="confirm" id="ax-confirm">
          <div class="confirm-msg" id="ax-confirm-msg"></div>
          <div class="confirm-btns">
            <button class="approve-btn" id="ax-approve">승인하고 진행</button>
            <button class="reject-btn"  id="ax-reject">취소</button>
          </div>
        </div>
        <div class="input-row">
          <textarea class="task-input" id="ax-input" rows="1"
            placeholder="이 화면에서 할 일을 지시하세요."></textarea>
          <button class="send-btn" id="ax-send">전송</button>
        </div>
      </div>
    </div>
  `;
  shadow.appendChild(root);

  const $          = id => shadow.getElementById(id);
  const launcher   = $("ax-launcher");
  const bubble     = $("ax-bubble");
  const bubbleText = $("ax-bubble-text");
  const bubbleClose= $("ax-bubble-close");
  const bar        = $("ax-bar");
  const counter    = $("ax-counter");
  const headEl     = shadow.querySelector(".head");
  const gearBtn    = $("ax-gear");
  const minBtn     = $("ax-min");
  const endBtn     = $("ax-end");
  const settings   = $("ax-settings");
  const serverInput= $("ax-server");
  const saveBtn    = $("ax-save");
  const taskArea   = $("ax-task-area");
  const resizeHandle=$("ax-resize");
  const tasksWrap  = $("ax-tasks-wrap");
  const tasksEl    = $("ax-tasks");
  const miniListEl = $("ax-mini-list");
  const clearAllBtn= $("ax-clear-all");
  const confirmBox = $("ax-confirm");
  const confirmMsg = $("ax-confirm-msg");
  const approveBtn = $("ax-approve");
  const rejectBtn  = $("ax-reject");
  const input      = $("ax-input");
  const sendBtnEl  = $("ax-send");

  document.documentElement.appendChild(host);

  const MAX_ACTIVE  = 5;
  let initialized   = false; // storage 확인 전까지 런처를 절대 표시하지 않음
  let enabled       = false;
  let pendingConfirm= null;
  let launcherBottom= 96;
  let launcherRight = 16;
  let barLeft = 0;
  let barTop = 0;
  let barManualPos = false;
  let barWidth = 560;
  let barHeight = 0;
  let kbOffset = 0; // 모바일 키보드 높이 오프셋
  let tasksHeight   = 260;
  let bubbleTimer   = null;
  let prevNotifT    = 0;

  const STATUS = {
    running:   { label: "● 실행중",  cls: "running"   },
    paused:    { label: "⏸ 중단됨", cls: "paused"    },
    success:   { label: "✅ 완료",   cls: "success"   },
    error:     { label: "❌ 오류",   cls: "error"     },
    handoff:   { label: "🟡 전달",   cls: "handoff"   },
    cancelled: { label: "🛑 취소됨", cls: "cancelled" },
  };
  const ACTIVE_SET = new Set(["running", "paused"]);

  function esc(s) {
    return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
  }

  // ── 표시 상태 ────────────────────────────────────────────────────────
  // storage 초기화 완료 전까지 런처를 표시하지 않도록 guard
  function render() {
    if (!initialized) { launcher.classList.remove("show"); return; }
    launcher.classList.toggle("show", enabled && bar.hidden);
  }
  function applyBarSize() {
    if (window.innerWidth <= 640) {
      bar.style.removeProperty("width");
      bar.style.removeProperty("height");
      return;
    }
    const w = Math.max(420, Math.min(window.innerWidth - 16, barWidth || 560));
    bar.style.width = w + "px";
    if (barHeight > 0) {
      const h = Math.max(360, Math.min(window.innerHeight - 16, barHeight));
      bar.style.height = h + "px";
    } else {
      bar.style.removeProperty("height");
    }
  }
  function applyManualBarPos() {
    const bW = bar.offsetWidth || 560;
    const bH = bar.offsetHeight || 760;
    const left = Math.max(8, Math.min(window.innerWidth - bW - 8, barLeft));
    const top = Math.max(8, Math.min(window.innerHeight - bH - 8, barTop));
    bar.style.left = left + "px";
    bar.style.top = top + "px";
    bar.style.right = "auto";
    bar.style.bottom = "auto";
  }

  function showBar(show) {
    bar.hidden = !show;
    render();
    if (show) {
      applyBarSize();
      if (window.innerWidth > 640 && barManualPos) {
        applyManualBarPos();
      } else {
        applyBarPos();
      }
      setTimeout(() => {
        input.focus();
        tasksWrap.scrollTop = tasksWrap.scrollHeight;
      }, 50);
    }
    // 모든 탭에 동기화 — 한 탭에서 접으면 다른 탭도 같이 접힘
    try { chrome.storage.local.set({ barOpen: show }); } catch {}
  }
  function broadcastAgentState(v) {
    try { window.postMessage({ __nasaroai: "agent", type: "STATE", enabled: !!v }, "*"); } catch {}
  }
  function applyEnabled(v) {
    enabled = v;
    if (!v) bar.hidden = true;
    render();
    broadcastAgentState(v);
  }
  async function setEnabled(v) {
    try { await chrome.storage.local.set({ agentEnabled: v }); } catch {}
    applyEnabled(v);
  }
  function applyLauncherPos() {
    launcher.style.bottom = (launcherBottom + kbOffset) + "px";
    launcher.style.right  = launcherRight + "px";
    bubble.style.bottom   = (launcherBottom + 58 + kbOffset) + "px";
    bubble.style.right    = (launcherRight + 60) + "px";
    if (!bar.hidden) {
      if (barManualPos && window.innerWidth > 640) applyManualBarPos();
      else applyBarPos();
    }
  }
  // 패널을 런처 옆에 배치 (desktop 전용; 모바일은 CSS @media가 처리)
  function applyBarPos() {
    if (bar.hidden) return;
    if (window.innerWidth <= 640) {
      // 모바일: CSS @media에서 이미 처리하므로 JS 재정의 제거
      ["left","top","right","bottom","width"].forEach(p => bar.style.removeProperty(p));
      return;
    }
    const lRect = launcher.getBoundingClientRect();
    const W = window.innerWidth;
    const H = window.innerHeight;
    const bW = Math.min(560, W - 32);
    const bH = Math.min(Math.floor(H * 0.78), 760);

    // 런처 위치를 그대로 따라가되 화면 안으로만 보정
    let left = lRect.left;
    if (left + bW > W - 8) left = W - bW - 8;
    if (left < 8) left = 8;

    // 수직: 런처 하단 기준 패널 하단 맞춤
    let top = lRect.bottom - bH;
    if (top < 8) top = 8;
    if (top + bH > H - 8) top = H - bH - 8;

    bar.style.left   = left + "px";
    bar.style.top    = top  + "px";
    bar.style.right  = "auto";
    bar.style.bottom = "auto";
    bar.style.width  = bW + "px";
    bar.style.removeProperty("height");
    barWidth = bW;
    barHeight = 0;
    if (!barManualPos) { barLeft = left; barTop = top; }
  }
  function applyTasksHeight() { if (taskArea) taskArea.style.height = tasksHeight + "px"; }

  // ── 말풍선 ───────────────────────────────────────────────────────────
  const BUBBLE_CLS = { success:"success", error:"error", handoff:"handoff", cancelled:"cancelled" };
  function showBubble(text, kind) {
    bubbleText.textContent = text;
    bubble.className = "bubble show" + (BUBBLE_CLS[kind] ? " " + BUBBLE_CLS[kind] : "");
    clearTimeout(bubbleTimer);
    bubbleTimer = setTimeout(hideBubble, 3000);
  }
  function hideBubble() { clearTimeout(bubbleTimer); bubble.className = "bubble"; }
  bubbleClose.addEventListener("click", hideBubble);

  // ── 임무 렌더링 ──────────────────────────────────────────────────────
  function makeCard(task, archived) {
    const s = STATUS[task.status] || STATUS.error;
    const card = document.createElement("div");
    card.className = "task-card" + (archived ? " archived" : "");
    card.dataset.id     = task.id;
    card.dataset.status = task.status;

    const isActive = ACTIVE_SET.has(task.status);
    const latestStep = isActive && task.steps?.length ? task.steps[task.steps.length - 1] : null;
    const latestHtml = latestStep ? `<div class="task-latest">${esc(latestStep.text)}</div>` : "";
    const siteLabel = task.pageHost || task.pageUrl || "";
    const siteHtml = siteLabel ? `<div class="task-site">사이트: ${esc(siteLabel)}</div>` : "";
    const resultHtml = task.result ? `<div class="task-result ${esc(s.cls)}">${esc(task.result)}</div>` : "";

    let btnHtml = "";
    if (task.status === "running") {
      btnHtml = `<button class="tbtn pause-btn"  data-id="${esc(task.id)}">중단</button>
                 <button class="tbtn continue-btn" data-id="${esc(task.id)}" data-text="${esc(task.text)}">추가 임무</button>
                 <button class="tbtn amend-btn"  data-id="${esc(task.id)}" data-text="${esc(task.text)}">수정</button>
                 <button class="tbtn cancel-btn" data-id="${esc(task.id)}">취소</button>`;
    } else if (task.status === "paused") {
      btnHtml = `<button class="tbtn resume-btn" data-id="${esc(task.id)}">재개</button>
                 <button class="tbtn continue-btn" data-id="${esc(task.id)}" data-text="${esc(task.text)}">추가 임무</button>
                 <button class="tbtn amend-btn"  data-id="${esc(task.id)}" data-text="${esc(task.text)}">수정</button>
                 <button class="tbtn cancel-btn" data-id="${esc(task.id)}">취소</button>`;
    } else {
      btnHtml = `<button class="tbtn continue-btn" data-id="${esc(task.id)}" data-text="${esc(task.text)}">추가 임무</button>
                 <button class="tbtn edit-btn" data-id="${esc(task.id)}" data-text="${esc(task.text)}">수정</button>`;
    }
    btnHtml += `<button class="tbtn delete-btn" data-id="${esc(task.id)}">삭제</button>`;

    const logs = (task.steps || []).map(step => {
      const ts = step?.t ? new Date(step.t) : null;
      const stamp = ts && !Number.isNaN(ts.getTime())
        ? `${String(ts.getHours()).padStart(2, "0")}:${String(ts.getMinutes()).padStart(2, "0")}:${String(ts.getSeconds()).padStart(2, "0")}`
        : "--:--:--";
      return `<div class="task-log-line">[${stamp}] ${esc(step?.text || "")}</div>`;
    }).join("");
    const logHtml = logs ? `<div class="task-log">${logs}</div>` : "";

    card.innerHTML = `
      <div class="task-head">
        <span class="task-text" title="${esc(task.text)}">${esc(task.text)}</span>
        <span class="task-badge ${esc(s.cls)}">${esc(s.label)}</span>
      </div>
      ${siteHtml}
      ${latestHtml}
      ${logHtml}
      ${resultHtml}
      <div class="task-btns">${btnHtml}</div>
    `;
    return card;
  }

  function renderTasks(tasks) {
    const list = tasks || [];
    const active   = list.filter(t => ACTIVE_SET.has(t.status));
    const archived = list.filter(t => !ACTIVE_SET.has(t.status));

    // 실행 중인 임무가 있으면 런처에 초록 도트 표시
    launcher.classList.toggle("has-active", active.some(t => t.status === "running"));

    counter.textContent = `${active.length}/${MAX_ACTIVE} 활성`;
    counter.classList.toggle("full", active.length >= MAX_ACTIVE);
    input.classList.toggle("full", active.length >= MAX_ACTIVE);
    input.placeholder = active.length >= MAX_ACTIVE
      ? `활성 임무가 ${MAX_ACTIVE}개입니다. 완료/취소 후 입력하세요.`
      : "이 화면에서 할 일을 지시하세요.";

    tasksEl.innerHTML = "";
    if (miniListEl) miniListEl.innerHTML = "";

    // 왼쪽 미니바: 최신이 아래에 쌓이도록 원본 순서로 렌더
    if (miniListEl) {
      for (const t of list) {
        const item = document.createElement("div");
        item.className = "task-mini-item";
        item.innerHTML = `
          <button class="task-mini-link" data-id="${esc(t.id)}" title="${esc(t.text)}">${esc(t.text)}</button>
          <button class="task-mini-del" data-id="${esc(t.id)}" title="삭제">✕</button>
        `;
        miniListEl.appendChild(item);
      }
      miniListEl.scrollTop = miniListEl.scrollHeight;
    }

    // ── 기록 먼저(위) — 위로 스크롤하면 볼 수 있음 ──
    if (archived.length) {
      const lbl = document.createElement("div");
      lbl.className = "section-label"; lbl.textContent = "기록";
      tasksEl.appendChild(lbl);
      archived.forEach(t => tasksEl.appendChild(makeCard(t, true)));
    }

    // ── 활성 임무 나중(아래) — 항상 화면에 보임 ──
    if (active.length) {
      const lbl = document.createElement("div");
      lbl.className = "section-label"; lbl.textContent = "활성 임무";
      tasksEl.appendChild(lbl);
      active.forEach(t => tasksEl.appendChild(makeCard(t, false)));
    }

    if (!active.length && !archived.length) {
      tasksEl.innerHTML = '<div class="empty">아직 임무가 없습니다. 아래에 지시사항을 입력하세요.</div>';
    }

    // 활성 임무가 아래 → 스크롤 하단으로 맞춰 활성 임무가 기본 노출
    tasksWrap.scrollTop = tasksWrap.scrollHeight;
  }

  // ── 임무 버튼 이벤트 (위임) ──────────────────────────────────────────
  tasksEl.addEventListener("click", async e => {
    const btn = e.target.closest("button[data-id]");
    if (!btn) return;
    const taskId = btn.dataset.id;
    if (btn.classList.contains("pause-btn"))
      { try { await chrome.runtime.sendMessage({ type: "PAUSE_TASK",  taskId }); } catch {} }
    else if (btn.classList.contains("resume-btn"))
      { try { await chrome.runtime.sendMessage({ type: "RESUME_TASK", taskId }); } catch {} }
    else if (btn.classList.contains("cancel-btn"))
      { try { await chrome.runtime.sendMessage({ type: "CANCEL_TASK", taskId }); } catch {} }
    else if (btn.classList.contains("delete-btn"))
      { try { await chrome.runtime.sendMessage({ type: "DELETE_TASK", taskId }); } catch {} }
    else if (btn.classList.contains("continue-btn")) {
      try {
        const base = (btn.dataset.text || "").trim();
        input.value = `${base}\n\n추가 임무: `;
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 120) + "px";
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      } catch {}
    }
    else if (btn.classList.contains("edit-btn")) {
      // 완료된 임무: 입력창에 복사해서 재실행 준비
      try {
        input.value = btn.dataset.text || "";
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 120) + "px";
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      } catch {}
    }
    else if (btn.classList.contains("amend-btn")) {
      // 실행중/중단됨 임무: 카드 안에 인라인 수정 UI 표시
      const card = btn.closest(".task-card");
      if (!card || card.querySelector(".amend-row")) return;
      const amendRow = document.createElement("div");
      amendRow.className = "amend-row";
      const ta = document.createElement("textarea");
      ta.value = btn.dataset.text || "";
      ta.rows = 2;
      ta.placeholder = "새 지시를 입력하세요…";
      const confirmBtn = document.createElement("button");
      confirmBtn.className = "tbtn amend-btn";
      confirmBtn.textContent = "확인";
      const cancelABtn = document.createElement("button");
      cancelABtn.className = "tbtn";
      cancelABtn.textContent = "취소";
      amendRow.append(ta, confirmBtn, cancelABtn);
      card.appendChild(amendRow);
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);

      confirmBtn.addEventListener("click", async () => {
        const newText = ta.value.trim();
        if (!newText) return;
        try {
          await chrome.runtime.sendMessage({ type: "AMEND_TASK", taskId, amendment: newText });
        } catch {}
        amendRow.remove();
      });
      cancelABtn.addEventListener("click", () => amendRow.remove());
    }
  });

  // ── 왼쪽 임무 미니바 이벤트 ────────────────────────────────────────────
  miniListEl && miniListEl.addEventListener("click", async e => {
    const link = e.target.closest(".task-mini-link");
    const del = e.target.closest(".task-mini-del");
    if (link?.dataset.id) {
      const target = tasksEl.querySelector(`.task-card[data-id="${link.dataset.id}"]`);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
      }
      return;
    }
    if (del?.dataset.id) {
      try { await chrome.runtime.sendMessage({ type: "DELETE_TASK", taskId: del.dataset.id }); } catch {}
    }
  });
  clearAllBtn && clearAllBtn.addEventListener("click", async () => {
    try { await chrome.runtime.sendMessage({ type: "CLEAR_TASKS" }); } catch {}
  });

  // ── 임무 제출 ────────────────────────────────────────────────────────
  function submitTask() {
    const task = input.value.trim();
    if (!task) return;
    input.value = ""; input.style.height = "auto"; input.focus();
    try {
      chrome.runtime.sendMessage({ type: "RUN_TASK", task }, () => void chrome.runtime.lastError);
    } catch (e) {
      // Extension context invalidated (확장 리로드 시) → 무시
    }
  }

  // ── 에이전트 종료 ────────────────────────────────────────────────────
  async function endAgent() {
    try { await chrome.runtime.sendMessage({ type: "STOP_TASK" }); } catch {}
    await setEnabled(false);
  }

  // ── 런처 드래그 (상하좌우 2D) ──────────────────────────────────────
  let launcherDrag = null;
  launcher.addEventListener("pointerdown", e => {
    launcherDrag = { y: e.clientY, b: launcherBottom, x: e.clientX, r: launcherRight, moved: false };
    try { launcher.setPointerCapture(e.pointerId); } catch {}
    e.preventDefault();
  });
  launcher.addEventListener("pointermove", e => {
    if (!launcherDrag) return;
    const dy = launcherDrag.y - e.clientY;
    const dx = launcherDrag.x - e.clientX;
    if (Math.abs(dy) + Math.abs(dx) > 4) launcherDrag.moved = true;
    launcherBottom = Math.max(12, Math.min(window.innerHeight - 64, launcherDrag.b + dy));
    launcherRight  = Math.max(8,  Math.min(window.innerWidth  - 60, launcherDrag.r + dx));
    applyLauncherPos(); // bar가 열려있으면 applyLauncherPos 내부에서 applyBarPos 호출
  });
  function endLauncherDrag(e) {
    if (!launcherDrag) return;
    try { launcher.releasePointerCapture(e.pointerId); } catch {}
    const moved = launcherDrag.moved; launcherDrag = null;
    if (moved) { try { chrome.storage.local.set({ launcherBottom, launcherRight }); } catch {} }
    else showBar(true);
  }
  launcher.addEventListener("pointerup",     endLauncherDrag);
  launcher.addEventListener("pointercancel", () => { launcherDrag = null; });

  // ── 패널 드래그 (헤더 잡고 이동) ──────────────────────────────────────
  let barDrag = null;
  headEl && headEl.addEventListener("pointerdown", e => {
    if (window.innerWidth <= 640 || bar.hidden) return;
    if (e.target.closest("button,input,textarea")) return;
    const r = bar.getBoundingClientRect();
    barDrag = { x: e.clientX, y: e.clientY, l: r.left, t: r.top, moved: false };
    try { headEl.setPointerCapture(e.pointerId); } catch {}
    e.preventDefault();
  });
  headEl && headEl.addEventListener("pointermove", e => {
    if (!barDrag) return;
    const dx = e.clientX - barDrag.x;
    const dy = e.clientY - barDrag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) barDrag.moved = true;
    if (!barDrag.moved) return;
    const bW = bar.offsetWidth || 560;
    const bH = bar.offsetHeight || 760;
    let left = barDrag.l + dx;
    let top = barDrag.t + dy;
    left = Math.max(8, Math.min(window.innerWidth - bW - 8, left));
    top = Math.max(8, Math.min(window.innerHeight - bH - 8, top));
    bar.style.left = left + "px";
    bar.style.top = top + "px";
    bar.style.right = "auto";
    bar.style.bottom = "auto";
  });
  function endBarDrag(e) {
    if (!barDrag) return;
    try { headEl && headEl.releasePointerCapture(e.pointerId); } catch {}
    if (barDrag.moved) {
      const r = bar.getBoundingClientRect();
      barLeft = Math.round(r.left);
      barTop = Math.round(r.top);
      barManualPos = true;
      try { chrome.storage.local.set({ barLeft, barTop, barManualPos: true }); } catch {}
    }
    barDrag = null;
  }
  headEl && headEl.addEventListener("pointerup", endBarDrag);
  headEl && headEl.addEventListener("pointercancel", () => { barDrag = null; });

  // ── 패널 크기 변경 저장 (모든 사이트 공통) ─────────────────────────────
  if (typeof ResizeObserver !== "undefined") {
    const barResizeObs = new ResizeObserver(() => {
      if (bar.hidden || window.innerWidth <= 640) return;
      const r = bar.getBoundingClientRect();
      const w = Math.round(r.width);
      const h = Math.round(r.height);
      if (Math.abs(w - barWidth) < 2 && Math.abs(h - barHeight) < 2) return;
      barWidth = w;
      barHeight = h;
      try { chrome.storage.local.set({ barWidth, barHeight }); } catch {}
    });
    barResizeObs.observe(bar);
  }

  // ── 임무 목록 높이 조절 (드래그 핸들) ───────────────────────────────
  let resizing = null;
  resizeHandle.addEventListener("pointerdown", e => {
    resizing = { y: e.clientY, h: tasksHeight };
    try { resizeHandle.setPointerCapture(e.pointerId); } catch {}
    e.preventDefault();
  });
  resizeHandle.addEventListener("pointermove", e => {
    if (!resizing) return;
    // 핸들이 tasks-wrap 아래에 있으므로: 위로 드래그 = 줄이기, 아래로 드래그 = 늘리기
    const dy = e.clientY - resizing.y;
    tasksHeight = Math.max(80, Math.min(Math.round(window.innerHeight * 0.6), resizing.h + dy));
    applyTasksHeight();
  });
  function endResize(e) {
    if (!resizing) return;
    try { resizeHandle.releasePointerCapture(e.pointerId); } catch {}
    resizing = null;
    try { chrome.storage.local.set({ tasksHeight }); } catch {}
  }
  resizeHandle.addEventListener("pointerup",     endResize);
  resizeHandle.addEventListener("pointercancel", () => { resizing = null; });

  // ── 헤더 버튼 ────────────────────────────────────────────────────────
  minBtn.addEventListener("click", () => showBar(false));
  endBtn.addEventListener("click", endAgent);
  gearBtn.addEventListener("click", () => settings.classList.toggle("open"));

  saveBtn.addEventListener("click", async () => {
    try {
      const res = await chrome.runtime.sendMessage({ type: "SET_SERVER_URL", serverUrl: serverInput.value });
      if (res?.serverUrl) serverInput.value = res.serverUrl;
      saveBtn.textContent = "저장됨 ✓";
      setTimeout(() => (saveBtn.textContent = "저장"), 1500);
    } catch { saveBtn.textContent = "실패"; setTimeout(() => (saveBtn.textContent = "저장"), 1500); }
  });

  sendBtnEl.addEventListener("click", submitTask);
  input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitTask(); } });
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 120) + "px"; });

  // ── 확인 요청 ────────────────────────────────────────────────────────
  function resolveConfirm(approved) {
    confirmBox.classList.remove("open");
    if (pendingConfirm) { try { pendingConfirm({ approved }); } catch {} pendingConfirm = null; }
  }
  approveBtn.addEventListener("click", () => resolveConfirm(true));
  rejectBtn.addEventListener("click",  () => resolveConfirm(false));

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "AGENT_CONFIRM") {
      showBar(true);
      confirmMsg.textContent =
        "[확인 필요] " + (msg.message || "중요한 동작") +
        "\n동작: " + (msg.action || "") + "\n이유: " + (msg.reasoning || "");
      confirmBox.classList.add("open");
      pendingConfirm = sendResponse;
      return true;
    }
    if (msg.type === "AX_SYNC_STATE") {
      const enabledNow = !!msg.enabled;
      const openNow = !!msg.barOpen;
      applyEnabled(enabledNow);
      if (!enabledNow) {
        bar.hidden = true;
        render();
        return;
      }
      if (openNow) {
        bar.hidden = false;
        render();
        if (barManualPos && window.innerWidth > 640) applyManualBarPos();
        else applyBarPos();
        tasksWrap.scrollTop = tasksWrap.scrollHeight;
      } else {
        bar.hidden = true;
        render();
      }
    }
  });

  // ── 사이트 브리지 ────────────────────────────────────────────────────
  function openAboveAnchor(anchor) {
    if (!anchor || window.innerWidth <= 640) return false;
    applyBarSize();
    const bW = bar.offsetWidth || 560;
    const bH = bar.offsetHeight || 760;
    const W = window.innerWidth;
    const H = window.innerHeight;
    const width = Number(anchor.width || 0);
    const left0 = Number(anchor.left || 0);
    const top0 = Number(anchor.top || 0);
    let left = left0 + width / 2 - bW / 2;
    let top = top0 - bH - 8;
    if (top < 8) top = 8;
    if (left < 8) left = 8;
    if (left + bW > W - 8) left = W - bW - 8;
    if (top + bH > H - 8) top = H - bH - 8;
    bar.style.left = left + "px";
    bar.style.top = top + "px";
    bar.style.right = "auto";
    bar.style.bottom = "auto";
    return true;
  }

  window.addEventListener("message", event => {
    if (event.source !== window) return;
    const d = event.data;
    if (!d || d.__nasaroai !== "agent") return;
    if (d.type === "PING") window.postMessage({ __nasaroai: "agent", type: "READY" }, "*");
    else if (d.type === "OPEN") {
      window.postMessage({ __nasaroai: "agent", type: "READY" }, "*");
      setEnabled(true);
      showBar(true);
      // 에이전트 버튼에서 열었으면 버튼 위로 배치
      if (openAboveAnchor(d.anchor)) {
        barManualPos = true;
        const r = bar.getBoundingClientRect();
        barLeft = Math.round(r.left);
        barTop = Math.round(r.top);
        try { chrome.storage.local.set({ barLeft, barTop, barManualPos: true }); } catch {}
      }
    }
    else if (d.type === "CLOSE") endAgent();
  });
  window.postMessage({ __nasaroai: "agent", type: "READY" }, "*");
  broadcastAgentState(enabled);

  // ── storage.onChanged ────────────────────────────────────────────────
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.agentEnabled) applyEnabled(!!changes.agentEnabled.newValue);
    if (changes.agentTasks)   renderTasks(changes.agentTasks.newValue || []);
    if (changes.launcherBottom && typeof changes.launcherBottom.newValue === "number") {
      launcherBottom = changes.launcherBottom.newValue; applyLauncherPos();
    }
    if (changes.launcherRight && typeof changes.launcherRight.newValue === "number") {
      launcherRight = changes.launcherRight.newValue; applyLauncherPos();
    }
    if (changes.barLeft && typeof changes.barLeft.newValue === "number") barLeft = changes.barLeft.newValue;
    if (changes.barTop && typeof changes.barTop.newValue === "number") barTop = changes.barTop.newValue;
    if (changes.barManualPos !== undefined) barManualPos = !!changes.barManualPos.newValue;
    if (changes.barWidth && typeof changes.barWidth.newValue === "number") barWidth = changes.barWidth.newValue;
    if (changes.barHeight && typeof changes.barHeight.newValue === "number") barHeight = changes.barHeight.newValue;
    if (changes.tasksHeight && typeof changes.tasksHeight.newValue === "number") {
      tasksHeight = changes.tasksHeight.newValue; applyTasksHeight();
    }
    if (changes.latestNotification) {
      const n = changes.latestNotification.newValue;
      if (n && n.t && n.t !== prevNotifT) {
        prevNotifT = n.t;
        if (bar.hidden) showBubble(n.text, n.kind);
      }
    }
    // 탭 간 바 열기/닫기 동기화
    if (changes.barOpen !== undefined && enabled) {
      const open = !!changes.barOpen.newValue;
      bar.hidden = !open;
      render();
      if (open) {
        applyBarSize();
        if (barManualPos && window.innerWidth > 640) {
          applyManualBarPos();
        } else {
          applyBarPos();
        }
      }
    }
  });

  // ── 모바일 키보드 올라올 때 바/런처 위로 이동 ─────────────────────────
  function updateKbOffset() {
    if (!window.visualViewport) return;
    const vv = window.visualViewport;
    // 키보드 높이 = 전체 내부 높이 - 가시 뷰포트 높이 - 상단 오프셋
    const raw = window.innerHeight - vv.height - Math.max(0, vv.offsetTop);
    const newOffset = Math.max(0, raw);
    if (newOffset === kbOffset) return;
    kbOffset = newOffset;
    host.style.bottom = kbOffset + "px";
    applyLauncherPos();
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", updateKbOffset);
    window.visualViewport.addEventListener("scroll", updateKbOffset);
  }
  // input focus 시 즉시 반영 (키보드 애니메이션 완료 후 재계산)
  input && input.addEventListener("focus", () => {
    setTimeout(updateKbOffset, 100);
    setTimeout(updateKbOffset, 350);
  });
  input && input.addEventListener("blur", () => {
    setTimeout(updateKbOffset, 150);
  });

  // ── 초기화 ───────────────────────────────────────────────────────────
  // 창 크기 변경 시 패널 재배치
  window.addEventListener("resize", () => {
    if (bar.hidden) return;
    applyBarSize();
    if (barManualPos && window.innerWidth > 640) {
      applyManualBarPos();
    } else {
      applyBarPos();
    }
  });

  chrome.storage.local
    .get(["agentEnabled", "serverUrl", "agentTasks", "launcherBottom", "launcherRight", "barLeft", "barTop", "barManualPos", "barWidth", "barHeight", "tasksHeight", "barOpen"])
    .then(s => {
      if (s.serverUrl) serverInput.value = s.serverUrl;
      if (typeof s.launcherBottom === "number") launcherBottom = s.launcherBottom;
      if (typeof s.launcherRight  === "number") launcherRight  = s.launcherRight;
      if (typeof s.barLeft === "number") barLeft = s.barLeft;
      if (typeof s.barTop === "number") barTop = s.barTop;
      barManualPos = !!s.barManualPos;
      if (typeof s.barWidth === "number") barWidth = s.barWidth;
      if (typeof s.barHeight === "number") barHeight = s.barHeight;
      if (typeof s.tasksHeight    === "number") tasksHeight    = s.tasksHeight;
      applyLauncherPos();
      applyBarSize();
      applyTasksHeight();
      renderTasks(s.agentTasks || []);
      // storage 확인 완료 → 이제부터 render()가 실제로 동작
      initialized = true;
      applyEnabled(!!s.agentEnabled);
      // barOpen: 에이전트가 켜져 있고 바가 열린 상태였으면 복원
      if (!!s.agentEnabled && s.barOpen === true) {
        bar.hidden = false;
        render();
        const restore = () => {
          applyBarSize();
          if (barManualPos && window.innerWidth > 640) {
            applyManualBarPos();
          } else {
            applyBarPos();
          }
          tasksWrap.scrollTop = tasksWrap.scrollHeight;
          if (miniListEl) miniListEl.scrollTop = miniListEl.scrollHeight;
        };
        // 런처 DOM이 완전히 렌더된 후 위치 복원
        setTimeout(restore, 80);
        setTimeout(restore, 250);
      }
    })
    .catch(() => {});
})();
