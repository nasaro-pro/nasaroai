/**
 * Studio Hub App — orchestrates hub, shell, and all tools
 */
(function (global) {
  "use strict";

  let root = null;
  let deps = {};
  let shell = null;
  let activeTool = null;
  let activeDestroy = null;
  let projects = [];
  let pricingCatalog = [];
  let mediaState = { modality: "image", label: "DeepSeek", lastResult: null, history: [] };

  async function loadProjects() {
    try {
      const uid = await deps.ensureDeviceId();
      const res = await deps.apiFetch(`/studio/projects?user_id=${encodeURIComponent(uid)}`);
      const data = await res.json().catch(() => ({}));
      projects = data.projects || [];
    } catch (_) { projects = []; }
  }

  async function loadPricing() {
    try {
      const res = await deps.apiFetch("/pricing/catalog");
      if (!res.ok) return;
      const data = await res.json();
      pricingCatalog = data.models || [];
    } catch (_) {}
  }

  function modelsFor(mod) {
    return pricingCatalog.filter((m) => m.modality === mod && m.is_active !== false);
  }

  function coinCost(label, mod) {
    const hit = pricingCatalog.find((m) => m.label === label && m.modality === mod);
    return hit ? hit.coin_cost : 1;
  }

  function showHub() {
    activeTool = null;
    if (activeDestroy) { try { activeDestroy(); } catch (_) {} activeDestroy = null; }
    if (shell) { shell.destroy(); shell = null; }
    loadProjects().then(() => {
      global.StudioHub?.render(root, {
        projects,
        onSelect: (toolId) => openTool(toolId),
      });
    });
  }

  function openTool(toolId, project) {
    activeTool = toolId;
    if (activeDestroy) { try { activeDestroy(); } catch (_) {} activeDestroy = null; }
    root.innerHTML = "";
    shell = global.StudioShell.create(root, {
      title: project?.name || defaultTitle(toolId),
      onBack: showHub,
      onTitleChange: () => {},
    });

    const opts = { deps, project, shell };

    if (toolId === "code") {
      mountCodeTool(shell, project);
    } else if (toolId === "doc") {
      global.DocumentEditor?.mount(shell, opts).then(() => {
        activeDestroy = () => global.DocumentEditor?.destroy?.();
      });
    } else if (toolId === "slide") {
      global.SlideEditor?.mount(shell, opts).then(() => {
        activeDestroy = () => global.SlideEditor?.destroy?.();
      });
    } else if (toolId === "video") {
      mountVideoTool(shell, project);
    } else if (toolId === "image" || toolId === "audio") {
      mountMediaTool(shell, toolId);
    } else {
      showHub();
    }
  }

  function defaultTitle(id) {
    const m = { image: "이미지", video: "영상", audio: "오디오", code: "코드", doc: "문서", slide: "슬라이드" };
    return "새 " + (m[id] || "프로젝트");
  }

  async function mountCodeTool(shell, project) {
    shell.left.innerHTML = `<div class="studio-panel-label">코드 프로젝트</div><p class="studio-panel-hint">Monaco · Pyodide · ZIP</p>`;
    shell.right.innerHTML = `<div class="studio-panel-label">프리뷰</div><p class="studio-panel-hint">HTML/JS는 iframe에서 실행</p>`;
    const mount = document.createElement("div");
    mount.style.cssText = "height:100%;min-height:400px;";
    shell.center.appendChild(mount);
    shell.showBottom(true);
    const consoleWrap = document.createElement("div");
    consoleWrap.className = "studio-code-console";
    consoleWrap.id = "shellCodeConsole";
    shell.bottom.appendChild(consoleWrap);

    if (global.CodeStudio) {
      await global.CodeStudio.mount(mount, project?.files ? { text: Object.entries(project.files).map(([k, v]) => "```" + k + "\n" + v + "\n```").join("\n\n") } : {});
      activeDestroy = () => global.CodeStudio.destroy?.();
    } else {
      shell.center.innerHTML = "<p>CodeStudio 모듈 로드 실패</p>";
    }

    shell.setExportHandler(async () => {
      if (global.CodeStudio?.saveProject) await global.CodeStudio.saveProject();
    });
    shell.setSaveStatus("saved");
  }

  let videoEditorInst = null;
  let videoMode = "edit";

  function mountVideoTool(shell, project) {
    videoMode = "edit";
    renderVideoPanels(shell, project);
  }

  function renderVideoPanels(shell, project) {
    if (videoMode === "generate") {
      mountMediaTool(shell, "video", () => { videoMode = "edit"; renderVideoPanels(shell, project); });
      return;
    }
    global.VideoEditor?.mount(shell, {
      deps,
      project,
      onSwitchGenerate: () => { videoMode = "generate"; renderVideoPanels(shell, project); },
    });
    activeDestroy = () => { global.VideoEditor?.destroy?.(); videoEditorInst = null; };
  }

  function mountMediaTool(shell, modality, onBackEdit) {
    mediaState.modality = modality;
    const list = modelsFor(modality);
    if (list.length && !list.some((m) => m.label === mediaState.label)) {
      mediaState.label = list[0].label;
    }

    shell.left.innerHTML = `
      <div class="studio-panel-label">모델</div>
      <div id="mediaModelList" class="studio-hub-model-list"></div>
      <div class="studio-panel-label" style="margin-top:12px;">기록</div>
      <div id="mediaHistoryList" class="studio-history-list"></div>`;

    shell.center.innerHTML = `
      <label class="studio-label">프롬프트</label>
      <textarea id="mediaPrompt" class="studio-media-prompt" placeholder="원하는 결과를 설명하세요…"></textarea>
      <div id="mediaOptRow" class="studio-options-row visible" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;"></div>
      <div id="mediaUploadZone" class="studio-upload-zone" style="display:none;margin-top:8px;">드래그 또는 클릭하여 이미지 업로드</div>
      <input type="file" id="mediaUploadInput" accept="image/*" hidden>
      <input id="mediaRefUrl" type="url" class="studio-ref-url" placeholder="참조 URL (선택)" style="width:100%;margin-top:8px;display:none;">
      <button type="button" id="mediaRunBtn" class="studio-media-run">생성하기 <span id="mediaRunCost">🪙1</span></button>`;

    shell.right.innerHTML = `
      <div class="studio-panel-label">결과</div>
      <div id="mediaResultMount" class="studio-result-mount">모델을 선택하고 생성하세요.</div>
      <div class="studio-result-actions" style="margin-top:8px;">
        <button type="button" id="mediaDlBtn" disabled>다운로드</button>
        <button type="button" id="mediaSaveBtn" disabled>저장</button>
      </div>`;

    if (onBackEdit) {
      const back = document.createElement("button");
      back.type = "button";
      back.textContent = "← 편집기";
      back.style.cssText = "width:100%;margin-bottom:8px;padding:8px;border-radius:8px;border:1px solid #e5e7eb;cursor:pointer;";
      back.addEventListener("click", onBackEdit);
      shell.left.prepend(back);
    }

    renderMediaModels(shell);
    renderMediaHistory(shell);

    const optRow = shell.center.querySelector("#mediaOptRow");
    if (modality === "image" || modality === "video") {
      optRow.innerHTML = `
        <label>비율</label>
        <select id="mediaAspect"><option>1:1</option><option selected>16:9</option><option>9:16</option></select>
        <input id="mediaNegative" placeholder="제외 (선택)" style="flex:1;min-width:120px;">`;
      if (modality === "video") {
        optRow.innerHTML += `<label>길이</label><select id="mediaDuration"><option value="5">5초</option><option value="10">10</option></select>`;
      }
      shell.center.querySelector("#mediaUploadZone").style.display = modality === "image" ? "block" : "none";
      shell.center.querySelector("#mediaRefUrl").style.display = "block";
    } else {
      optRow.innerHTML = "";
    }

    const resultMount = shell.right.querySelector("#mediaResultMount");
    if (global.StudioApp) {
      global.StudioApp.init(resultMount);
      global.StudioApp.setModality(modality);
    }

    global.StudioApp?.bindUploadZone?.(
      shell.center.querySelector("#mediaUploadZone"),
      shell.center.querySelector("#mediaUploadInput"),
      (url) => {
        const ref = shell.center.querySelector("#mediaRefUrl");
        if (ref) ref.value = url.startsWith("http") ? url : (deps.getApiBase?.() || "") + url;
      }
    );

    shell.center.querySelector("#mediaRunBtn")?.addEventListener("click", () => runMediaGenerate(shell));
    shell.right.querySelector("#mediaDlBtn")?.addEventListener("click", () => downloadMediaResult());
    shell.right.querySelector("#mediaSaveBtn")?.addEventListener("click", () => saveMediaAsWork(shell));

    activeDestroy = () => {
      global.StudioApp?.destroyRenderer?.();
    };
  }

  function renderMediaModels(shell) {
    const box = shell.left?.querySelector("#mediaModelList");
    if (!box) return;
    const list = modelsFor(mediaState.modality);
    box.innerHTML = list.map((m) =>
      `<button type="button" class="studio-model-card${m.label === mediaState.label ? " active" : ""}" data-label="${m.label}">
        <div>${m.label}</div><div class="coin">🪙${m.coin_cost}</div>
      </button>`
    ).join("") || "<span class='studio-panel-hint'>로딩…</span>";
    box.querySelectorAll(".studio-model-card").forEach((btn) => {
      btn.addEventListener("click", () => {
        mediaState.label = btn.dataset.label;
        renderMediaModels(shell);
        const cost = shell.center?.querySelector("#mediaRunCost");
        if (cost) cost.textContent = "🪙" + coinCost(mediaState.label, mediaState.modality);
      });
    });
    const cost = shell.center?.querySelector("#mediaRunCost");
    if (cost) cost.textContent = "🪙" + coinCost(mediaState.label, mediaState.modality);
  }

  function renderMediaHistory(shell) {
    const box = shell.left?.querySelector("#mediaHistoryList");
    if (!box) return;
    const items = mediaState.history.filter((h) => h.modality === mediaState.modality).slice(0, 8);
    if (global.StudioApp?.renderHistoryGrid) {
      global.StudioApp.renderHistoryGrid(box, items.map((h) => ({ mod: h.modality, prompt: h.prompt, label: h.label, url: h.url, thumb: h.thumb })));
    } else {
      box.innerHTML = items.map((h) => `<div style="font-size:11px;padding:4px 0;">${h.label}</div>`).join("") || "없음";
    }
  }

  async function runMediaGenerate(shell) {
    const promptEl = shell.center?.querySelector("#mediaPrompt");
    let prompt = (promptEl?.value || "").trim();
    if (!prompt) { deps.showToast?.("프롬프트를 입력하세요.", "warn"); return; }
    const neg = shell.center?.querySelector("#mediaNegative")?.value?.trim();
    if (neg) prompt += "\n\n제외: " + neg;

    const btn = shell.center?.querySelector("#mediaRunBtn");
    const mount = shell.right?.querySelector("#mediaResultMount");
    if (btn) btn.disabled = true;
    global.StudioApp?.showSkeleton?.();

    try {
      const uid = await deps.ensureDeviceId();
      let promptForApi = prompt;
      if (mediaState.modality === "video") {
        const dur = shell.center?.querySelector("#mediaDuration")?.value || "5";
        promptForApi += `\n\n[영상] ${dur}초, ${shell.center?.querySelector("#mediaAspect")?.value || "16:9"}`;
      }
      const res = await deps.apiFetch("/studio/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          label: mediaState.label,
          prompt: promptForApi,
          user_id: uid,
          image_url: (shell.center?.querySelector("#mediaRefUrl")?.value || "").trim(),
          aspect_ratio: (shell.center?.querySelector("#mediaAspect")?.value || "1:1").trim(),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "생성 실패");

      mediaState.lastResult = data;
      displayMediaResult(data, mount, shell);
      mediaState.history.unshift({
        label: mediaState.label,
        modality: data.modality || mediaState.modality,
        prompt: promptEl?.value || "",
        url: data.media_url,
        thumb: data.media_url,
        result: data,
      });
      if (mediaState.history.length > 20) mediaState.history.length = 20;
      renderMediaHistory(shell);
      shell.right?.querySelector("#mediaDlBtn")?.removeAttribute("disabled");
      shell.right?.querySelector("#mediaSaveBtn")?.removeAttribute("disabled");
    } catch (e) {
      if (mount) mount.textContent = e.message || "실패";
      deps.showToast?.(e.message || "생성 실패", "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function displayMediaResult(data, mount, shell) {
    if (!mount) return;
    const mod = data.modality || mediaState.modality;
    if (mod === "video" && data.job_id && !data.media_url) {
      global.StudioApp?.mount?.({
        polling: true,
        jobId: data.job_id,
        apiFetch: deps.apiFetch,
        onUpdate: (u) => {
          if (u.done && u.job?.result_url) {
            mediaState.lastResult = { ...mediaState.lastResult, media_url: u.job.result_url };
            if (videoMode === "generate" && global.VideoEditor) {
              global.VideoEditor.addGeneratedClip(u.job.result_url, "AI 영상");
            }
          }
        },
      });
      return;
    }
    const payload = { text: data.text, modality: mod };
    if (data.media_url) payload.url = data.media_url;
    global.StudioApp?.mount?.(payload);
  }

  function downloadMediaResult() {
    const exp = global.StudioApp?.getExportData?.() || {};
    const url = exp.url || mediaState.lastResult?.media_url;
    if (url) {
      const a = document.createElement("a");
      a.href = url;
      a.download = "studio-" + mediaState.modality;
      a.target = "_blank";
      a.click();
    }
  }

  async function saveMediaAsWork(shell) {
    if (!mediaState.lastResult || !deps.currentUser?.()) {
      deps.showToast?.("로그인 후 저장", "warn");
      return;
    }
    const exp = global.StudioApp?.getExportData?.() || {};
    try {
      await deps.apiFetch("/social/works", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: (mediaState.label + " · " + mediaState.modality).slice(0, 80),
          body: exp.text || mediaState.lastResult.text || "",
          media_type: exp.type !== "text" ? exp.type : mediaState.modality,
          media_url: exp.url || mediaState.lastResult.media_url || "",
        }),
      });
      deps.showToast?.("작업물로 저장됨", "success");
    } catch (e) {
      deps.showToast?.("저장 실패", "error");
    }
  }

  const StudioHubApp = {
    init(container, options) {
      root = container;
      deps = options || {};
      loadPricing().then(showHub);
    },

    showHub,
    openTool,

    openFromWork(work) {
      if (!work) return;
      const typeMap = { image: "image", video: "video", audio: "audio", code: "code", text: "doc" };
      const toolId = typeMap[work.media_type] || "image";
      const prompt = `다음 작업물을 발전:\n${work.title || ""}\n${work.body || ""}`;
      openTool(toolId);
      setTimeout(() => {
        const promptEl = shell?.center?.querySelector("#mediaPrompt") || shell?.center?.querySelector("textarea");
        if (promptEl) promptEl.value = prompt;
        const ref = shell?.center?.querySelector("#mediaRefUrl");
        if (ref && work.media_url) ref.value = work.media_url;
      }, 400);
    },

    get mediaState() { return mediaState; },
  };

  global.StudioHubApp = StudioHubApp;

  global.addEventListener("studio:history-select", (e) => {
    const h = e.detail;
    if (!h || !shell) return;
    const prompt = shell.center?.querySelector("#mediaPrompt");
    if (prompt) prompt.value = h.prompt || "";
    if (h.label) mediaState.label = h.label;
    renderMediaModels(shell);
    if (h.result || h.url) displayMediaResult(h.result || { media_url: h.url, modality: h.mod }, shell.right?.querySelector("#mediaResultMount"), shell);
  });
})(typeof window !== "undefined" ? window : global);
