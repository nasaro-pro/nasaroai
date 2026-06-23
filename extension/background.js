// ArenaX 에이전트 백그라운드 서비스 워커
// 역할: 사용자가 보고 있는 "그 탭"을 chrome.debugger(CDP)로 직접 조작한다.
//  - 화면 스캔(DOM)         : Runtime.evaluate 로 상호작용 요소 수집
//  - 액션 실행(클릭/입력 등) : Input.dispatch* 로 "진짜 신뢰 입력 이벤트" 전송
// content script 주입 없이 전부 debugger 채널로 처리한다.

// ----------------------- 사이드패널 열기(기존 로직 유지) -----------------------
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch(console.error);

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setOptions({
    enabled: true,
    path: "sidepanel.html",
  });
});

// ----------------------- 디버거 연결 상태 추적 -----------------------
const DEBUGGER_VERSION = "1.3";
const attachedTabs = new Set();

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
      resolve({ success: true, already: true });
      return;
    }
    chrome.debugger.attach({ tabId }, DEBUGGER_VERSION, () => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      attachedTabs.add(tabId);
      resolve({ success: true, already: false });
    });
  });
}

function detachDebugger(tabId) {
  return new Promise((resolve) => {
    if (!attachedTabs.has(tabId)) {
      resolve({ success: true });
      return;
    }
    chrome.debugger.detach({ tabId }, () => {
      // detach 실패해도(이미 떨어졌거나 탭이 닫힘) 상태만 정리하고 넘어간다.
      attachedTabs.delete(tabId);
      resolve({ success: true });
    });
  });
}

// 사용자가 배너의 "취소"를 누르거나 탭이 닫히면 상태를 정리한다.
chrome.debugger.onDetach.addListener((source) => {
  if (source.tabId != null) {
    attachedTabs.delete(source.tabId);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  attachedTabs.delete(tabId);
});

// ----------------------- 화면 스캔 -----------------------
// custom_agent.py 의 DOM_INJECTOR_JS 와 같은 로직을 Runtime.evaluate 용 IIFE로 옮긴 것.
// 차이점/개선점:
//   - 매 스캔마다 data-agent-id 를 새로 덮어써 "이번 라운드"에서만 유효한 안정적 ID를 보장
//     (스캔→판단→실행이 한 라운드 안에서 끝나므로 stale ID 충돌 위험 제거).
//   - 가시성 판정을 "뷰포트와 교차하면 보임"으로 완화(원본의 '완전히 안쪽' 기준보다 실용적).
//   - [보안] input[type="password"] 의 value 는 절대 수집하지 않고 has_password 만 표시.
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
      rect: {
        top: Math.round(rect.top),
        left: Math.round(rect.left),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      }
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
  if (!value || !Array.isArray(value.elements)) {
    return { elements: [], url: "", title: "" };
  }
  return value;
}

