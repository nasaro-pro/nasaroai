// ArenaX 에이전트 백그라운드 서비스 워커 (v3 — 임무 관리)
// 역할:
//  - content.js(하단 바)가 보낸 RUN_TASK를 받아 "그 탭"에서 작업 루프를 돌린다.
//  - 화면 스캔/실제 입력은 chrome.debugger(CDP)로 수행한다.
//  - 임무 상태는 chrome.storage.local(agentTasks)에 저장 → 모든 탭이 storage.onChanged로 공유한다.
//  - PAUSE_TASK / RESUME_TASK / CANCEL_TASK 메시지로 임무를 제어한다.

const DEFAULT_SERVER    = "https://arenax-4812.onrender.com";
const DEBUGGER_VERSION  = "1.3";
const MAX_ROUNDS        = 20;
const INTERNAL_URL_RE   = /^(chrome|edge|brave|about|chrome-extension|devtools|view-source):/i;

const attachedTabs = new Set();
const runningTabs  = new Set();   // Set<tabId>  — 현재 제어 중인 탭
const tabToTask    = new Map();   // tabId → taskId (활성 임무)
const pauseFlags   = new Set();   // Set<taskId>  — 루프 중단 대기
const cancelFlags  = new Set();   // Set<taskId>  — 루프 취소 요청

// STARTUP_TIME: 이후에 정의될 _withTasks 초기화 직후에 cleanup 실행
const STARTUP_TIME = Date.now();

// ---- 유틸 ----
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function getServerUrl() {
  try {
    const { serverUrl } = await chrome.storage.local.get("serverUrl");
    return (serverUrl || DEFAULT_SERVER).trim().replace(/\/+$/, "") || DEFAULT_SERVER;
  } catch { return DEFAULT_SERVER; }
}

function sendToTab(tabId, msg) {
  try { chrome.tabs.sendMessage(tabId, msg, () => void chrome.runtime.lastError); }
  catch {}
}

// ---- 임무 스토리지 (직렬화 쓰기 — 동시 쓰기 충돌 방지) ----
let _twChain = Promise.resolve();

function _withTasks(fn) {
  // raw promise 반환 → 호출자가 에러 받을 수 있음
  // chain은 .catch로 막아서 에러 시에도 다음 write가 block 안 됨
  const p = _twChain.then(async () => {
    const { agentTasks = [] } = await chrome.storage.local.get("agentTasks");
    const extra = (await fn(agentTasks)) || {};
    await chrome.storage.local.set({ agentTasks, ...extra });
  });
  _twChain = p.catch(() => {});
  return p;
}

// ---- 서비스워커 시작 시: stale 임무 정리 (STARTUP_TIME 이전 생성 임무만) ----
// _withTasks 체인을 이용해 race condition 없이 직렬 처리
_withTasks(async (ts) => {
  for (const t of ts) {
    if ((t.status === "running" || t.status === "paused") && (t.createdAt || 0) < STARTUP_TIME) {
      t.status  = "error";
      t.result  = "⚠️ 에이전트가 재시작됐습니다. 수정 후 다시 실행하세요.";
      t.updatedAt = STARTUP_TIME;
    }
  }
});

function tsCreate(taskId, text, tabId) {
  return _withTasks(async (ts) => {
    // 활성(실행중/중단됨) 임무는 최대 5개 — 이미 5개이면 오류 표시 후 생성 안 함
    const active = ts.filter(t => t.status === "running" || t.status === "paused").length;
    if (active >= 5) throw new Error("MAX_ACTIVE");
    // push(append) — 최신이 마지막(UI에서 아래 = 최신)
    ts.push({
      id: taskId, text, status: "running", result: "", steps: [],
      tabId, createdAt: Date.now(), updatedAt: Date.now(),
    });
    // 완료된 기록은 최대 50개 유지
    if (ts.length > 55) ts.splice(0, ts.length - 55);
  });
}

function tsStep(taskId, text, kind) {
  return _withTasks(async (ts) => {
    const t = ts.find(x => x.id === taskId);
    if (!t) return;
    t.steps.push({ text, kind: kind || "step", t: Date.now() });
    if (t.steps.length > 40) t.steps.splice(0, t.steps.length - 40);
    t.updatedAt = Date.now();
  });
}

function tsStatus(taskId, status) {
  return _withTasks(async (ts) => {
    const t = ts.find(x => x.id === taskId);
    if (t) { t.status = status; t.updatedAt = Date.now(); }
  });
}

