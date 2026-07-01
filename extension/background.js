// Nasaro AI 에이전트 백그라운드 서비스 워커 (v3 — 임무 관리)
// 역할:
//  - content.js(하단 바)가 보낸 RUN_TASK를 받아 "그 탭"에서 작업 루프를 돌린다.
//  - 화면 스캔/실제 입력은 chrome.debugger(CDP)로 수행한다.
//  - 임무 상태는 chrome.storage.local(agentTasks)에 저장 → 모든 탭이 storage.onChanged로 공유한다.
//  - PAUSE_TASK / RESUME_TASK / CANCEL_TASK 메시지로 임무를 제어한다.

const DEFAULT_SERVER    = "";
const DEBUGGER_VERSION  = "1.3";
const MAX_ROUNDS        = 20;
const INTERNAL_URL_RE   = /^(chrome|edge|brave|about|chrome-extension|devtools|view-source|chrome-search):/i;

const attachedTabs = new Set();
const runningTabs  = new Set();   // Set<tabId>  — 현재 제어 중인 탭
const tabToTask    = new Map();   // tabId → taskId (활성 임무)
const pauseFlags   = new Set();   // Set<taskId>  — 루프 중단 대기
const cancelFlags  = new Set();   // Set<taskId>  — 루프 취소 요청
const amendMap     = new Map();   // taskId → string (실행 중 수정 지시)

// STARTUP_TIME: 이후에 정의될 _withTasks 초기화 직후에 cleanup 실행
const STARTUP_TIME = Date.now();

// ---- 유틸 ----
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function getServerUrl() {
  try {
    const { serverUrl } = await chrome.storage.local.get("serverUrl");
    const stored = (serverUrl || "").trim().replace(/\/+$/, "");
    if (stored) return stored;
    const tabs = await chrome.tabs.query({});
    for (const tab of tabs) {
      if (!tab.url || INTERNAL_URL_RE.test(tab.url)) continue;
      try {
        const u = new URL(tab.url);
        if (u.protocol === "http:" || u.protocol === "https:") {
          const base = u.origin;
          await chrome.storage.local.set({ serverUrl: base });
          return base;
        }
      } catch {}
    }
    return DEFAULT_SERVER;
  } catch { return DEFAULT_SERVER; }
}

async function showElementHighlight(tabId, rect) {
  if (!rect || !tabId) return;
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (r) => {
        const id = "__nasaro_highlight__";
        let el = document.getElementById(id);
        if (!el) {
          el = document.createElement("div");
          el.id = id;
          el.style.cssText = "position:fixed;pointer-events:none;z-index:2147483646;border:3px solid #7c3aed;border-radius:6px;box-shadow:0 0 0 4px rgba(124,58,237,.25);transition:opacity .3s;";
          document.documentElement.appendChild(el);
        }
        el.style.top = (r.top - 3) + "px";
        el.style.left = (r.left - 3) + "px";
        el.style.width = (r.width + 6) + "px";
        el.style.height = (r.height + 6) + "px";
        el.style.opacity = "1";
        clearTimeout(el._hideTimer);
        el._hideTimer = setTimeout(() => { el.style.opacity = "0"; }, 1600);
      },
      args: [rect],
    });
  } catch {}
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

function parseHost(url) {
  try { return new URL(String(url || "")).host || ""; } catch { return ""; }
}

function safeAgentId(id) {
  const s = String(id || "");
  if (!/^a\d+$/.test(s)) return "";
  return s;
}

function tsCreate(taskId, text, tabId, pageUrl) {
  return _withTasks(async (ts) => {
    // 활성(실행중/중단됨) 임무는 최대 5개 — 이미 5개이면 오류 표시 후 생성 안 함
    const active = ts.filter(t => t.status === "running" || t.status === "paused").length;
    if (active >= 5) throw new Error("MAX_ACTIVE");
    // push(append) — 최신이 마지막(UI에서 아래 = 최신)
    ts.push({
      id: taskId, text, status: "running", result: "", steps: [],
      tabId, pageUrl: pageUrl || "", pageHost: parseHost(pageUrl),
      createdAt: Date.now(), updatedAt: Date.now(),
    });
    // 완료된 기록은 최대 50개 유지
    if (ts.length > 55) ts.splice(0, ts.length - 55);
  });
}

