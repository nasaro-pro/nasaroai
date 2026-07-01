/**
 * Chat SNS upgrades — typing, work-file share, mobile slide, AI styling
 */
(function (global) {
  "use strict";

  let deps = {};
  let typingTimer = null;
  let typingPollTimer = null;

  function enhanceComposeBar() {
    const compose = document.querySelector(".discord-compose");
    if (!compose || document.getElementById("chatWorkShareBtn")) return;
    const shareBtn = document.createElement("button");
    shareBtn.type = "button";
    shareBtn.id = "chatWorkShareBtn";
    shareBtn.className = "chat-ai-btn";
    shareBtn.title = "내 작업물 공유";
    shareBtn.innerHTML = '<i data-lucide="folder-open" class="rail-icon"></i>';
    compose.insertBefore(shareBtn, compose.firstChild);
    shareBtn.addEventListener("click", () => {
      global.WorkFiles?.openPicker?.({
        onPick: async (f) => {
          if (!global.chatRoomId) { deps.showToast?.("대화방을 선택하세요.", "warn"); return; }
          const body = `📎 ${f.title || "작업물"}`;
          const attach = f.content_url_or_path || "";
          try {
            const res = await deps.apiFetch("/social/chat/room/" + encodeURIComponent(global.chatRoomId), {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ body, attachment_url: attach }),
            });
            if (!res.ok) throw new Error("전송 실패");
            global.loadChatMessages?.();
          } catch (e) { deps.showToast?.(e.message || "공유 실패", "error"); }
        },
      });
    });
    global.lucide?.createIcons?.({ nodes: [shareBtn] });
  }

  function patchAppendBubble() {
    const orig = global.appendChatBubble;
    if (!orig || orig._sns) return;
    global.appendChatBubble = function (box, m) {
      const row = orig(box, m);
      if (!row) return row;
      row.classList.add("chat-bubble-v2");
      if ((m.sender_type || "") === "ai") row.classList.add("chat-bubble-ai");
      const bubble = row.querySelector(".chat-bubble");
      if (bubble && m.attachment_url && !m.body) {
        const url = m.attachment_url;
        if (/\.(png|jpe?g|gif|webp)/i.test(url)) {
          bubble.innerHTML = `<div class="chat-work-card"><img src="${url}" alt=""><span>${escapeHtml(m.body || "작업물")}</span></div>`;
        } else if (/\.(mp4|webm)/i.test(url)) {
          bubble.innerHTML = `<video src="${url}" controls playsinline style="max-width:100%;border-radius:12px;"></video>`;
        }
      }
      return row;
    };
    global.appendChatBubble._sns = true;
  }

  function escapeHtml(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function wireTyping() {
    const input = document.getElementById("chatInput");
    if (!input || input.dataset.typingWired) return;
    input.dataset.typingWired = "1";
    input.addEventListener("input", () => {
      if (!global.chatRoomId || !global.currentUser) return;
      clearTimeout(typingTimer);
      typingTimer = setTimeout(() => {
        deps.apiFetch?.("/social/chat/typing", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ room_id: global.chatRoomId }),
        }).catch(() => {});
      }, 300);
    });
  }

  function startTypingPoll() {
    clearInterval(typingPollTimer);
    typingPollTimer = setInterval(async () => {
      if (!global.chatRoomId) return;
      try {
        const res = await deps.apiFetch("/social/chat/typing/" + encodeURIComponent(global.chatRoomId));
        const data = await res.json().catch(() => ({}));
        const el = document.getElementById("chatTypingIndicator");
        if (!el) return;
        if (data.typing && data.user) {
          el.textContent = data.user + " 입력 중…";
          el.style.display = "block";
          el.setAttribute("aria-hidden", "false");
        } else {
          el.style.display = "none";
          el.setAttribute("aria-hidden", "true");
        }
      } catch (_) {}
    }, 2500);
  }

  function patchSelectRoom() {
    const orig = global.selectChatRoom;
    if (!orig || orig._sns) return;
    global.selectChatRoom = function (roomId, name, roomType) {
      orig(roomId, name, roomType);
      const layout = document.getElementById("chatLayout");
      if (layout) layout.classList.add("chat-slide-in");
      setTimeout(() => layout?.classList.remove("chat-slide-in"), 350);
    };
    global.selectChatRoom._sns = true;
  }

  function init(options) {
    deps = options || {};
    enhanceComposeBar();
    patchAppendBubble();
    wireTyping();
    startTypingPoll();
    patchSelectRoom();
  }

  global.ChatSNS = { init };
})(typeof window !== "undefined" ? window : global);