function tsFinish(taskId, status, result) {
  return _withTasks(async (ts) => {
    const t = ts.find(x => x.id === taskId);
    if (t) { t.status = status; t.result = result; t.updatedAt = Date.now(); }
    return { latestNotification: { text: result, kind: status, t: Date.now() } };
  });
}

// ---- CDP 래퍼 ----
function rawSend(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params, result => {
      if (chrome.runtime.lastError) { reject(new Error(chrome.runtime.lastError.message)); return; }
      resolve(result);
    });
  });
}

// 페이지 이동 등으로 디버거가 떨어졌으면 한 번 재연결 후 재시도한다.
async function sendCommand(tabId, method, params = {}) {
  try {
    return await rawSend(tabId, method, params);
  } catch (e) {
    const msg = String(e?.message ?? e);
    if (/not attached|detached|target closed|No tab with given id/i.test(msg) && !cancelFlags.has(tabToTask.get(tabId))) {
      attachedTabs.delete(tabId);
      await attachDebugger(tabId);
      return await rawSend(tabId, method, params);
    }
    throw e;
  }
}

function attachDebugger(tabId) {
  return new Promise((resolve, reject) => {
    if (attachedTabs.has(tabId)) { resolve(); return; }
    chrome.debugger.attach({ tabId }, DEBUGGER_VERSION, () => {
      const err = chrome.runtime.lastError;
      if (err) {
        if (/already attached/i.test(err.message)) { attachedTabs.add(tabId); resolve(); return; }
        reject(new Error(err.message)); return;
      }
      attachedTabs.add(tabId);
      resolve();
    });
  });
}

function detachDebugger(tabId) {
  return new Promise(resolve => {
    if (!attachedTabs.has(tabId)) { resolve(); return; }
    chrome.debugger.detach({ tabId }, () => {
      attachedTabs.delete(tabId);
      void chrome.runtime.lastError;
      resolve();
    });
  });
}

chrome.debugger.onDetach.addListener(s => { if (s.tabId != null) attachedTabs.delete(s.tabId); });
chrome.tabs.onRemoved.addListener(tabId => {
  attachedTabs.delete(tabId);
  runningTabs.delete(tabId);
  tabToTask.delete(tabId);
});

// ---- 화면 스캔 ----
const SCAN_EXPRESSION = `(() => {
  const TAGS = ['a','button','input','select','textarea','[role="button"]','[role="link"]','[role="menuitem"]','[role="option"]','[role="tab"]','[onclick]'];
  const nodes = Array.from(document.querySelectorAll(TAGS.join(',')));
  const vw = window.innerWidth, vh = window.innerHeight;
  let counter = 0;
  const items = [];
  for (const el of nodes) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
    el.setAttribute('data-agent-id', 'a' + counter++);
    const vis = (rect.bottom > 0 && rect.top < vh && rect.right > 0 && rect.left < vw);
    const isPw = (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'password');
    items.push({
      id: el.getAttribute('data-agent-id'),
      tag: el.tagName.toLowerCase(), type: el.type || null,
      text: (el.innerText || el.textContent || '').trim().slice(0, 60),
      placeholder: el.placeholder || null, href: el.href || null,
      value: isPw ? null : (el.value || null),
      has_password: isPw,
      checked: (el.type === 'checkbox' || el.type === 'radio') ? el.checked : null,
      aria_label: el.getAttribute('aria-label') || null, name: el.name || null,
      is_visible: vis,
      rect: { top: Math.round(rect.top), left: Math.round(rect.left), width: Math.round(rect.width), height: Math.round(rect.height) }
    });
  }
  items.sort((a, b) => (b.is_visible ? 1 : 0) - (a.is_visible ? 1 : 0));
  return { elements: items.slice(0, 100), url: location.href, title: document.title };
})()`;

async function scanCurrentTab(tabId) {
  const res = await sendCommand(tabId, "Runtime.evaluate", {
    expression: SCAN_EXPRESSION, returnByValue: true, awaitPromise: false,
  });
  const val = res?.result?.value;
  if (!val || !Array.isArray(val.elements)) return { elements: [], url: "", title: "" };
  return val;
}

// ---- 액션 실행 ----
function buildResolveExpression(agentId) {
  return `(() => {
    const el = document.querySelector('[data-agent-id="${agentId}"]');
    if (!el) return { found: false };
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const r = el.getBoundingClientRect();
    return { found: true, x: r.left + r.width / 2, y: r.top + r.height / 2 };
  })()`;
}

async function resolveElementCenter(tabId, agentId) {
  const res = await sendCommand(tabId, "Runtime.evaluate", {
    expression: buildResolveExpression(agentId), returnByValue: true,
  });
  return res?.result?.value ?? { found: false };
}

