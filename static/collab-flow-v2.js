/**
 * Collab flow v2 — intake progress, pipeline diagram, fullscreen modal
 */
(function (global) {
  "use strict";

  const MAX_INTAKE = 5;

  function openModal() {
    global.setAppWorkspace?.("ai");
    global.switchMode?.("collab");
    document.body.classList.add("collab-modal-open");
    const intro = document.getElementById("collabIntro");
    if (intro) intro.style.display = "flex";
  }

  function closeModal() {
    document.body.classList.remove("collab-modal-open");
  }

  function mountIntakeProgress() {
    const panel = document.getElementById("collabIntakePanel");
    if (!panel || panel.querySelector(".collab-intake-progress")) return;
    const bar = document.createElement("div");
    bar.className = "collab-intake-progress";
    bar.innerHTML = `<div class="collab-intake-progress-track"><div class="collab-intake-progress-fill"></div></div><span class="collab-intake-progress-label">정보 수집 0%</span>`;
    panel.insertBefore(bar, panel.querySelector("#collabIntakeChat"));
  }

  function updateIntakeProgress(count) {
    const fill = document.querySelector(".collab-intake-progress-fill");
    const label = document.querySelector(".collab-intake-progress-label");
    const pct = Math.min(100, Math.round((count / MAX_INTAKE) * 100));
    if (fill) fill.style.width = pct + "%";
    if (label) label.textContent = `정보 수집 ${pct}% · ${count}개 답변`;
  }

  function enhancePipelineRail() {
    const rail = document.querySelector(".collab-stage-rail");
    if (!rail || rail.dataset.v2) return;
    rail.dataset.v2 = "1";
    rail.classList.add("collab-pipeline-v2");
  }

  function patchCollabHooks() {
    const origAppend = global.appendCollabIntakeMessage;
    if (origAppend && !origAppend._v2) {
      global.appendCollabIntakeMessage = function (role, text, speaker) {
        mountIntakeProgress();
        const el = origAppend(role, text, speaker);
        if (role === "user") {
          const chat = document.getElementById("collabIntakeChat");
          updateIntakeProgress(chat ? chat.querySelectorAll(".collab-intake-msg.user").length : 0);
        }
        return el;
      };
      global.appendCollabIntakeMessage._v2 = true;
    }
    const origRender = global.renderCollabRunner;
    if (origRender && !origRender._v2) {
      global.renderCollabRunner = function (c) { origRender(c); requestAnimationFrame(enhancePipelineRail); };
      global.renderCollabRunner._v2 = true;
    }
  }

  function init() {
    mountIntakeProgress();
    global.addEventListener("studio:open-collab", openModal);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && document.body.classList.contains("collab-modal-open")) closeModal();
    });
    if (!document.querySelector(".collab-modal-close")) {
      const closeBtn = document.createElement("button");
      closeBtn.type = "button";
      closeBtn.className = "collab-modal-close nx-btn nx-btn-secondary";
      closeBtn.textContent = "협업 닫기";
      closeBtn.addEventListener("click", closeModal);
      document.getElementById("collabPanel")?.prepend(closeBtn);
    }
    setTimeout(patchCollabHooks, 600);
  }

  global.CollabFlowV2 = { init, openModal, closeModal };
})(typeof window !== "undefined" ? window : global);
