/**
 * Nasaro — unified work-files library + WorkPickerModal
 */
(function (global) {
  "use strict";

  const TYPE_ICONS = { image: "🎨", video: "🎬", audio: "🎵", code: "💻", ppt: "📊", doc: "📄", collab: "🤝" };
  let deps = {};
  let filterType = "";
  let searchQ = "";
  let onPickCallback = null;

  async function ensureUid() {
    return deps.ensureDeviceId ? deps.ensureDeviceId() : "";
  }

  async function loadFiles(opts) {
    opts = opts || {};
    const uid = await ensureUid();
    const params = new URLSearchParams({ user_id: uid, limit: String(opts.limit || 60) });
    if (opts.type || filterType) params.set("type", opts.type || filterType);
    if (opts.q || searchQ) params.set("q", opts.q || searchQ);
    if (opts.project_id != null) params.set("project_id", String(opts.project_id));
    const res = await deps.apiFetch("/files?" + params);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "불러오기 실패");
    return data.files || [];
  }

  async function saveFile(payload) {
    const uid = await ensureUid();
    const res = await deps.apiFetch("/files/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: uid, is_pinned: true, ...payload }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "저장 실패");
    deps.showToast?.("내 작업물에 저장됨", "success");
    updateBadge();
    return data.file;
  }

  async function deleteFile(id) {
    const uid = await ensureUid();
    const res = await deps.apiFetch(`/files/${id}?user_id=${encodeURIComponent(uid)}`, { method: "DELETE" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "삭제 실패");
    }
    updateBadge();
  }

  async function updateBadge() {
    try {
      const uid = await ensureUid();
      const res = await deps.apiFetch("/files?user_id=" + encodeURIComponent(uid) + "&limit=1");
      const data = await res.json().catch(() => ({}));
      const n = data.total || 0;
      const el = document.getElementById("workFilesBadge");
      if (el) {
        el.style.display = n > 0 ? "inline-flex" : "none";
        el.textContent = n > 99 ? "99+" : String(n);
      }
    } catch (_) {}
  }

  function escapeHtml(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function renderGrid(container, list, actions) {
    if (!container) return;
    if (!list.length) {
      container.innerHTML = '<div class="nx-empty">저장된 작업물이 없습니다.</div>';
      return;
    }
    container.innerHTML = list.map((f) => {
      const thumb = f.thumbnail_url || f.content_url_or_path || "";
      const isImg = thumb && /\.(png|jpe?g|gif|webp|svg)/i.test(thumb);
      const inner = isImg ? `<img src="${thumb}" alt="">` : (TYPE_ICONS[f.type] || "📁");
      return `<div class="work-file-card${f.is_pinned ? " pinned" : ""}" data-id="${f.id}">
        <div class="wf-thumb">${inner}</div>
        <div class="wf-title">${escapeHtml(f.title || "작업물")}</div>
      </div>`;
    }).join("");
    container.querySelectorAll(".work-file-card").forEach((el) => {
      el.addEventListener("click", () => {
        const f = list.find((x) => String(x.id) === el.dataset.id);
        if (f && actions?.onSelect) actions.onSelect(f);
      });
    });
  }

  function mountSidePanel(root) {
    if (!root) return;
    root.innerHTML = `
      <div class="work-files-panel">
        <div class="works-section-title">내 작업물</div>
        <div class="work-files-toolbar" id="workFilesChips"></div>
        <input type="search" class="work-files-search" id="workFilesSearch" placeholder="검색…">
        <div class="work-files-grid" id="workFilesGrid"><div class="nx-skeleton"></div></div>
      </div>`;
    const chips = root.querySelector("#workFilesChips");
    ["", "image", "video", "audio", "code", "ppt", "doc", "collab"].forEach((t, i) => {
      const lbl = ["전체", "이미지", "영상", "오디오", "코드", "PPT", "문서", "협업"][i];
      const c = document.createElement("button");
      c.type = "button";
      c.className = "nx-chip" + (i === 0 ? " active" : "");
      c.textContent = lbl;
      c.addEventListener("click", () => {
        filterType = t;
        chips.querySelectorAll(".nx-chip").forEach((x, j) => x.classList.toggle("active", j === i));
        refreshSidePanel(root);
      });
      chips.appendChild(c);
    });
    root.querySelector("#workFilesSearch")?.addEventListener("input", (e) => {
      searchQ = e.target.value.trim();
      clearTimeout(root._wfTimer);
      root._wfTimer = setTimeout(() => refreshSidePanel(root), 250);
    });
    refreshSidePanel(root);
  }

  async function refreshSidePanel(root) {
    const grid = root?.querySelector("#workFilesGrid");
    if (!grid) return;
    grid.innerHTML = '<div class="nx-skeleton"></div>';
    try {
      const list = await loadFiles();
      renderGrid(grid, list, {
        onSelect: (f) => global.dispatchEvent(new CustomEvent("workfile:selected", { detail: f })),
      });
    } catch (e) {
      grid.innerHTML = `<div class="nx-empty">${escapeHtml(e.message)}</div>`;
    }
  }

  function ensurePickerModal() {
    let ov = document.getElementById("workPickerOverlay");
    if (ov) return ov;
    ov = document.createElement("div");
    ov.id = "workPickerOverlay";
    ov.className = "work-picker-overlay";
    ov.innerHTML = `
      <div class="work-picker-box" role="dialog">
        <div class="work-picker-head"><span>작업물 불러오기</span><button type="button" class="nx-btn-ghost nx-btn-sm" id="workPickerClose">✕</button></div>
        <div class="work-picker-body"><div id="workPickerGrid" class="work-files-grid"></div></div>
      </div>`;
    document.body.appendChild(ov);
    ov.querySelector("#workPickerClose")?.addEventListener("click", () => closePicker());
    ov.addEventListener("click", (e) => { if (e.target === ov) closePicker(); });
    return ov;
  }

  function openPicker(opts) {
    opts = opts || {};
    onPickCallback = opts.onPick || null;
    const ov = ensurePickerModal();
    ov.classList.add("open");
    const grid = ov.querySelector("#workPickerGrid");
    grid.innerHTML = '<div class="nx-skeleton"></div>';
    loadFiles({ type: opts.type || "" }).then((list) => {
      renderGrid(grid, list, {
        onSelect: (f) => {
          if (onPickCallback) onPickCallback(f);
          else global.dispatchEvent(new CustomEvent("workfile:selected", { detail: f }));
          closePicker();
        },
      });
    }).catch((e) => { grid.innerHTML = `<div class="nx-empty">${escapeHtml(e.message)}</div>`; });
  }

  function closePicker() {
    document.getElementById("workPickerOverlay")?.classList.remove("open");
    onPickCallback = null;
  }

  global.WorkFiles = {
    init(options) {
      deps = options || {};
      updateBadge();
      mountSidePanel(document.getElementById("sideMenuWorkFiles"));
    },
    refreshStudioPanel() {
      refreshSidePanel(document.getElementById("sideMenuWorkFiles"));
    },
    loadFiles,
    saveFile,
    deleteFile,
    openPicker,
    closePicker,
    refreshSidePanel,
    updateBadge,
    TYPE_ICONS,
  };
})(typeof window !== "undefined" ? window : global);