const KEY_MAP = {
  Enter:     { key: "Enter",     code: "Enter",     windowsVirtualKeyCode: 13 },
  Tab:       { key: "Tab",       code: "Tab",       windowsVirtualKeyCode: 9  },
  Escape:    { key: "Escape",    code: "Escape",    windowsVirtualKeyCode: 27 },
  Backspace: { key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8  },
  ArrowUp:   { key: "ArrowUp",   code: "ArrowUp",   windowsVirtualKeyCode: 38 },
  ArrowDown: { key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40 },
};

async function dispatchClickAt(tabId, x, y) {
  await sendCommand(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, buttons: 0 });
  await sendCommand(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
  await sendCommand(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
}

async function dispatchKey(tabId, keyName) {
  const k = KEY_MAP[keyName];
  if (!k) {
    await sendCommand(tabId, "Input.dispatchKeyEvent", { type: "keyDown", key: keyName });
    await sendCommand(tabId, "Input.dispatchKeyEvent", { type: "keyUp",   key: keyName });
    return;
  }
  await sendCommand(tabId, "Input.dispatchKeyEvent", { type: "keyDown", ...k });
  await sendCommand(tabId, "Input.dispatchKeyEvent", { type: "keyUp",   ...k });
}

async function executeAction(tabId, action) {
  const type = action?.action;
  try {
    if (type === "click") {
      const c = await resolveElementCenter(tabId, action.target_id);
      if (!c.found) return { success: false, error: "요소 없음(클릭): " + action.target_id };
      await dispatchClickAt(tabId, c.x, c.y);
    } else if (type === "type") {
      const c = await resolveElementCenter(tabId, action.target_id);
      if (!c.found) return { success: false, error: "요소 없음(입력): " + action.target_id };
      await dispatchClickAt(tabId, c.x, c.y);
      await sendCommand(tabId, "Runtime.evaluate", {
        expression: `(() => { const el = document.querySelector('[data-agent-id="${action.target_id}"]'); if (el) { el.focus(); if (el.select) el.select(); } })()`,
      });
      await sendCommand(tabId, "Input.insertText", { text: String(action.value ?? "") });
    } else if (type === "select") {
      const val = JSON.stringify(String(action.value ?? ""));
      const expr = `(() => {
        const el = document.querySelector('[data-agent-id="${action.target_id}"]');
        if (!el) return false;
        const want = ${val};
        let ok = false;
        for (const opt of el.options || []) {
          if (opt.value === want || (opt.textContent||'').trim() === want) { el.value = opt.value; ok = true; break; }
        }
        if (!ok) el.value = want;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return ok;
      })()`;
      const res = await sendCommand(tabId, "Runtime.evaluate", { expression: expr, returnByValue: true });
      if (!res?.result?.value) return { success: false, error: "select 옵션 없음: " + action.value };
    } else if (type === "scroll_down") {
      await sendCommand(tabId, "Runtime.evaluate", { expression: "window.scrollBy(0,600)" });
    } else if (type === "scroll_up") {
      await sendCommand(tabId, "Runtime.evaluate", { expression: "window.scrollBy(0,-600)" });
    } else if (type === "press_key") {
      await dispatchKey(tabId, String(action.value || "Enter"));
    } else if (type === "navigate") {
      await sendCommand(tabId, "Page.navigate", { url: String(action.value || "") });
      await sleep(1200);
    } else if (type === "back") {
      await sendCommand(tabId, "Runtime.evaluate", { expression: "history.back()" });
    } else if (type === "wait") {
      await sleep(Number.isFinite(parseInt(action.value, 10)) ? parseInt(action.value, 10) : 1000);
    } else if (type === "done") {
      return { success: true, error: null };
    } else {
      return { success: false, error: "알 수 없는 액션: " + type };
    }
    return { success: true, error: null };
  } catch (err) {
    return { success: false, error: String(err?.message ?? err) };
  }
}

// ---- 서버 호출 ----
async function postStep(serverUrl, task, scan, actionHistory) {
  let resp;
  try {
    resp = await fetch(serverUrl + "/agent/step", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task,
        elements: scan.elements || [],
        current_url: scan.url || "",
        action_history: actionHistory,
      }),
    });
  } catch (e) {
    throw new Error("서버에 연결할 수 없습니다. 설정에서 서버 주소를 확인하세요.");
  }
  let data;
  try { data = await resp.json(); }
  catch { throw new Error("서버 응답을 해석하지 못했습니다."); }
  if (!resp.ok || data.status === "error") {
    throw new Error(data.message || data.detail?.message || "서버 오류 (" + resp.status + ")");
  }
  return data;
}

