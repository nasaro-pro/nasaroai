// ArenaX 에이전트 백그라운드 서비스 워커 (v2 — 하단 바 방식)
// 역할:
//  - content.js(하단 바)가 보낸 RUN_TASK를 받아 "그 탭"에서 작업 루프를 돌린다.
//  - 화면 스캔/실제 입력은 chrome.debugger(CDP)로 수행한다.
//  - 서버(/agent/step) 호출도 여기서 한다(host_permissions 덕분에 페이지 CORS 영향 없음).
// content.js는 UI만 담당한다.

const DEFAULT_SERVER = "https://arenax-4812.onrender.com";
const DEBUGGER_VERSION = "1.3";
const MAX_ROUNDS = 20;
const INTERNAL_URL_RE = /^(chrome|edge|brave|about|chrome-extension|devtools|view-source):/i;

const attachedTabs = new Set();
const runningTabs = new Set();

// ----------------------- 유틸 -----------------------
function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function getServerUrl() {
  try {
    const stored = await chrome.storage.local.get("serverUrl");
    const raw = (stored.serverUrl || DEFAULT_SERVER).trim().replace(/\/+$/, "");
    return raw || DEFAULT_SERVER;
  } catch (e) {
    return DEFAULT_SERVER;
  }
}

function sendToTab(tabId, message) {
  // content 스크립트가 아직 없거나 페이지가 바뀌어도 조용히 무시한다.
  try {
    chrome.tabs.sendMessage(tabId, message, () => void chrome.runtime.lastError);
  } catch (e) {
    /* ignore */
  }
}

function progress(tabId, text, kind) {
  sendToTab(tabId, { type: "AGENT_PROGRESS", text, kind });
}

// ----------------------- CDP 래퍼 -----------------------
function sendCommand(tabId, method, params = {}) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params, (result) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(result);
    });
  });
}

function attachDebugger(tabId) {
  return new Promise((resolve, reject) => {
    if (attachedTabs.has(tabId)) {
      resolve();
      return;
    }
    chrome.debugger.attach({ tabId }, DEBUGGER_VERSION, () => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      attachedTabs.add(tabId);
      resolve();
    });
  });
}

function detachDebugger(tabId) {
  return new Promise((resolve) => {
    if (!attachedTabs.has(tabId)) {
      resolve();
      return;
    }
    chrome.debugger.detach({ tabId }, () => {
      attachedTabs.delete(tabId);
      void chrome.runtime.lastError;
      resolve();
    });
  });
}

chrome.debugger.onDetach.addListener((source) => {
  if (source.tabId != null) attachedTabs.delete(source.tabId);
});
chrome.tabs.onRemoved.addListener((tabId) => {
  attachedTabs.delete(tabId);
  runningTabs.delete(tabId);
});

// ----------------------- 화면 스캔 -----------------------
// custom_agent.py DOM_INJECTOR_JS 와 같은 로직(매 스캔마다 data-agent-id 재부여,
// 뷰포트 교차=가시, 비밀번호 값 미수집).
const SCAN_EXPRESSION = `(() => {
  const TAGS = [
    'a','button','input','select','textarea',
    '[role="button"]','[role="link"]','[role="menuitem"]',
    '[role="option"]','[role="tab"]','[onclick]'
  ];
  const nodes = Array.from(document.querySelectorAll(TAGS.join(',')));
  const vw = window.innerWidth, vh = window.innerHeight;
  let counter = 0;
  const items = [];
  for (const el of nodes) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
    el.setAttribute('data-agent-id', 'a' + counter);
    counter++;
    const isVisible = (rect.bottom > 0 && rect.top < vh && rect.right > 0 && rect.left < vw);
    const isPassword = (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'password');
    items.push({
      id: el.getAttribute('data-agent-id'),
      tag: el.tagName.toLowerCase(),
      type: el.type || null,
      text: (el.innerText || el.textContent || '').trim().slice(0, 80),
      placeholder: el.placeholder || null,
      href: el.href || null,
      value: isPassword ? null : (el.value || null),
      has_password: isPassword,
      checked: (el.type === 'checkbox' || el.type === 'radio') ? el.checked : null,
      aria_label: el.getAttribute('aria-label') || null,
      name: el.name || null,
      is_visible: isVisible,
      rect: { top: Math.round(rect.top), left: Math.round(rect.left), width: Math.round(rect.width), height: Math.round(rect.height) }
    });
  }
  items.sort((a, b) => (b.is_visible ? 1 : 0) - (a.is_visible ? 1 : 0));
  return { elements: items.slice(0, 150), url: location.href, title: document.title };
})()`;

