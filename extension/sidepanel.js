let serverUrl = "http://localhost:8000";
let agentInFlight = false;
let agentHistory = [];

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

const STATUS_LABEL = { success: "완료", error: "실패", loading: "진행 중" };

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

  try {
    let response;
    try {
      response = await fetch(`${serverUrl}/agent/task`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: task }),
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

    const result = data.result || "결과 없음";
    updateCard(id, result, "success");
    setStatus("✅ 완료", "success");
  } catch (err) {
    updateCard(id, err.message, "error");
    setStatus("❌ 실패", "error");
  } finally {
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
