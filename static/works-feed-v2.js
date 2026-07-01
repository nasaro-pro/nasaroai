/**
 * Works feed v2 — Instagram-style cards, type filter, AI feedback button
 */
(function (global) {
  "use strict";

  let deps = {};

  function patchLoadFeed() {
    const orig = global.loadWorksFeed;
    if (!orig || orig._v2) return;
    global.loadWorksFeed = async function (append) {
      await orig(append);
      applyTypeFilter();
    };
    global.loadWorksFeed._v2 = true;
  }

  function applyTypeFilter() {
    const t = global._worksTypeFilter || "";
    if (!t) return;
    document.querySelectorAll("#worksFeed .works-card").forEach((card) => {
      const wId = card.dataset.workId;
      const hidden = card.dataset.mediaType && card.dataset.mediaType !== t;
      card.style.display = hidden ? "none" : "";
    });
  }

  function patchBuildCard() {
    const orig = global.buildWorkCard;
    if (!orig || orig._v2) return;
    global.buildWorkCard = function (w) {
      const card = orig(w);
      if (!card) return card;
      card.dataset.mediaType = (w.media_type || "text").toLowerCase();
      card.classList.add("works-card-v2");
      const actions = card.querySelector(".insta-actions");
      if (actions && !actions.querySelector(".works-ai-comment-btn")) {
        const aiBtn = document.createElement("button");
        aiBtn.type = "button";
        aiBtn.className = "works-ai-comment-btn nx-btn nx-btn-sm nx-btn-ghost";
        aiBtn.textContent = "✨ AI 피드백";
        aiBtn.addEventListener("click", () => requestAiComment(w.id, aiBtn));
        actions.appendChild(aiBtn);
      }
      return card;
    };
    global.buildWorkCard._v2 = true;
  }

  async function requestAiComment(workId, btn) {
    if (!global.currentUser) { deps.showToast?.("로그인이 필요합니다.", "warn"); return; }
    btn.disabled = true;
    btn.textContent = "생성 중…";
    try {
      const uid = deps.ensureDeviceId ? await deps.ensureDeviceId() : "";
      const res = await deps.apiFetch("/social/work/" + workId + "/ai-comment", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: uid }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "실패");
      deps.showToast?.("AI 피드백이 댓글로 등록되었습니다.", "success");
      global.toggleWorkComments?.(String(workId));
      global.toggleWorkComments?.(String(workId));
    } catch (e) {
      deps.showToast?.(e.message || "AI 피드백 실패", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "✨ AI 피드백";
    }
  }

  const TYPE_CHIPS = [
    { id: "", label: "전체" },
    { id: "image", label: "이미지" },
    { id: "video", label: "영상" },
    { id: "audio", label: "음성" },
    { id: "text", label: "텍스트" },
  ];

  function mountTypeFilter() {
    const tabs = document.querySelector(".works-feed-tabs");
    if (!tabs || document.getElementById("worksTypeFilter")) return;
    const row = document.createElement("div");
    row.id = "worksTypeFilter";
    row.className = "works-type-filter";
    row.innerHTML = TYPE_CHIPS.map((t) =>
      `<button type="button" class="nx-chip works-type-chip${t.id ? "" : " active"}" data-type="${t.id}">${t.label}</button>`
    ).join("");
    tabs.insertAdjacentElement("afterend", row);
    row.querySelectorAll(".works-type-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        global._worksTypeFilter = btn.dataset.type || "";
        row.querySelectorAll(".works-type-chip").forEach((b) => b.classList.toggle("active", b === btn));
        applyTypeFilter();
      });
    });
  }

  function init(options) {
    deps = options || {};
    mountTypeFilter();
    patchBuildCard();
    patchLoadFeed();
  }

  global.WorksFeedV2 = { init, applyTypeFilter };
})(typeof window !== "undefined" ? window : global);