async function scanCurrentTab(tabId) {
  const res = await sendCommand(tabId, "Runtime.evaluate", {
    expression: SCAN_EXPRESSION,
    returnByValue: true,
    awaitPromise: false,
  });
  const value = res && res.result ? res.result.value : null;
  if (!value || !Array.isArray(value.elements)) return { elements: [], url: "", title: "" };
  return value;
}

// ----------------------- 액션 실행 -----------------------
// 클릭/입력은 실행 직전에 data-agent-id로 요소를 다시 찾아 최신 좌표를 계산한다(SPA 대비).
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
    expression: buildResolveExpression(agentId),
    returnByValue: true,
  });
  return res && res.result ? res.result.value : { found: false };
}

const KEY_MAP = {
  Enter: { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 },
  Tab: { key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 },
  Escape: { key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 },
  Backspace: { key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 },
  ArrowUp: { key: "ArrowUp", code: "ArrowUp", windowsVirtualKeyCode: 38 },
  ArrowDown: { key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40 },
  ArrowLeft: { key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37 },
  ArrowRight: { key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39 },
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
    await sendCommand(tabId, "Input.dispatchKeyEvent", { type: "keyUp", key: keyName });
    return;
  }
  await sendCommand(tabId, "Input.dispatchKeyEvent", { type: "keyDown", key: k.key, code: k.code, windowsVirtualKeyCode: k.windowsVirtualKeyCode });
  await sendCommand(tabId, "Input.dispatchKeyEvent", { type: "keyUp", key: k.key, code: k.code, windowsVirtualKeyCode: k.windowsVirtualKeyCode });
}

async function executeAction(tabId, action) {
  const type = action && action.action;
  try {
    if (type === "click") {
      const c = await resolveElementCenter(tabId, action.target_id);
      if (!c.found) return { success: false, error: "요소를 찾을 수 없습니다(클릭): " + action.target_id };
      await dispatchClickAt(tabId, c.x, c.y);
    } else if (type === "type") {
      const c = await resolveElementCenter(tabId, action.target_id);
      if (!c.found) return { success: false, error: "요소를 찾을 수 없습니다(입력): " + action.target_id };
      await dispatchClickAt(tabId, c.x, c.y);
      await sendCommand(tabId, "Runtime.evaluate", {
        expression: `(() => { const el = document.querySelector('[data-agent-id="${action.target_id}"]'); if (el) { el.focus(); if (el.select) el.select(); } })()`,
      });
      await sendCommand(tabId, "Input.insertText", { text: String(action.value ?? "") });
    } else if (type === "select") {
      const value = JSON.stringify(String(action.value ?? ""));
      const expr = `(() => {
        const el = document.querySelector('[data-agent-id="${action.target_id}"]');
        if (!el) return false;
        const want = ${value};
        let matched = false;
        for (const opt of el.options || []) {
          if (opt.value === want || (opt.textContent || '').trim() === want) { el.value = opt.value; matched = true; break; }
        }
        if (!matched) el.value = want;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return matched;
      })()`;
      const res = await sendCommand(tabId, "Runtime.evaluate", { expression: expr, returnByValue: true });
      if (!(res && res.result && res.result.value)) return { success: false, error: "select 옵션을 찾지 못했습니다: " + action.value };
    } else if (type === "scroll_down") {
      await sendCommand(tabId, "Runtime.evaluate", { expression: "window.scrollBy(0, 600)" });
    } else if (type === "scroll_up") {
      await sendCommand(tabId, "Runtime.evaluate", { expression: "window.scrollBy(0, -600)" });
    } else if (type === "press_key") {
      await dispatchKey(tabId, String(action.value || "Enter"));
    } else if (type === "navigate") {
      await sendCommand(tabId, "Page.navigate", { url: String(action.value || "") });
      await sleep(1200);
    } else if (type === "back") {
      await sendCommand(tabId, "Runtime.evaluate", { expression: "history.back()" });
    } else if (type === "wait") {
      const ms = parseInt(action.value, 10);
      await sleep(Number.isFinite(ms) ? ms : 1000);
    } else if (type === "done") {
      return { success: true, error: null };
    } else {
      return { success: false, error: "알 수 없는 액션: " + type };
    }
    return { success: true, error: null };
  } catch (err) {
    return { success: false, error: String(err && err.message ? err.message : err) };
  }
}

