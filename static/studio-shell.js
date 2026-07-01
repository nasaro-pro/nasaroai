/**
 * Studio Editor Shell — shared layout for all studio tools
 */
(function (global) {
  "use strict";

  function el(tag, cls, html) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  const StudioShell = {
    create(root, opts) {
      opts = opts || {};
      root.innerHTML = "";
      const wrap = el("div", "studio-shell");
      wrap.innerHTML = `
        <header class="studio-shell-toolbar">
          <button type="button" class="studio-shell-back" title="허브로">← 허브</button>
          <input type="text" class="studio-shell-title" value="" maxlength="120" placeholder="프로젝트 이름">
          <span class="studio-shell-save-status" data-state="idle">—</span>
          <div class="studio-shell-toolbar-actions">
            <button type="button" class="studio-shell-export">내보내기 ▾</button>
          </div>
        </header>
        <div class="studio-shell-body">
          <aside class="studio-shell-left"></aside>
          <main class="studio-shell-center"></main>
          <aside class="studio-shell-right"></aside>
        </div>
        <footer class="studio-shell-bottom" style="display:none;"></footer>`;
      root.appendChild(wrap);

      const titleInput = wrap.querySelector(".studio-shell-title");
      const saveStatus = wrap.querySelector(".studio-shell-save-status");
      const exportBtn = wrap.querySelector(".studio-shell-export");
      const backBtn = wrap.querySelector(".studio-shell-back");
      const bottom = wrap.querySelector(".studio-shell-bottom");

      if (opts.title) titleInput.value = opts.title;

      const api = {
        el: wrap,
        left: wrap.querySelector(".studio-shell-left"),
        center: wrap.querySelector(".studio-shell-center"),
        right: wrap.querySelector(".studio-shell-right"),
        bottom,
        getTitle() { return (titleInput.value || "").trim() || "새 프로젝트"; },
        setTitle(v) { titleInput.value = v || ""; },
        setSaveStatus(state, text) {
          saveStatus.dataset.state = state || "idle";
          const labels = { idle: "—", saving: "저장 중…", saved: "저장됨 ✓", error: "저장 실패", dirty: "변경됨" };
          saveStatus.textContent = text || labels[state] || state;
        },
        showBottom(show) { bottom.style.display = show ? "" : "none"; },
        setExportHandler(fn) {
          exportBtn.onclick = (e) => { e.stopPropagation(); fn?.(exportBtn); };
        },
        destroy() { root.innerHTML = ""; },
      };

      backBtn.addEventListener("click", () => opts.onBack?.());
      titleInput.addEventListener("input", () => opts.onTitleChange?.(api.getTitle()));
      return api;
    },
  };

  global.StudioShell = StudioShell;
})(typeof window !== "undefined" ? window : global);
