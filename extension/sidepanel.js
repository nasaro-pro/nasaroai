// [보안 메모]
//  - 학번/비밀번호 같은 민감정보는 서버로 전송되지 않는다. background.js의 스캔이
//    input[type="password"]의 value를 수집하지 않고 has_password 플래그만 보낸다.
//  - 로그인/캡차/결제 폼이 감지되면 서버가 handoff_required로 즉시 정지하고,
//    그 단계는 사용자가 직접 진행한다(아래 루프에서 handoff 처리).
//  - 향후: 최종 제출(결제/신청 확정) 액션 직전 "사용자 확인 게이트"를 추가할 계획(이번 범위 아님).
let serverUrl = "https://arenax-4812.onrender.com";
let agentInFlight = false;
let agentHistory = [];

const MAX_ROUNDS = 20;

// 사이드패널이 닫히면 background가 onDisconnect로 디버거를 정리하도록 포트 연결.
const bgPort = chrome.runtime.connect({ name: "sidepanel" });

function sendToBackground(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(response);
    });
  });
}

const serverUrlInput = document.getElementById("serverUrl");
const saveServerBtn = document.getElementById("saveServerBtn");
const urlSettingToggle = document.getElementById("urlSettingToggle");
const urlSettingArea = document.getElementById("urlSettingArea");
const statusBar = document.getElementById("statusBar");
const historyArea = document.getElementById("historyArea");
const taskInput = document.getElementById("taskInput");
const sendBtn = document.getElementById("sendBtn");

urlSettingToggle.addEventListener("click", () => {
  const isOpen = urlSettingArea.classList.toggle("open");
  urlSettingToggle.textContent = isOpen ? "서버 설정 ▲" : "서버 설정 ▼";
});

saveServerBtn.addEventListener("click", async () => {
  const raw = serverUrlInput.value.trim().replace(/\/$/, "");
  if (!raw) return;

  serverUrl = raw.startsWith("http") ? raw : `http://${raw}`;
  serverUrlInput.value = serverUrl;

  try {
    await chrome.storage.local.set({ serverUrl });
    saveServerBtn.textContent = "저장됨 ✓";
  } catch (e) {
    saveServerBtn.textContent = "저장 실패";
  } finally {
    setTimeout(() => {
      saveServerBtn.textContent = "저장";
    }, 1500);
  }
});

document.addEventListener("DOMContentLoaded", async () => {
  try {
    const stored = await chrome.storage.local.get("serverUrl");
    if (stored.serverUrl) {
      serverUrl = stored.serverUrl;
      serverUrlInput.value = serverUrl;
    }
  } catch (e) {
    console.warn("서버 URL 불러오기 실패:", e);
  }
  renderHistory();
});

function setStatus(text, type = "idle") {
  statusBar.textContent = text;
  statusBar.dataset.type = type;
}

const STATUS_LABEL = { success: "완료", error: "실패", loading: "진행 중", handoff: "직접 진행" };

function renderHistory() {
  historyArea.innerHTML = "";

  if (!agentHistory.length) {
    const empty = document.createElement("div");
    empty.className = "empty-msg";
    empty.textContent = "에이전트에게 웹 작업을 지시하세요.\n어떤 사이트에서든 이 패널에서 작업할 수 있습니다.";
    historyArea.appendChild(empty);
    return;
  }

  agentHistory.forEach(item => {
    const card = document.createElement("article");
    card.className = `agent-card ${item.status}`;
    card.dataset.id = item.id;

    const header = document.createElement("div");
    header.className = "card-header";

    const taskEl = document.createElement("div");
    taskEl.className = "card-task";
    taskEl.textContent = item.task;

    const badge = document.createElement("span");
    badge.className = `card-badge ${item.status}`;
    badge.textContent = STATUS_LABEL[item.status] || item.status;

    header.appendChild(taskEl);
    header.appendChild(badge);

    const timeEl = document.createElement("div");
    timeEl.className = "card-time";
    timeEl.textContent = item.timestamp;

    const body = document.createElement("div");
    body.className = "card-body";
    body.textContent = item.status === "loading" ? "처리 중..." : (item.result || "결과 없음");

    card.appendChild(header);
    card.appendChild(timeEl);
    card.appendChild(body);
    historyArea.appendChild(card);
  });

  historyArea.scrollTop = historyArea.scrollHeight;
}

