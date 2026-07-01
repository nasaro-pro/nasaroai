/**
 * SNS extras — stories viewer, feed interactions (C-2, C-1)
 */
(function (global) {
  "use strict";

  const SocialFeatures = {
    storyGroups: [],

    async loadStories(apiFetch) {
      try {
        const r = await apiFetch("/social/stories");
        if (!r.ok) return [];
        const data = await r.json();
        this.storyGroups = data.stories || [];
        return this.storyGroups;
      } catch {
        return [];
      }
    },

    renderStoryBar(barEl, groups, onOpen) {
      if (!barEl) return;
      if (!groups?.length) {
        barEl.innerHTML = currentUserCanStory()
          ? `<button type="button" class="insta-story-add" id="storyAddBtn">＋ 내 스토리</button>`
          : "";
        barEl.querySelector("#storyAddBtn")?.addEventListener("click", () => SocialFeatures.promptCreateStory(apiFetchGlobal));
        return;
      }
      barEl.innerHTML = groups.map((g, i) => {
        const initial = (g.author_name || "?").charAt(0).toUpperCase();
        const hasNew = (g.items || []).length > 0;
        return `<div class="insta-story${hasNew ? " has-story" : ""}" data-story-idx="${i}">
          <div class="insta-story-ring"><span>${esc(initial)}</span></div>
          <span class="insta-story-label">${esc((g.author_name || "").slice(0, 6))}</span></div>`;
      }).join("") + `<button type="button" class="insta-story-add" id="storyAddBtn">＋</button>`;
      barEl.querySelectorAll(".insta-story[data-story-idx]").forEach(el => {
        el.addEventListener("click", () => {
          const idx = Number(el.dataset.storyIdx);
          if (onOpen) onOpen(groups[idx], idx);
          else SocialFeatures.openViewer(groups, idx);
        });
      });
      barEl.querySelector("#storyAddBtn")?.addEventListener("click", () => SocialFeatures.promptCreateStory(apiFetchGlobal));
    },

    openViewer(groups, startIdx) {
      let gIdx = startIdx || 0;
      let iIdx = 0;
      const ov = document.createElement("div");
      ov.className = "story-viewer-overlay";
      ov.style.cssText = "position:fixed;inset:0;z-index:9500;background:#000;display:flex;flex-direction:column;";
      const render = () => {
        const g = groups[gIdx];
        const items = g?.items || [];
        const item = items[iIdx];
        if (!item) { ov.remove(); return; }
        const progress = items.map((_, i) =>
          `<div style="flex:1;height:3px;background:${i <= iIdx ? "#fff" : "rgba(255,255,255,.3)"};border-radius:2px;margin:0 2px;"></div>`
        ).join("");
        let media = "";
        if (item.media_url && item.media_type === "image") {
          media = `<img src="${esc(item.media_url)}" style="max-width:100%;max-height:70vh;object-fit:contain;">`;
        } else if (item.body) {
          media = `<p style="color:#fff;font-size:18px;padding:24px;text-align:center;line-height:1.6;">${esc(item.body)}</p>`;
        }
        ov.innerHTML = `
          <div style="padding:12px 16px;display:flex;gap:4px;">${progress}</div>
          <div style="flex:1;display:flex;align-items:center;justify-content:center;padding:16px;">${media}</div>
          <div style="padding:16px;color:#fff;font-weight:700;">${esc(g.author_name || "")}</div>
          <button type="button" id="storyClose" style="position:absolute;top:12px;right:16px;background:none;border:none;color:#fff;font-size:24px;cursor:pointer;">✕</button>`;
        ov.querySelector("#storyClose")?.addEventListener("click", () => ov.remove());
        ov.onclick = (e) => {
          const rect = ov.getBoundingClientRect();
          const x = e.clientX - rect.left;
          if (x > rect.width / 2) {
            if (iIdx < items.length - 1) iIdx++;
            else if (gIdx < groups.length - 1) { gIdx++; iIdx = 0; }
            else { ov.remove(); return; }
          } else {
            if (iIdx > 0) iIdx--;
            else if (gIdx > 0) { gIdx--; iIdx = (groups[gIdx].items?.length || 1) - 1; }
          }
          render();
        };
      };
      render();
      document.body.appendChild(ov);
    },

    async promptCreateStory(apiFetch) {
      if (!window.currentUser) {
        if (global.showToast) global.showToast("로그인 후 스토리를 올릴 수 있습니다.", "warn");
        return;
      }
      const text = prompt("스토리 텍스트 (24시간 노출)", "");
      if (text == null) return;
      try {
        const r = await apiFetch("/social/stories", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ body: text, media_type: "text" }),
        });
        if (!r.ok) throw new Error("실패");
        if (global.showToast) global.showToast("스토리가 등록되었습니다.", "success");
        if (global.refreshWorksStories) global.refreshWorksStories();
      } catch (e) {
        if (global.showToast) global.showToast("스토리 등록 실패", "error");
      }
    },

    bindDoubleTapLike(feedEl, onLike) {
      if (!feedEl) return;
      feedEl.addEventListener("dblclick", (e) => {
        const card = e.target.closest(".works-card");
        if (!card) return;
        const id = card.dataset.workId;
        const heart = document.createElement("div");
        heart.textContent = "❤️";
        heart.style.cssText = "position:absolute;font-size:64px;left:50%;top:40%;transform:translate(-50%,-50%) scale(0);animation:heartPop .6s ease-out forwards;pointer-events:none;z-index:10;";
        card.style.position = "relative";
        card.appendChild(heart);
        setTimeout(() => heart.remove(), 700);
        if (onLike && id) onLike(id);
      });
    },
  };

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

    function currentUserCanStory() {
        return !!window.currentUser;
    }

  let apiFetchGlobal = global.apiFetch || fetch.bind(global);

  global.SocialFeatures = SocialFeatures;
})(typeof window !== "undefined" ? window : global);
