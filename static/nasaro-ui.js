/**
 * Nasaro UI upgrades — home widgets, studio banner, theme (no duplicate mode tabs)
 */
(function (global) {
  "use strict";

  function initTheme() {
    const sel = document.getElementById("settingsThemeSelect");
    const saved = localStorage.getItem("nasaro_theme") || "light";
    document.documentElement.setAttribute("data-theme", saved === "dark" ? "dark" : "light");
    if (sel && !sel.dataset.nxTheme) {
      sel.dataset.nxTheme = "1";
      sel.value = saved;
      sel.addEventListener("change", () => {
        const v = sel.value === "dark" ? "dark" : "light";
        document.documentElement.setAttribute("data-theme", v);
        localStorage.setItem("nasaro_theme", v);
      });
    }
  }

  function removeAiModeSegment() {
    document.getElementById("aiModeSegment")?.remove();
  }

  function patchSwitchMode() {
    const orig = global.switchMode;
    if (!orig || orig._nxPatched) return;
    global.switchMode = function (mode) {
      if (mode === "agent") mode = "compare";
      if (mode === "collab") {
        global.setAppWorkspace?.("ai");
        orig("collab");
        global.CollabFlowV2?.show?.();
        return;
      }
      orig(mode);
      document.getElementById("askPanel")?.classList.toggle("active", mode === "ask");
    };
    global.switchMode._nxPatched = true;
    global.switchMode._orig = orig;
  }

  function mountHomeWidgets() {
    const layout = document.getElementById("homeLayout");
    if (!layout || document.getElementById("homeWidgetGrid")) return;
    const tabs = layout.querySelector(".home-tab-row");
    const grid = document.createElement("div");
    grid.id = "homeWidgetGrid";
    grid.className = "home-widget-grid";
    grid.innerHTML = `
      <div class="nx-card home-widget" data-widget="economy-summary">
        <span class="home-widget-badge">곧 제공</span>
        <h4 style="margin:0 0 6px;font-size:14px;">오늘의 경제 요약</h4>
        <p style="font-size:12px;color:var(--text-muted);margin:0;">AI 경제 브리핑</p>
      </div>
      <div class="nx-card home-widget" data-widget="news">
        <span class="home-widget-badge">곧 제공</span>
        <h4 style="margin:0 0 6px;font-size:14px;">최신 뉴스</h4>
        <p style="font-size:12px;color:var(--text-muted);margin:0;">관심 뉴스 피드</p>
      </div>
      <div class="nx-card home-widget" data-widget="work-highlight">
        <h4 style="margin:0 0 6px;font-size:14px;">내 작업물</h4>
        <div id="homeWorkHighlight" class="nx-empty" style="padding:8px;">—</div>
      </div>
      <div class="nx-card home-widget" data-widget="quota">
        <h4 style="margin:0 0 6px;font-size:14px;">코인 · 쿼터</h4>
        <div id="homeQuotaWidget" style="font-size:12px;">—</div>
        <div class="quota-gauge"><div class="quota-gauge-fill" id="homeQuotaFill" style="width:40%"></div></div>
      </div>`;
    if (tabs) layout.insertBefore(grid, tabs);
  }

  async function refreshHomeWidgets() {
    const hl = document.getElementById("homeWorkHighlight");
    if (hl && global.WorkFiles) {
      try {
        const list = await global.WorkFiles.loadFiles({ limit: 4 });
        hl.innerHTML = list.length
          ? list.map((f) => `<div style="font-size:11px;padding:4px 0;">${f.title}</div>`).join("")
          : "저장된 작업물 없음";
      } catch (_) { hl.textContent = "—"; }
    }
  }

  function mountStudioCollabBanner() {
    const root = document.getElementById("studioAppRoot");
    if (!root?.parentElement || document.getElementById("studioCollabBanner")) return;
    const banner = document.createElement("button");
    banner.type = "button";
    banner.id = "studioCollabBanner";
    banner.className = "studio-collab-banner";
    banner.innerHTML = "<h3>🤝 협업 시작</h3><p>여러 AI가 역할을 나눠 함께 결과물을 완성합니다</p>";
    banner.addEventListener("click", () => global.switchMode?.("collab"));
    root.parentElement.insertBefore(banner, root);
  }

  function wireExtras() {
    document.getElementById("dockAttachFileBtn")?.addEventListener("click", () => {
      global.WorkFiles?.openPicker?.({
        onPick: (f) => {
          const inp = document.getElementById("mainInput");
          if (inp && f.text_content) inp.value = ((inp.value || "") + "\n\n[참고]\n" + f.text_content.slice(0, 1500)).trim();
          global.showToast?.("참고 자료를 첨부했습니다.", "success");
        },
      });
    });
    document.getElementById("compareResponses")?.classList.add("cols-scroll");
  }

  global.NasaroUI = {
    init() {
      initTheme();
      removeAiModeSegment();
      patchSwitchMode();
      mountHomeWidgets();
      mountStudioCollabBanner();
      wireExtras();
      refreshHomeWidgets();
    },
    refreshHomeWidgets,
  };
})(typeof window !== "undefined" ? window : global);