function updateCard(id, result, status) {
  const item = agentHistory.find(h => h.id === id);
  if (item) {
    item.result = result;
    item.status = status;
  }
  renderHistory();
}

const INTERNAL_URL_RE = /^(chrome|edge|brave|about|chrome-extension|devtools):/i;

// 활성 탭이 currentTabId와 다르면(새 탭/팝업으로 포커스가 옮겨감) 새 탭에 디버거를
// 붙이고 이전 탭은 떼어 배너가 중복되지 않게 한다. 실패하면 기존 탭을 유지한다.
async function followActiveTab(currentTabId) {
  try {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    const tab = tabs && tabs[0];
    if (!tab || tab.id == null || tab.id === currentTabId) return currentTabId;
    if (tab.url && INTERNAL_URL_RE.test(tab.url)) return currentTabId;

    const attachResp = await sendToBackground({ type: "ATTACH_DEBUGGER", tabId: tab.id });
    if (!attachResp || !attachResp.success) return currentTabId;

    if (currentTabId != null) {
      try {
        await sendToBackground({ type: "DETACH_DEBUGGER", tabId: currentTabId });
      } catch (e) {
        /* 무시 */
      }
    }
    return tab.id;
  } catch (e) {
    return currentTabId;
  }
}

async function postStep(task, scan, actionHistory) {
  let response;
  try {
    response = await fetch(`${serverUrl}/agent/step`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task,
        elements: scan.elements || [],
        current_url: scan.url || "",
        action_history: actionHistory,
      }),
    });
  } catch (networkErr) {
    throw new Error("서버에 연결할 수 없습니다. 서버 주소를 확인해주세요.");
  }

  let data;
  try {
    data = await response.json();
  } catch (parseErr) {
    throw new Error("서버 응답을 해석하지 못했습니다.");
  }

  if (!response.ok || data.status === "error") {
    throw new Error(data.message || data.detail?.message || `서버 오류 (${response.status})`);
  }
  return data;
}