// ----------------------- 서버 호출 -----------------------
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
    throw new Error("서버에 연결할 수 없습니다. 서버 주소(설정)를 확인하세요.");
  }
  let data;
  try {
    data = await resp.json();
  } catch (e) {
    throw new Error("서버 응답을 해석하지 못했습니다.");
  }
  if (!resp.ok || data.status === "error") {
    throw new Error(data.message || (data.detail && data.detail.message) || "서버 오류 (" + resp.status + ")");
  }
  return data;
}

// 제출/결제 확인을 하단 바에 물어보고 응답(승인/취소)을 기다린다. 60초 무응답이면 취소.
function askConfirm(tabId, payload) {
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        resolve(false);
      }
    }, 60000);
    try {
      chrome.tabs.sendMessage(tabId, { type: "AGENT_CONFIRM", ...payload }, (resp) => {
        if (chrome.runtime.lastError) {
          if (!settled) {
            settled = true;
            clearTimeout(timer);
            resolve(false);
          }
          return;
        }
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          resolve(!!(resp && resp.approved));
        }
      });
    } catch (e) {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        resolve(false);
      }
    }
  });
}

// ----------------------- 작업 루프 -----------------------
async function runTask(tabId, task) {
  const serverUrl = await getServerUrl();

  try {
    await attachDebugger(tabId);
  } catch (e) {
    return { ok: false, finalText: "이 탭을 제어할 수 없습니다(디버거 연결 실패): " + (e.message || e), kind: "error" };
  }
  progress(tabId, "⚙️ 이 탭을 제어합니다. 상단에 디버깅 배너가 표시됩니다.", "info");

  const actionHistory = [];
  let finalText = "최대 단계(20단계)에 도달했습니다. 목표를 완전히 달성하지 못했을 수 있습니다.";
  let kind = "error";

  try {
    for (let round = 1; round <= MAX_ROUNDS; round++) {
      const scan = await scanCurrentTab(tabId);
      const data = await postStep(serverUrl, task, scan, actionHistory);
      const reasoning = data.reasoning || "(이유 없음)";
      progress(tabId, `(${round}/${MAX_ROUNDS}) ${reasoning}`, "step");

      if (data.handoff_required) {
        finalText = "🟡 사용자가 직접 진행해야 합니다.\n" + reasoning;
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
          action: data.action,
          reasoning,
        });
        if (!approved) {
          finalText = "🟡 사용자가 실행을 취소했습니다.";
          kind = "handoff";
          break;
        }
      }

      const exec = await executeAction(tabId, {
        action: data.action,
        target_id: data.target_id,
        value: data.value,
      });
      actionHistory.push({
        step: round,
        action: data.action,
        target: data.target_id,
        value: data.value,
        error: exec.success ? null : exec.error,
      });
      await sleep(700);
    }
  } catch (e) {
    finalText = "❌ " + (e.message || e);
    kind = "error";
  } finally {
    await detachDebugger(tabId);
  }

  return { ok: kind !== "error", finalText, kind };
}

// ----------------------- 메시지 라우팅 -----------------------
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      if (message.type === "RUN_TASK") {
        const tabId = sender.tab && sender.tab.id;
        if (tabId == null) {
          sendResponse({ ok: false, finalText: "탭 정보를 찾을 수 없습니다.", kind: "error" });
          return;
        }
        if (runningTabs.has(tabId)) {
          sendResponse({ ok: false, finalText: "이미 이 탭에서 작업이 진행 중입니다.", kind: "error" });
          return;
        }
        runningTabs.add(tabId);
        try {
          const result = await runTask(tabId, message.task);
          sendResponse(result);
        } finally {
          runningTabs.delete(tabId);
        }
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
      sendResponse({ ok: false, finalText: String(e && e.message ? e.message : e), kind: "error" });
    }
  })();
  return true; // 비동기 응답
});

// ----------------------- 아이콘 클릭 = 하단 바 토글 -----------------------
chrome.action.onClicked.addListener(async (tab) => {
  if (!tab || tab.id == null) return;
  if (tab.url && INTERNAL_URL_RE.test(tab.url)) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "TOGGLE_BAR" });
  } catch (e) {
    // 설치 전부터 열려 있던 탭은 content script가 없으므로 주입 후 토글한다.
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
      await chrome.tabs.sendMessage(tab.id, { type: "TOGGLE_BAR" });
    } catch (e2) {
      /* ignore */
    }
  }
});
