/**
 * AI Ask mode — single-model chat via /agent/ask
 */
(function (global) {
  "use strict";

  let deps = {};
  let history = [];

  function escapeHtml(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function mount() {
    const panel = document.getElementById("askPanel");
    if (!panel || panel.dataset.mounted) return;
    panel.dataset.mounted = "1";
    panel.innerHTML = `
      <div id="askMessages" class="ask-messages"></div>`;
  }

  function appendMsg(role, text, withActions) {
    const box = document.getElementById("askMessages");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "ask-msg " + role;
    if (role === "assistant" && global.marked && global.DOMPurify) {
      el.innerHTML = global.DOMPurify.sanitize(global.marked.parse(text));
    } else {
      el.textContent = text;
    }
    if (role === "assistant" && withActions !== false) {
      const act = document.createElement("div");
      act.className = "ask-msg-actions";
      act.innerHTML = `
        <button type="button" class="nx-btn nx-btn-sm nx-btn-secondary" data-act="save">💾 저장</button>
        <button type="button" class="nx-btn nx-btn-sm nx-btn-secondary" data-act="image">🎨 이미지</button>
        <button type="button" class="nx-btn nx-btn-sm nx-btn-secondary" data-act="video">🎬 영상</button>`;
      act.querySelector('[data-act="save"]')?.addEventListener("click", () => {
        global.WorkFiles?.saveFile?.({ type: "doc", title: text.slice(0, 60), text_content: text, source_tool: "ai-ask" });
      });
      act.querySelector('[data-act="image"]')?.addEventListener("click", () => openStudio("image", text));
      act.querySelector('[data-act="video"]')?.addEventListener("click", () => openStudio("video", text));
      el.appendChild(act);
    }
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
  }

  function openStudio(tool, prompt) {
    global.setAppWorkspace?.("studio");
    global.StudioHubApp?.openTool?.(tool);
    setTimeout(() => {
      const p = document.querySelector("#mediaPrompt, textarea");
      if (p) p.value = prompt.slice(0, 2000);
    }, 500);
  }

  async function send(query) {
    query = (query || "").trim();
    if (!query) return;
    appendMsg("user", query, false);
    appendMsg("assistant", "생각 중…", false);
    const box = document.getElementById("askMessages");
    const loading = box?.lastElementChild;
    try {
      const uid = deps.ensureDeviceId ? await deps.ensureDeviceId() : "";
      const res = await deps.apiFetch("/agent/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, user_id: uid, history: history.slice(-3) }),
      });
      const data = await res.json().catch(() => ({}));
      if (loading) loading.remove();
      const answer = data.result || data.message || data.content || "";
      if (!res.ok || data.status === "error") throw new Error(answer || data.message || "요청 실패");
      appendMsg("assistant", answer);
      history.push({ mission: query, result: answer.slice(0, 500) });
      deps.refreshSideQuota?.();
    } catch (e) {
      if (loading) loading.textContent = e.message || "오류";
      deps.showToast?.(e.message || "AI 질문 실패", "error");
    }
  }

  global.AiAskMode = {
    init(options) { deps = options || {}; mount(); },
    send,
    mount,
  };
})(typeof window !== "undefined" ? window : global);