async function sendTask() {
  if (agentInFlight) return;

  const task = taskInput.value.trim();
  if (!task) return;

  agentInFlight = true;
  taskInput.disabled = true;
  sendBtn.disabled = true;
  taskInput.value = "";
  taskInput.style.height = "auto";

  const id = Date.now();
  const timestamp = new Date().toLocaleString("ko-KR");
  agentHistory.push({ id, task, result: "", status: "loading", timestamp });
  renderHistory();
  setStatus("⏳ 작업 중...", "loading");

  let tabId = null;
  try {
    // 1) 현재 활성 탭 확보
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    const tab = tabs && tabs[0];
    if (!tab || tab.id == null) throw new Error("활성 탭을 찾을 수 없습니다.");
    tabId = tab.id;
    if (tab.url && INTERNAL_URL_RE.test(tab.url)) {
      throw new Error("브라우저 내부 페이지는 제어할 수 없습니다. 일반 웹사이트로 이동한 뒤 다시 시도하세요.");
    }

    // 2) 디버거 연결 (상단에 "ArenaX가 이 브라우저를 디버깅 중입니다" 배너가 뜸)
    const attachResp = await sendToBackground({ type: "ATTACH_DEBUGGER", tabId });
    if (!attachResp || !attachResp.success) {
      throw new Error((attachResp && attachResp.error) || "디버거 연결에 실패했습니다.");
    }
    updateCard(
      id,
      "⚠️ 이 탭을 제어하기 위해 디버깅을 시작했습니다(상단 배너 표시).\n작업이 끝나면 자동으로 해제됩니다.\n\n진행 중...",
      "loading"
    );

    // 3) 인식 → 판단 → 실행 루프
    const actionHistory = [];
    let finalText = "최대 단계(20단계)에 도달했습니다. 목표를 완전히 달성하지 못했을 수 있습니다.";
    let finalStatus = "error";

    for (let round = 1; round <= MAX_ROUNDS; round++) {
      // a-0) 새 탭/팝업 추적: 활성 탭이 바뀌었으면(예: target=_blank 클릭으로
      //      새 탭이 열려 포커스됨) 그 탭으로 제어를 옮긴다.
      tabId = await followActiveTab(tabId);

      // a) 화면 스캔
      const scan = await sendToBackground({ type: "SCAN", tabId });
      if (!scan || !scan.success) {
        throw new Error((scan && scan.error) || "화면을 읽지 못했습니다.");
      }

      // b) 서버 두뇌에 다음 액션 질의
      const data = await postStep(task, scan, actionHistory);
      const reasoning = data.reasoning || "(이유 없음)";

      // d) 진행 상황 표시
      updateCard(id, `(${round}/${MAX_ROUNDS}) ${reasoning}`, "loading");

      // e) 사용자 직접 진행 필요(로그인/캡차/결제)
      if (data.handoff_required) {
        finalText = "🟡 사용자가 직접 진행해야 합니다.\n\n" + reasoning;
        finalStatus = "handoff";
        break;
      }

      // f) 완료
      if (data.done) {
        finalText = reasoning;
        finalStatus = "success";
        break;
      }

      // f-2) 제출/결제성 동작은 실행 전에 사용자 확인을 받는다(확인 게이트).
      if (data.confirm_required) {
        const approved = window.confirm(
          "[ArenaX 확인]\n" +
            (data.confirm_message || "중요한 동작을 실행하려고 합니다.") +
            "\n\n실행할까요?\n동작: " +
            data.action +
            "\n이유: " +
            reasoning
        );
        if (!approved) {
          finalText = "🟡 사용자가 실행을 취소했습니다.\n\n" + reasoning;
          finalStatus = "handoff";
          break;
        }
      }

      // g) 액션 실행
      const execResp = await sendToBackground({
        type: "EXECUTE",
        tabId,
        action: { action: data.action, target_id: data.target_id, value: data.value },
      });
      actionHistory.push({
        step: round,
        action: data.action,
        target: data.target_id,
        value: data.value,
        error: execResp && execResp.success ? null : (execResp && execResp.error) || "실행 실패",
      });

      // 페이지가 액션에 반응/렌더링할 시간을 준다.
      await new Promise((r) => setTimeout(r, 700));
    }

    updateCard(id, finalText, finalStatus);
    if (finalStatus === "success") setStatus("✅ 완료", "success");
    else if (finalStatus === "handoff") setStatus("🟡 직접 진행 필요", "handoff");
    else setStatus("⏹️ 종료", "error");
  } catch (err) {
    updateCard(id, err.message, "error");
    setStatus("❌ 실패", "error");
  } finally {
    // 항상 디버거를 해제해 배너를 정리한다.
    if (tabId != null) {
      try {
        await sendToBackground({ type: "DETACH_DEBUGGER", tabId });
      } catch (e) {
        /* 무시: 탭이 닫혔거나 이미 해제됨 */
      }
    }
    agentInFlight = false;
    taskInput.disabled = false;
    sendBtn.disabled = false;
    taskInput.focus();
  }
}

sendBtn.addEventListener("click", sendTask);

taskInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendTask();
  }
});

taskInput.addEventListener("input", () => {
  taskInput.style.height = "auto";
  taskInput.style.height = Math.min(taskInput.scrollHeight, 120) + "px";
});