// ----------------------- 액션 실행 -----------------------
// 핵심 보강(스펙 대비 개선): 클릭/입력은 스캔 시점 좌표를 신뢰하지 않고,
// "실행 직전에" data-agent-id 로 요소를 다시 찾아 scrollIntoView 후 최신 좌표를
// 계산한다. SPA에서 스캔과 실행 사이에 레이아웃이 바뀌어도 좌표가 틀어지지 않는다.
function buildResolveExpression(agentId) {
  // agentId 는 'a0' 형태의 안전한 값이라 문자열 보간이 안전하다.
  return `(() => {
    const el = document.querySelector('[data-agent-id="${agentId}"]');
    if (!el) return { found: false };
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const r = el.getBoundingClientRect();
    return {
      found: true,
      x: r.left + r.width / 2,
      y: r.top + r.height / 2,
      width: r.width,
      height: r.height
    };
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
  await sendCommand(tabId, "Input.dispatchMouseEvent", {
    type: "mouseMoved", x, y, buttons: 0,
  });
  await sendCommand(tabId, "Input.dispatchMouseEvent", {
    type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1,
  });
  await sendCommand(tabId, "Input.dispatchMouseEvent", {
    type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1,
  });
}

async function dispatchKey(tabId, keyName) {
  const k = KEY_MAP[keyName];
  if (!k) {
    // 한 글자 키 등은 그대로 시도
    await sendCommand(tabId, "Input.dispatchKeyEvent", { type: "keyDown", key: keyName });
    await sendCommand(tabId, "Input.dispatchKeyEvent", { type: "keyUp", key: keyName });
    return;
  }
  await sendCommand(tabId, "Input.dispatchKeyEvent", {
    type: "keyDown", key: k.key, code: k.code, windowsVirtualKeyCode: k.windowsVirtualKeyCode,
  });
  await sendCommand(tabId, "Input.dispatchKeyEvent", {
    type: "keyUp", key: k.key, code: k.code, windowsVirtualKeyCode: k.windowsVirtualKeyCode,
  });
}

async function executeAction(tabId, action) {
  const type = action && action.action;
  try {
    if (type === "click") {
      const center = await resolveElementCenter(tabId, action.target_id);
      if (!center.found) return { success: false, error: "요소를 찾을 수 없습니다(클릭): " + action.target_id };
      await dispatchClickAt(tabId, center.x, center.y);

    } else if (type === "type") {
      const center = await resolveElementCenter(tabId, action.target_id);
      if (!center.found) return { success: false, error: "요소를 찾을 수 없습니다(입력): " + action.target_id };
      // 1) 클릭으로 포커스 → 2) 기존 내용 전체 선택 → 3) 신뢰 입력(insertText)
      await dispatchClickAt(tabId, center.x, center.y);
      await sendCommand(tabId, "Runtime.evaluate", {
        expression: `(() => { const el = document.querySelector('[data-agent-id="${action.target_id}"]'); if (el) { el.focus(); if (el.select) el.select(); } })()`,
      });
      await sendCommand(tabId, "Input.insertText", { text: String(action.value ?? "") });

    } else if (type === "select") {
      // 네이티브 select 드롭다운은 OS 레벨로 렌더링되어 CDP 마우스로 다루기 까다롭다.
      // 이 경우에 한해 예외적으로 Runtime.evaluate 로 value 설정 + 이벤트 디스패치한다.
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
      const ok = res && res.result ? res.result.value : false;
      if (!ok) return { success: false, error: "select 옵션을 찾지 못했습니다: " + action.value };

    } else if (type === "scroll_down") {
      // 스크롤은 신뢰 입력 여부가 사이트 동작에 영향을 주지 않아 evaluate로 처리(안정적).
      await sendCommand(tabId, "Runtime.evaluate", { expression: "window.scrollBy(0, 600)" });

    } else if (type === "scroll_up") {
      await sendCommand(tabId, "Runtime.evaluate", { expression: "window.scrollBy(0, -600)" });

    } else if (type === "press_key") {
      await dispatchKey(tabId, String(action.value || "Enter"));

    } else if (type === "navigate") {
      await sendCommand(tabId, "Page.navigate", { url: String(action.value || "") });

    } else if (type === "back") {
      await sendCommand(tabId, "Runtime.evaluate", { expression: "history.back()" });

    } else if (type === "wait") {
      const ms = parseInt(action.value, 10);
      await new Promise((r) => setTimeout(r, Number.isFinite(ms) ? ms : 1000));

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

// ----------------------- 메시지 라우팅 -----------------------
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    try {
      if (message.type === "ATTACH_DEBUGGER") {
        const res = await attachDebugger(message.tabId);
        sendResponse(res);
      } else if (message.type === "DETACH_DEBUGGER") {
        const res = await detachDebugger(message.tabId);
        sendResponse(res);
      } else if (message.type === "SCAN") {
        const tabId = message.tabId;
        const data = await scanCurrentTab(tabId);
        sendResponse({ success: true, ...data });
      } else if (message.type === "EXECUTE") {
        const tabId = message.tabId;
        const res = await executeAction(tabId, message.action);
        sendResponse(res);
      } else {
        sendResponse({ success: false, error: "알 수 없는 메시지 타입" });
      }
    } catch (err) {
      sendResponse({ success: false, error: String(err && err.message ? err.message : err) });
    }
  })();
  // 비동기 응답을 위해 반드시 true 반환
  return true;
});

// ----------------------- 사이드패널 수명주기 정리 -----------------------
// 사이드패널이 열리면 포트를 연결하고, 닫히면 onDisconnect 로 모든 디버거를 정리한다.
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "sidepanel") return;
  port.onDisconnect.addListener(async () => {
    for (const tabId of Array.from(attachedTabs)) {
      await detachDebugger(tabId);
    }
  });
});
