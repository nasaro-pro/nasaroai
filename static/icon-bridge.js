/**
 * Lucide icon bridge — replace key emoji controls with lucide + label
 */
(function (global) {
  "use strict";

  const MAP = [
    { sel: "#dockAttachFileBtn", icon: "paperclip", label: "첨부" },
    { sel: "#dockMicBtn", icon: "mic", label: "" },
    { sel: "#chatAttachBtn", icon: "paperclip", label: "" },
    { sel: "#chatAiBtn", icon: "bot", label: "AI" },
    { sel: "#chatSendBtn", icon: "send", label: "" },
    { sel: "#chatRefreshBtn", icon: "refresh-cw", label: "" },
    { sel: "#chatMobileBackBtn", icon: "arrow-left", label: "" },
    { sel: "#settingsBtn .side-menu-settings-btn span:first-child, #settingsBtn span:first-child", icon: "settings", label: "" },
  ];

  function apply() {
    if (!global.lucide?.createIcons) return;
    MAP.forEach(({ sel, icon, label }) => {
      document.querySelectorAll(sel).forEach((el) => {
        if (el.dataset.lucideDone) return;
        el.dataset.lucideDone = "1";
        el.innerHTML = `<i data-lucide="${icon}" class="rail-icon"></i>${label ? `<span class="icon-label">${label}</span>` : ""}`;
      });
    });
    document.querySelectorAll(".app-rail-btn[data-workspace]").forEach((btn) => {
      const ws = btn.dataset.workspace;
      const icons = { ai: "bot", studio: "palette", chat: "message-circle", works: "layout-grid", home: "home" };
      const ic = icons[ws];
      if (!ic || btn.querySelector("[data-lucide]")) return;
      const label = btn.querySelector(".rail-label");
      btn.querySelector("span")?.insertAdjacentHTML("afterbegin", `<i data-lucide="${ic}" class="rail-icon"></i>`);
      if (label && !label.textContent.trim()) label.textContent = ws;
    });
    global.lucide.createIcons();
  }

  global.IconBridge = { apply, init: apply };
})(typeof window !== "undefined" ? window : global);