// ---- 확인 요청 (결제/제출 등) ----
function askConfirm(tabId, payload) {
  return new Promise(resolve => {
    let settled = false;
    const timer = setTimeout(() => { if (!settled) { settled = true; resolve(false); } }, 60000);
    try {
      chrome.tabs.sendMessage(tabId, { type: "AGENT_CONFIRM", ...payload }, resp => {
        void chrome.runtime.lastError;
        if (!settled) { settled = true; clearTimeout(timer); resolve(!!(resp?.approved)); }
      });
    } catch { if (!settled) { settled = true; clearTimeout(timer); resolve(false); } }
  });
}

// ---- 작업 루프 ----
async function runTask(tabId, text, taskId) {
  const serverUrl = await getServerUrl();
  cancelFlags.delete(taskId);
  pauseFlags.delete(taskId);

  // 외부 try/finally: 어떤 경로(early return 포함)로 나가도 cleanup 보장
  try {
    // 1) 태스크 생성 — MAX_ACTIVE이면 에러 throw (이제 _withTasks가 에러를 전파함)
    try {
      await tsCreate(taskId, text, tabId);
    } catch (e) {
      const msg = String(e?.message ?? e);
      if (msg === "MAX_ACTIVE") {
        return { ok: false, finalText: "⚠️ 활성 임무가 5개입니다. 먼저 완료/취소 후 시도하세요.", kind: "error" };
      }
      return { ok: false, finalText: "임무 생성 실패: " + msg, kind: "error" };
    }

    // 2) 디버거 연결
    try {
      await attachDebugger(tabId);
    } catch (e) {
      const msg = "디버거 연결 실패: " + (e.message || e);
      await tsFinish(taskId, "error", msg);
      return { ok: false, finalText: msg, kind: "error" };
      // → finally 블록이 cleanup 처리
    }

    await tsStep(taskId, "⚙️ 탭 제어 시작 (상단에 디버깅 배너 표시됨)", "info");

    const actionHistory = [];
    let finalText = "최대 단계(" + MAX_ROUNDS + "단계)에 도달했습니다. 목표를 완전히 달성하지 못했을 수 있습니다.";
    let kind = "error";

    // 3) 메인 루프
    try {
      for (let round = 1; round <= MAX_ROUNDS; round++) {
        if (cancelFlags.has(taskId)) {
          finalText = "🛑 사용자가 취소했습니다.";
          kind = "cancelled";
          break;
        }

        if (pauseFlags.has(taskId)) {
          await tsStep(taskId, "⏸ 중단됨 — 재개를 기다리는 중...", "info");
          await tsStatus(taskId, "paused");
          while (pauseFlags.has(taskId) && !cancelFlags.has(taskId)) await sleep(400);
          if (cancelFlags.has(taskId)) { finalText = "🛑 사용자가 취소했습니다."; kind = "cancelled"; break; }
          await tsStep(taskId, "▶ 재개됨", "info");
          await tsStatus(taskId, "running");
        }

        const scan = await scanCurrentTab(tabId);
        const data  = await postStep(serverUrl, text, scan, actionHistory);
        const reasoning = data.reasoning || "(이유 없음)";
        await tsStep(taskId, `(${round}/${MAX_ROUNDS}) ${reasoning}`, "step");

        if (data.handoff_required) {
          finalText = "🟡 직접 진행 필요: " + reasoning;
          kind = "handoff";
          break;
        }
        if (data.done) {
          finalText = reasoning;
          kind = "success";
          break;
        }
        if (data.confirm_required) {
          const approved = await askConfirm(tabId, {
            message: data.confirm_message || "중요한 동작을 실행하려고 합니다.",
            action: data.action, reasoning,
          });
          if (!approved) { finalText = "🟡 사용자가 실행을 취소했습니다."; kind = "cancelled"; break; }
        }

        const exec = await executeAction(tabId, { action: data.action, target_id: data.target_id, value: data.value });
        actionHistory.push({ step: round, action: data.action, target: data.target_id, value: data.value, error: exec.success ? null : exec.error });
        await sleep(400);
      }
    } catch (e) {
      finalText = "❌ " + (e.message || e);
      kind = "error";
    }

    await tsFinish(taskId, kind, finalText);
    return { ok: kind === "success", finalText, kind };

  } finally {
    // 어떤 경로(early return, throw, 정상 종료)든 항상 실행
    tabToTask.delete(tabId);
    runningTabs.delete(tabId);
    await detachDebugger(tabId);
  }
}