function tsStep(taskId, text, kind, image) {
  return _withTasks(async (ts) => {
    const t = ts.find(x => x.id === taskId);
    if (!t) return;
    t.steps.push({ text, kind: kind || "step", t: Date.now(), image: image || null });
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

function tsClearAll() {
  return _withTasks(async (ts) => {
    ts.splice(0, ts.length);
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
  const safe = safeAgentId(agentId);
  if (!safe) return "({ found: false })";
  return `(() => {
    const el = document.querySelector('[data-agent-id="${safe}"]');
    if (!el) return { found: false };
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const r = el.getBoundingClientRect();
    return { found: true, x: r.left + r.width / 2, y: r.top + r.height / 2,
      rect: { top: Math.round(r.top), left: Math.round(r.left), width: Math.round(r.width), height: Math.round(r.height) } };
  })()`;
}

async function resolveElementCenter(tabId, agentId) {
  const safe = safeAgentId(agentId);
  if (!safe) return { found: false };
  const res = await sendCommand(tabId, "Runtime.evaluate", {
    expression: buildResolveExpression(safe), returnByValue: true,
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

const SCROLL_DOWN_EXPRESSION = `(() => {
  const step = Math.max(320, Math.min(900, Math.floor(window.innerHeight * 0.65)));
  const roots = [document.scrollingElement, document.documentElement, document.body].filter(Boolean);
  let moved = false;
  for (const el of roots) {
    const before = el.scrollTop;
    el.scrollBy({ top: step, left: 0, behavior: 'auto' });
    if (el.scrollTop !== before) moved = true;
  }
  if (!moved) {
    const nodes = Array.from(document.querySelectorAll('*')).filter(el => {
      const s = getComputedStyle(el);
      const oy = s.overflowY;
      return (oy === 'auto' || oy === 'scroll' || oy === 'overlay')
        && el.scrollHeight > el.clientHeight + 8;
    }).sort((a, b) => (b.clientHeight * b.clientWidth) - (a.clientHeight * a.clientWidth));
    for (const el of nodes.slice(0, 4)) {
      el.scrollBy({ top: step, left: 0, behavior: 'auto' });
    }
  }
  window.dispatchEvent(new Event('scroll', { bubbles: true }));
  return true;
})()`;

const SCROLL_UP_EXPRESSION = `(() => {
  const step = Math.max(320, Math.min(900, Math.floor(window.innerHeight * 0.65)));
  const roots = [document.scrollingElement, document.documentElement, document.body].filter(Boolean);
  for (const el of roots) {
    el.scrollBy({ top: -step, left: 0, behavior: 'auto' });
  }
  window.dispatchEvent(new Event('scroll', { bubbles: true }));
  return true;
})()`;

async function executeAction(tabId, action) {
  const type = action?.action;
  try {
    if (type === "click") {
      const safe = safeAgentId(action.target_id);
      if (!safe) return { success: false, error: "잘못된 요소 ID: " + action.target_id };
      const c = await resolveElementCenter(tabId, safe);
      if (!c.found) return { success: false, error: "요소 없음(클릭): " + safe };
      if (c.rect) await showElementHighlight(tabId, c.rect);
      await dispatchClickAt(tabId, c.x, c.y);
    } else if (type === "type") {
      const safe = safeAgentId(action.target_id);
      if (!safe) return { success: false, error: "잘못된 요소 ID: " + action.target_id };
      const c = await resolveElementCenter(tabId, safe);
      if (!c.found) return { success: false, error: "요소 없음(입력): " + safe };
      if (c.rect) await showElementHighlight(tabId, c.rect);
      await dispatchClickAt(tabId, c.x, c.y);
      await sendCommand(tabId, "Runtime.evaluate", {
        expression: `(() => { const el = document.querySelector('[data-agent-id="${safe}"]'); if (el) { el.focus(); if (el.select) el.select(); } })()`,
      });
      await sendCommand(tabId, "Input.insertText", { text: String(action.value ?? "") });
    } else if (type === "select") {
      const safe = safeAgentId(action.target_id);
      if (!safe) return { success: false, error: "잘못된 요소 ID: " + action.target_id };
      const val = JSON.stringify(String(action.value ?? ""));
      const expr = `(() => {
        const el = document.querySelector('[data-agent-id="${safe}"]');
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
      await sendCommand(tabId, "Runtime.evaluate", { expression: SCROLL_DOWN_EXPRESSION });
    } else if (type === "scroll_up") {
      await sendCommand(tabId, "Runtime.evaluate", { expression: SCROLL_UP_EXPRESSION });
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
async function getDeviceId() {
  try {
    const stored = await chrome.storage.local.get("nasaroDeviceId");
    if (stored.nasaroDeviceId) return stored.nasaroDeviceId;
    const id = crypto.randomUUID();
    await chrome.storage.local.set({ nasaroDeviceId: id });
    return id;
  } catch {
    return "extension-anonymous";
  }
}

async function postStep(serverUrl, task, scan, actionHistory, missionId) {
  const deviceId = await getDeviceId();
  let resp;
  try {
    resp = await fetch(serverUrl + "/agent/step", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Device-Id": deviceId,
        "X-Platform": "extension",
      },
      body: JSON.stringify({
        task,
        mission_id: missionId || "",
        elements: scan.elements || [],
        current_url: scan.url || "",
        action_history: actionHistory,
        user_id: deviceId,
      }),
    });
  } catch (e) {
    throw new Error("서버에 연결할 수 없습니다. 잠시 후 다시 시도해주세요.");
  }
  let data;
  try { data = await resp.json(); }
  catch { throw new Error("서버 응답을 해석하지 못했습니다."); }
  if (!resp.ok || data.status === "error") {
    const detail = data.detail;
    const detailMsg = typeof detail === "string" ? detail : detail?.message;
    throw new Error(data.message || detailMsg || "서버 오류 (" + resp.status + ")");
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
async function runTask(tabId, text, taskId, pageUrl) {
  const serverUrl = await getServerUrl();
  cancelFlags.delete(taskId);
  pauseFlags.delete(taskId);
  let taskText = text;

  // 외부 try/finally: 어떤 경로(early return 포함)로 나가도 cleanup 보장
  try {
    // 1) 태스크 생성 — MAX_ACTIVE이면 에러 throw (이제 _withTasks가 에러를 전파함)
    try {
      await tsCreate(taskId, taskText, tabId, pageUrl);
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

    let actionHistory = [];
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

        // 실행 중 수정 지시가 있으면 목표 갱신
        if (amendMap.has(taskId)) {
          const newGoal = amendMap.get(taskId);
          amendMap.delete(taskId);
          taskText = newGoal;
          actionHistory = [];
          await tsStep(taskId, `✏️ 지시 수정됨: ${newGoal}`, "info");
          await _withTasks(async ts => {
            const t = ts.find(x => x.id === taskId);
            if (t) t.text = newGoal;
          });
        }

        const scan = await scanCurrentTab(tabId);
        const data  = await postStep(serverUrl, taskText, scan, actionHistory, taskId);
        const reasoning = data.reasoning || "(이유 없음)";
        await tsStep(taskId, `(${round}/${MAX_ROUNDS}) ${reasoning}`, "step");

        if (data.handoff_required) {
          finalText = data.reasoning || "보안상 사용자가 직접 처리해야 합니다.";
          kind = "handoff";
          await tsStep(taskId, "⏸ " + finalText, "info");
          try {
            await chrome.scripting.executeScript({
              target: { tabId },
              func: (reason) => {
                window.postMessage({ __nasaroai: "agent", type: "HANDOFF", reason }, "*");
              },
              args: [finalText],
            });
          } catch (_) {}
          break;
        }
        if (data.done) {
          finalText = reasoning;
          kind = "success";
          break;
        }
        if (data.confirm_required) {
          const approved = await askConfirm(tabId, {
            title: "확인 필요",
            message: data.confirm_message || "결제·제출·은행 관련 작업일 수 있습니다. 계속할까요?",
          });
          if (!approved) {
            finalText = "🛑 사용자가 확인을 거부했습니다.";
            kind = "cancelled";
            await tsStep(taskId, finalText, "info");
            break;
          }
          await tsStep(taskId, "✅ 사용자 확인 완료", "info");
        }

        const exec = await executeAction(tabId, { action: data.action, target_id: data.target_id, value: data.value });
        let execResult = exec;
        if (!execResult.success && data.action !== "wait" && data.action !== "done") {
          await sleep(600);
          const rescan = await scanCurrentTab(tabId);
          if ((rescan.elements || []).length) {
            execResult = await executeAction(tabId, { action: data.action, target_id: data.target_id, value: data.value });
          }
        }
        actionHistory.push({ step: round, action: data.action, target: data.target_id, value: data.value, error: execResult.success ? null : execResult.error });
        if (execResult.success && data.action !== "wait") {
          try {
            const tabInfo = await chrome.tabs.get(tabId);
            const dataUrl = await chrome.tabs.captureVisibleTab(tabInfo.windowId, { format: "jpeg", quality: 50 });
            await tsStep(taskId, `📸 ${scan.url || data.action}`, "screenshot", dataUrl);
          } catch (_) {}
        }
        if (data.action === "scroll_down" || data.action === "scroll_up") await sleep(700);
        else await sleep(400);
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
      if (message.type === "SCHEDULE_TASK") {
        const mission = String(message.mission || "").trim();
        const runAt = Number(message.runAt) || 0;
        const label = String(message.label || "예약");
        const tabId = sender.tab?.id;
        if (!mission || !runAt) { sendResponse({ ok: false, label: "예약 실패" }); return; }
        const { agentScheduled = [] } = await chrome.storage.local.get("agentScheduled");
        agentScheduled.push({ id: "s" + Date.now(), mission, runAt, label, tabId: tabId ?? null });
        await chrome.storage.local.set({ agentScheduled });
        sendResponse({ ok: true, label });
        return true;

      } else if (message.type === "RUN_TASK") {
        const tabId = sender.tab?.id;
        const tabUrl = sender.tab?.url || "";
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
        runTask(tabId, message.task, taskId, tabUrl)
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

      } else if (message.type === "AMEND_TASK") {
        // 실행 중인 임무의 목표를 다음 스텝에서 교체
        const { taskId, amendment } = message;
        if (taskId && amendment) {
          amendMap.set(taskId, amendment);
          // 중단 상태였으면 자동으로 재개
          if (pauseFlags.has(taskId)) pauseFlags.delete(taskId);
        }
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

      } else if (message.type === "CLEAR_TASKS") {
        // 실행/중단 중인 임무들은 취소 플래그 후, 기록을 모두 삭제
        for (const taskId of tabToTask.values()) {
          pauseFlags.delete(taskId);
          cancelFlags.add(taskId);
        }
        await tsClearAll();
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
        const url = raw ? (raw.startsWith("http") ? raw : "https://" + raw) : "";
        if (url) await chrome.storage.local.set({ serverUrl: url });
        sendResponse({ ok: true, serverUrl: url || (await getServerUrl()) });

      } else if (message.type === "AX_REQUEST_SYNC") {
        const tabId = sender.tab?.id;
        if (tabId) await syncOneTabUi(tabId, sender.tab?.url || "");
        else await broadcastUiState({ ensureContent: true });
        sendResponse({ ok: true });

      } else {
        sendResponse({ ok: false, error: "알 수 없는 메시지" });
      }
    } catch (e) {
      sendResponse({ ok: false, finalText: String(e?.message ?? e), kind: "error" });
    }
  })();
  return true; // 비동기 응답
});

// ---- 모든 탭 동기화 ----
// enabled=true : 고아 인스턴스 제거 후 fresh content.js 주입 (런처 표시)
// enabled=false: 런처 DOM만 숨김 (content.js 재주입 없이 모든 인스턴스 즉시 제거)
async function syncAllTabs(enabled) {
  let allTabs = [];
  try { allTabs = await chrome.tabs.query({}); } catch { return; }
  for (const t of allTabs) {
    if (!t.id || !t.url || INTERNAL_URL_RE.test(t.url)) continue;
    try {
      if (enabled) {
        // 1) 고아 인스턴스 감지 & 제거 — 활성 인스턴스는 그대로 유지
        await chrome.scripting.executeScript({
          target: { tabId: t.id },
          func: () => {
            if (!window.__nasaroaiAgentInjected) return;
            // chrome.runtime.id가 undefined면 고아(orphaned) 인스턴스
            if (typeof chrome === "undefined" || !chrome.runtime?.id) {
              window.__nasaroaiAgentInjected = false;
              document.getElementById("__nasaroai_agent_host")?.remove();
            }
          },
        });
        // 2) content.js 주입 (guard로 이미 활성 탭은 스킵됨)
        await chrome.scripting.executeScript({ target: { tabId: t.id }, files: ["content.js"] });
      } else {
        // 비활성화: Shadow DOM 안의 런처/바를 직접 숨김 (고아 포함)
        await chrome.scripting.executeScript({
          target: { tabId: t.id },
          func: () => {
            const host = document.getElementById("__nasaroai_agent_host");
            if (!host?.shadowRoot) return;
            const s = host.shadowRoot;
            const launcher = s.getElementById("ax-launcher");
            const bar      = s.getElementById("ax-bar");
            if (launcher) launcher.classList.remove("show");
            if (bar)      bar.hidden = true;
          },
        });
      }
    } catch {}
  }
}

async function getUiSyncState() {
  try {
    const s = await chrome.storage.local.get([
      "agentEnabled",
      "barOpen",
      "launcherBottom",
      "launcherRight",
      "barLeft",
      "barTop",
      "barManualPos",
      "barWidth",
      "barHeight",
      "tasksHeight",
    ]);
    return {
      enabled: !!s.agentEnabled,
      barOpen: !!s.barOpen,
      launcherBottom: s.launcherBottom,
      launcherRight: s.launcherRight,
      barLeft: s.barLeft,
      barTop: s.barTop,
      barManualPos: s.barManualPos,
      barWidth: s.barWidth,
      barHeight: s.barHeight,
      tasksHeight: s.tasksHeight,
    };
  } catch {
    return { enabled: false, barOpen: false };
  }
}

async function broadcastUiState({ ensureContent = false } = {}) {
  const ui = await getUiSyncState();
  let tabs = [];
  try { tabs = await chrome.tabs.query({}); } catch { return; }
  for (const t of tabs) {
    if (!t.id || !t.url || INTERNAL_URL_RE.test(t.url)) continue;
    try {
      if (ensureContent && ui.enabled) {
        await chrome.scripting.executeScript({ target: { tabId: t.id }, files: ["content.js"] });
      }
      chrome.tabs.sendMessage(t.id, { type: "AX_SYNC_STATE", ...ui }, () => void chrome.runtime.lastError);
    } catch {}
  }
}

async function syncOneTabUi(tabId, url) {
  if (!tabId || !url || INTERNAL_URL_RE.test(url)) return;
  const ui = await getUiSyncState();
  try {
    if (ui.enabled) {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    }
    chrome.tabs.sendMessage(tabId, { type: "AX_SYNC_STATE", ...ui }, () => void chrome.runtime.lastError);
  } catch {}
}

async function ensureInjectedIfEnabled(tabId, url) {
  if (!tabId || !url || INTERNAL_URL_RE.test(url)) return;
  await syncOneTabUi(tabId, url);
}

// ---- storage.onChanged → 모든 탭 자동 동기화 ----
// 아이콘 클릭, Nasaro AI 사이트 버튼, 종료 버튼 등 어떤 경로로 agentEnabled가 바뀌어도
// 모든 탭에 즉시 반영된다.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.agentEnabled !== undefined) {
    syncAllTabs(!!changes.agentEnabled.newValue).catch(() => {});
  }
  if (changes.barOpen !== undefined) {
    broadcastUiState({ ensureContent: true }).catch(() => {});
  }
  const uiKeys = [
    "launcherBottom",
    "launcherRight",
    "barLeft",
    "barTop",
    "barManualPos",
    "barWidth",
    "barHeight",
    "tasksHeight",
  ];
  if (uiKeys.some(key => changes[key] !== undefined)) {
    broadcastUiState({ ensureContent: false }).catch(() => {});
  }
});

// ---- 아이콘 클릭 = 에이전트 켜기/끄기 토글 ----
// 스토리지 쓰기만 하면 storage.onChanged가 syncAllTabs를 자동 호출한다.
chrome.action.onClicked.addListener(async () => {
  let cur = false;
  try { cur = !!(await chrome.storage.local.get("agentEnabled")).agentEnabled; } catch {}
  try { await chrome.storage.local.set({ agentEnabled: !cur }); } catch {}
});

// 확장 시작/업데이트 직후 기존 탭도 즉시 동기화 (새로고침 없이)
chrome.runtime.onStartup.addListener(async () => {
  let enabled = false;
  try { enabled = !!(await chrome.storage.local.get("agentEnabled")).agentEnabled; } catch {}
  syncAllTabs(enabled).catch(() => {});
});
chrome.runtime.onInstalled.addListener(async () => {
  let enabled = false;
  try { enabled = !!(await chrome.storage.local.get("agentEnabled")).agentEnabled; } catch {}
  syncAllTabs(enabled).catch(() => {});
});

// ---- 사이트 이동 시 즉시 content 주입(새로고침 없이 따라오도록 보강) ----
const GOOGLE_URL_RE = /^https:\/\/(www\.)?google\.[a-z.]+\//i;

async function ensureInjectedWithRetries(tabId, url) {
  if (!tabId) return;
  await ensureInjectedIfEnabled(tabId, url || "");
  if (!url || !GOOGLE_URL_RE.test(url)) return;
  for (const delay of [400, 1200, 2500]) {
    setTimeout(() => {
      ensureInjectedIfEnabled(tabId, url).catch(() => {});
    }, delay);
  }
}

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.status !== "loading" && changeInfo.status !== "complete" && !changeInfo.url) return;
  const url = tab?.url || changeInfo.url || "";
  await ensureInjectedWithRetries(tabId, url);
});

// 탭 전환/신규 탭에서도 즉시 사용 가능하도록 보강
chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const tab = await chrome.tabs.get(tabId);
    await ensureInjectedIfEnabled(tabId, tab?.url || "");
  } catch {}
});
chrome.tabs.onCreated.addListener(async (tab) => {
  if (!tab?.id) return;
  await ensureInjectedIfEnabled(tab.id, tab.url || "");
  if (!tab.url || tab.url === "about:blank") {
    setTimeout(async () => {
      try {
        const t = await chrome.tabs.get(tab.id);
        await ensureInjectedIfEnabled(tab.id, t.url || "");
      } catch {}
    }, 600);
    setTimeout(async () => {
      try {
        const t = await chrome.tabs.get(tab.id);
        await ensureInjectedIfEnabled(tab.id, t.url || "");
      } catch {}
    }, 1800);
  }
});

// SPA 라우팅/히스토리 이동에서도 즉시 따라오도록 보강
chrome.webNavigation.onCommitted.addListener(async (details) => {
  if (details.frameId !== 0) return;
  const tab = await chrome.tabs.get(details.tabId).catch(() => null);
  await ensureInjectedIfEnabled(details.tabId, tab?.url || "");
});
chrome.webNavigation.onHistoryStateUpdated.addListener(async (details) => {
  if (details.frameId !== 0) return;
  const tab = await chrome.tabs.get(details.tabId).catch(() => null);
  await ensureInjectedIfEnabled(details.tabId, tab?.url || "");
});

// ---- 예약 임무 (1초 간격 체크) ----
async function runScheduledDueTasks() {
  try {
    const { agentScheduled = [], agentEnabled } = await chrome.storage.local.get(["agentScheduled", "agentEnabled"]);
    if (!agentEnabled || !agentScheduled.length) return;
    const now = Date.now();
    const due = agentScheduled.filter(t => Number(t.runAt) <= now);
    if (!due.length) return;
    const remaining = agentScheduled.filter(t => Number(t.runAt) > now);
    await chrome.storage.local.set({ agentScheduled: remaining });
    for (const item of due) {
      let tabId = item.tabId;
      if (tabId == null || runningTabs.has(tabId)) {
        const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
        tabId = tabs[0]?.id;
      }
      if (tabId == null || runningTabs.has(tabId)) continue;
      const tab = await chrome.tabs.get(tabId).catch(() => null);
      if (!tab || INTERNAL_URL_RE.test(tab.url || "")) continue;
      const taskId = "t" + Date.now() + "_" + Math.random().toString(36).slice(2, 7);
      runningTabs.add(tabId);
      tabToTask.set(tabId, taskId);
      runTask(tabId, item.mission, taskId, tab.url || "").catch(() => {});
    }
  } catch {}
}
setInterval(runScheduledDueTasks, 1000);
runScheduledDueTasks();
