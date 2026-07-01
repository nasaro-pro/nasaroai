/**
 * Studio 공동작업 폴더 — 여러 형식 작업물을 project_id로 묶음
 */
(function (global) {
  "use strict";

  let deps = {};
  let bundleProjectId = null;
  let activePanel = null;

  const TYPE_ICONS = { image: "🎨", video: "🎬", audio: "🎵", code: "💻", ppt: "📊", doc: "📄", collab: "🤝" };

  function escapeHtml(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  async function ensureUid() {
    return deps.ensureDeviceId ? deps.ensureDeviceId() : "";
  }

  async function ensureBundleProject(title) {
    if (bundleProjectId) return bundleProjectId;
    const uid = await ensureUid();
    const res = await deps.apiFetch("/studio/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: uid,
        name: (title || "공동작업 폴더").slice(0, 80),
        project_type: "doc",
        files: { "bundle.json": JSON.stringify({ kind: "work-bundle", created: Date.now() }) },
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "폴더 생성 실패");
    bundleProjectId = data.project?.id || null;
    return bundleProjectId;
  }

  async function assignFileToBundle(fileId) {
    const pid = await ensureBundleProject();
    if (!pid) return;
    const uid = await ensureUid();
    const res = await deps.apiFetch("/files/" + fileId, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: uid, project_id: pid }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "폴더에 추가 실패");
    }
  }

  async function refreshList(container) {
    if (!container) return;
    container.innerHTML = '<div class="nx-skeleton"></div>';
    try {
      const pid = bundleProjectId || (await ensureBundleProject());
      const list = await global.WorkFiles?.loadFiles?.({ project_id: pid, limit: 40 }) || [];
      if (!list.length) {
        container.innerHTML = '<div class="nx-empty" style="padding:8px;font-size:11px;">아직 폴더가 비어 있습니다.<br>현재 작업 또는 라이브러리에서 추가하세요.</div>';
        return;
      }
      container.innerHTML = list.map((f) => {
        const ic = TYPE_ICONS[f.type] || "📁";
        return `<div class="bundle-file-item" data-id="${f.id}" title="${escapeHtml(f.title)}">
          <span class="bundle-file-icon">${ic}</span>
          <span class="bundle-file-title">${escapeHtml(f.title || "작업물")}</span>
          <span class="bundle-file-type">${escapeHtml(f.type || "")}</span>
        </div>`;
      }).join("");
      container.querySelectorAll(".bundle-file-item").forEach((el) => {
        el.addEventListener("click", () => {
          const f = list.find((x) => String(x.id) === el.dataset.id);
          if (f) global.dispatchEvent(new CustomEvent("workfile:selected", { detail: f }));
        });
      });
    } catch (e) {
      container.innerHTML = `<div class="nx-empty">${escapeHtml(e.message)}</div>`;
    }
  }

  function mount(shell, opts) {
    opts = opts || {};
    deps = opts.deps || deps;
    if (opts.projectId) bundleProjectId = opts.projectId;

    let box = shell.right?.querySelector(".studio-bundle-panel");
    if (!box && shell.right) {
      box = document.createElement("div");
      box.className = "studio-bundle-panel";
      shell.right.appendChild(box);
    }
    if (!box) return;
    activePanel = box;

    box.innerHTML = `
      <div class="studio-panel-label">공동작업 폴더</div>
      <p class="studio-panel-hint">이미지·영상·문서 등 여러 형식을 한 폴더에 모읍니다.</p>
      <div id="bundleFileList" class="bundle-file-list"></div>
      <div class="bundle-file-actions">
        <button type="button" class="nx-btn nx-btn-sm nx-btn-secondary" id="bundleAddCurrentBtn">＋ 현재 작업</button>
        <button type="button" class="nx-btn nx-btn-sm nx-btn-ghost" id="bundlePickBtn">📁 추가</button>
      </div>`;

    const listEl = box.querySelector("#bundleFileList");
    refreshList(listEl);

    box.querySelector("#bundleAddCurrentBtn")?.addEventListener("click", async () => {
      try {
        if (opts.onSaveCurrent) {
          const saved = await opts.onSaveCurrent();
          if (saved?.id) await assignFileToBundle(saved.id);
          else if (saved) await refreshList(listEl);
        } else {
          deps.showToast?.("저장할 내용이 없습니다.", "warn");
        }
        await refreshList(listEl);
        deps.showToast?.("공동작업 폴더에 추가했습니다.", "success");
      } catch (e) {
        deps.showToast?.(e.message || "추가 실패", "error");
      }
    });

    box.querySelector("#bundlePickBtn")?.addEventListener("click", () => {
      global.WorkFiles?.openPicker?.({
        onPick: async (f) => {
          try {
            await assignFileToBundle(f.id);
            await refreshList(listEl);
            deps.showToast?.("폴더에 추가했습니다.", "success");
          } catch (e) {
            deps.showToast?.(e.message || "추가 실패", "error");
          }
        },
      });
    });
  }

  function setProjectId(id) {
    bundleProjectId = id || null;
    const listEl = activePanel?.querySelector("#bundleFileList");
    if (listEl) refreshList(listEl);
  }

  function getProjectId() {
    return bundleProjectId;
  }

  global.WorkBundlePanel = {
    init(options) { deps = options || {}; },
    mount,
    refreshList,
    ensureBundleProject,
    setProjectId,
    getProjectId,
    assignFileToBundle,
  };
})(typeof window !== "undefined" ? window : global);