// ---- 메시지 라우팅 ----
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      if (message.type === "RUN_TASK") {
        const tabId = sender.tab?.id;
        if (tabId == null) { sendResponse({ ok: false, finalText: "탭 정보를 찾을 수 없습니다.", kind: "error" }); return; }
        if (runningTabs.has(tabId)) {
          sendResponse({ ok: false, finalText: "⚠️ 이미 이 탭에서 작업 중입니다.", kind: "error" });
          return;
        }
        // 활성 임무 5개 제한 사전 체크
        try {
          const { agentTasks = [] } = await chrome.storage.local.get("agentTasks");
          const active = agentTasks.filter(t => t.status === "running" || t.status === "paused").length;
          if (active >= 5) {
            sendResponse({ ok: false, finalText: "⚠️ 활성 임무가 5개입니다. 임무를 완료/취소 후 다시 시도하세요.", kind: "error" });
            return;
          }
        } catch {}

        const taskId = "t" + Date.now() + "_" + Math.random().toString(36).slice(2, 7);
        runningTabs.add(tabId);
        tabToTask.set(tabId, taskId);
        // runTask의 외부 finally가 runningTabs/tabToTask cleanup을 보장함
        runTask(tabId, message.task, taskId)
          .then(sendResponse)
          .catch(e => {
            sendResponse({ ok: false, finalText: String(e?.message ?? e), kind: "error" });
          });

      } else if (message.type === "PAUSE_TASK") {
        pauseFlags.add(message.taskId);
        sendResponse({ ok: true });

      } else if (message.type === "RESUME_TASK") {
        pauseFlags.delete(message.taskId);
        sendResponse({ ok: true });

      } else if (message.type === "CANCEL_TASK") {
        pauseFlags.delete(message.taskId);
        cancelFlags.add(message.taskId);
        sendResponse({ ok: true });

      } else if (message.type === "DELETE_TASK") {
        // 실행/중단 중이면 먼저 취소 후 스토리지에서 삭제
        const { taskId } = message;
        pauseFlags.delete(taskId);
        cancelFlags.add(taskId);
        await _withTasks(async (ts) => {
          const idx = ts.findIndex(x => x.id === taskId);
          if (idx >= 0) ts.splice(idx, 1);
        });
        sendResponse({ ok: true });

      } else if (message.type === "STOP_TASK") {
        // "에이전트 종료" — 모든 실행 중 임무 취소
        for (const taskId of tabToTask.values()) {
          pauseFlags.delete(taskId);
          cancelFlags.add(taskId);
        }
        sendResponse({ ok: true });

      } else if (message.type === "GET_SERVER_URL") {
        sendResponse({ serverUrl: await getServerUrl() });

      } else if (message.type === "SET_SERVER_URL") {
        const raw = String(message.serverUrl || "").trim().replace(/\/+$/, "");
        const url = raw ? (raw.startsWith("http") ? raw : "https://" + raw) : DEFAULT_SERVER;
        await chrome.storage.local.set({ serverUrl: url });
        sendResponse({ ok: true, serverUrl: url });

      } else {
        sendResponse({ ok: false, error: "알 수 없는 메시지" });
      }
    } catch (e) {
      sendResponse({ ok: false, finalText: String(e?.message ?? e), kind: "error" });
    }
  })();
  return true; // 비동기 응답
});

// ---- 아이콘 클릭 = 에이전트 켜기/끄기 토글 ----
// 켜는 경우, 이미 열려 있는 모든 탭에 content.js를 주입한다.
// 설치 전 열린 탭은 manifest content_scripts로 자동 주입이 안 되기 때문에 직접 주입이 필요하다.
chrome.action.onClicked.addListener(async (tab) => {
  let cur = false;
  try { cur = !!(await chrome.storage.local.get("agentEnabled")).agentEnabled; } catch {}
  const next = !cur;
  try { await chrome.storage.local.set({ agentEnabled: next }); } catch {}

  if (next) {
    // 모든 열려 있는 탭에 content.js 주입 시도
    let allTabs = [];
    try { allTabs = await chrome.tabs.query({}); } catch {}
    for (const t of allTabs) {
      if (!t.id || !t.url || INTERNAL_URL_RE.test(t.url)) continue;
      try {
        await chrome.scripting.executeScript({ target: { tabId: t.id }, files: ["content.js"] });
      } catch { /* 이미 주입됐거나 제한된 페이지면 무시 */ }
    }
  }
});
