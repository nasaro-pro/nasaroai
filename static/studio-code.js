/**
 * Code studio — Monaco + IndexedDB + diff + zip (A-2)
 */
(function (global) {
  "use strict";

  const DB_NAME = "nasaro_code_projects";
  const STORE = "projects";
  let monacoReady = null;
  let editor = null;
  let diffEditor = null;
  let containerEl = null;
  let files = { "index.html": "<!DOCTYPE html>\n<html><body><h1>Hello</h1></body></html>" };
  let pendingDiff = null;
  let activeFile = "index.html";
  let pyodide = null;
  let projectId = "default";

  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: "id" });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function idbSave(id, data) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put({ id, ...data, updated_at: Date.now() });
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  async function idbLoad(id) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).get(id);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  }

  async function persistLocal() {
    try {
      await idbSave(projectId, { files, activeFile });
    } catch (_) {}
  }

  function loadMonaco() {
    if (monacoReady) return monacoReady;
    monacoReady = new Promise((resolve, reject) => {
      if (global.monaco?.editor) return resolve(global.monaco);
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs/loader.js";
      s.onload = () => {
        global.require.config({ paths: { vs: "https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs" } });
        global.require(["vs/editor/editor.main"], () => resolve(global.monaco));
      };
      s.onerror = reject;
      document.head.appendChild(s);
    });
    return monacoReady;
  }

  async function loadJSZip() {
    if (global.JSZip) return global.JSZip;
    await new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js";
      s.onload = res;
      s.onerror = rej;
      document.head.appendChild(s);
    });
    return global.JSZip;
  }

  async function loadPyodide() {
    if (pyodide) return pyodide;
    if (!global.loadPyodide) {
      await new Promise((res, rej) => {
        const s = document.createElement("script");
        s.src = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js";
        s.onload = res;
        s.onerror = rej;
        document.head.appendChild(s);
      });
    }
    pyodide = await global.loadPyodide({ indexURL: "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/" });
    return pyodide;
  }

  function langFor(name) {
    if (name.endsWith(".py")) return "python";
    if (name.endsWith(".js")) return "javascript";
    if (name.endsWith(".css")) return "css";
    if (name.endsWith(".html")) return "html";
    if (name.endsWith(".json")) return "json";
    return "plaintext";
  }

  function renderFileTree(treeEl) {
    if (!treeEl) return;
    treeEl.innerHTML = "";
    Object.keys(files).sort().forEach((name) => {
      const row = document.createElement("div");
      row.style.cssText = "padding:4px 6px;cursor:pointer;border-radius:6px;" + (name === activeFile ? "background:rgba(37,99,235,.15);font-weight:700;" : "");
      row.textContent = name;
      row.addEventListener("click", () => switchFile(name, treeEl));
      treeEl.appendChild(row);
    });
  }

  function switchFile(name, treeEl) {
    activeFile = name;
    if (editor) {
      editor.setValue(files[name] || "");
      global.monaco.editor.setModelLanguage(editor.getModel(), langFor(name));
    }
    renderFileTree(treeEl || containerEl?.querySelector("#codeFileTree"));
  }

  function parseCodeBlocks(text) {
    const out = {};
    const re = /```(?:[\w+-]+)?\s*([^\n`]*)\n([\s\S]*?)```/g;
    let m;
    while ((m = re.exec(text))) {
      let fname = (m[1] || "").trim();
      const body = m[2];
      if (!fname) fname = Object.keys(out).length ? "file" + Object.keys(out).length + ".txt" : "index.html";
      out[fname] = body;
    }
    return out;
  }

  function showDiff(container, fname, oldText, newText, onApply) {
    const panel = container.querySelector("#codeDiffPanel");
    if (!panel) return;
    panel.style.display = "block";
    panel.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
      <strong style="font-size:12px;">변경 미리보기: ${fname}</strong>
      <div style="display:flex;gap:6px;">
        <button type="button" id="codeApplyDiff" style="padding:4px 10px;border-radius:6px;border:none;background:#059669;color:#fff;cursor:pointer;font-weight:700;">적용</button>
        <button type="button" id="codeCancelDiff" style="padding:4px 10px;border-radius:6px;border:1px solid #e5e7eb;background:#fff;cursor:pointer;">취소</button>
      </div></div><div id="codeDiffMount" style="height:160px;border:1px solid #e5e7eb;border-radius:8px;"></div>`;
    const mount = panel.querySelector("#codeDiffMount");
    if (diffEditor) { try { diffEditor.dispose(); } catch (_) {} diffEditor = null; }
    diffEditor = global.monaco.editor.createDiffEditor(mount, {
      theme: document.documentElement.dataset.theme === "dark" ? "vs-dark" : "vs",
      readOnly: true,
      automaticLayout: true,
    });
    diffEditor.setModel({
      original: global.monaco.editor.createModel(oldText || "", langFor(fname)),
      modified: global.monaco.editor.createModel(newText || "", langFor(fname)),
    });
    panel.querySelector("#codeApplyDiff")?.addEventListener("click", () => {
      files[fname] = newText;
      if (activeFile === fname && editor) editor.setValue(newText);
      panel.style.display = "none";
      persistLocal();
      if (onApply) onApply();
    });
    panel.querySelector("#codeCancelDiff")?.addEventListener("click", () => {
      panel.style.display = "none";
    });
  }

  function mergeAiFiles(parsed) {
    const treeEl = containerEl?.querySelector("#codeFileTree");
    Object.entries(parsed).forEach(([fname, body]) => {
      if (files[fname] != null && files[fname] !== body) {
        showDiff(containerEl, fname, files[fname], body, () => renderFileTree(treeEl));
      } else {
        files[fname] = body;
      }
    });
    if (editor && files[activeFile] != null) editor.setValue(files[activeFile]);
    renderFileTree(treeEl);
    persistLocal();
  }

  async function runCode(consoleEl, previewFrame) {
    if (!consoleEl) return;
    consoleEl.textContent = "";
    const t0 = performance.now();
    if (activeFile.endsWith(".py")) {
      consoleEl.textContent = "Pyodide 로딩…\n";
      try {
        const p = await loadPyodide();
        p.setStdout({ batched: (s) => { consoleEl.textContent += s; } });
        p.setStderr({ batched: (s) => { consoleEl.textContent += s; } });
        await p.runPythonAsync(files[activeFile] || "");
        consoleEl.textContent += `\n— 완료 (${Math.round(performance.now() - t0)}ms)`;
      } catch (e) {
        consoleEl.textContent += "\n오류: " + (e.message || e);
      }
      return;
    }
    if (previewFrame && (files["index.html"] || activeFile.endsWith(".html"))) {
      const html = files["index.html"] || files[activeFile] || "";
      const blob = new Blob([html], { type: "text/html" });
      previewFrame.src = URL.createObjectURL(blob);
      consoleEl.textContent = "HTML 프리뷰 갱신됨 (" + Math.round(performance.now() - t0) + "ms)";
    } else {
      consoleEl.textContent = "실행: Python(.py) 또는 HTML 프로젝트를 지원합니다.";
    }
  }

  async function downloadZip() {
    const JSZip = await loadJSZip();
    const zip = new JSZip();
    Object.entries(files).forEach(([k, v]) => zip.file(k, v));
    const blob = await zip.generateAsync({ type: "blob" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (projectId || "project") + ".zip";
    a.click();
  }

  async function loadServerProjects(selectEl) {
    if (!selectEl) return;
    try {
      const uid = await (global.ensureDeviceId ? global.ensureDeviceId() : "");
      const r = await fetch("/studio/projects?user_id=" + encodeURIComponent(uid));
      const data = await r.json();
      const list = data.projects || [];
      selectEl.innerHTML = '<option value="">— 프로젝트 불러오기 —</option>' +
        list.map(p => `<option value="${p.id}">${p.name}</option>`).join("");
    } catch (_) {}
  }

  const CodeStudio = {
    async mount(container, payload) {
      containerEl = container;
      const saved = await idbLoad(projectId);
      if (saved?.files) {
        files = { ...saved.files };
        activeFile = saved.activeFile || activeFile;
      }

      container.innerHTML = `
        <div class="studio-code-layout">
          <div>
            <div class="studio-code-files" id="codeFileTree"></div>
            <select id="codeProjSelect" style="width:100%;margin-top:6px;font-size:11px;padding:4px;border-radius:6px;border:1px solid #e5e7eb;"></select>
          </div>
          <div style="display:flex;flex-direction:column;gap:6px;">
            <div style="display:flex;gap:6px;flex-wrap:wrap;">
              <button type="button" id="codeRunBtn" style="padding:6px 12px;border-radius:8px;border:none;background:#2563eb;color:#fff;font-weight:700;cursor:pointer;">▶ 실행</button>
              <button type="button" id="codeSaveProjBtn" style="padding:6px 12px;border-radius:8px;border:1px solid #e5e7eb;background:#fff;cursor:pointer;">저장</button>
              <button type="button" id="codeZipBtn" style="padding:6px 12px;border-radius:8px;border:1px solid #e5e7eb;background:#fff;cursor:pointer;">ZIP</button>
              <button type="button" id="codeNewFileBtn" style="padding:6px 12px;border-radius:8px;border:1px solid #e5e7eb;background:#fff;cursor:pointer;">+ 파일</button>
            </div>
            <div id="codeDiffPanel" style="display:none;"></div>
            <div class="studio-code-editor" id="codeEditorMount"></div>
            <iframe id="codePreview" sandbox="allow-scripts allow-modals" style="width:100%;height:120px;border:1px solid #e5e7eb;border-radius:8px;"></iframe>
          </div>
          <div class="studio-code-console" id="codeConsole"></div>
        </div>`;

      const parsed = payload?.text ? parseCodeBlocks(payload.text) : {};
      if (Object.keys(parsed).length) mergeAiFiles(parsed);

      await loadMonaco();
      const mount = container.querySelector("#codeEditorMount");
      editor = global.monaco.editor.create(mount, {
        value: files[activeFile] || "",
        language: langFor(activeFile),
        theme: document.documentElement.dataset.theme === "dark" ? "vs-dark" : "vs",
        fontSize: 13,
        minimap: { enabled: false },
        automaticLayout: true,
      });
      editor.onDidChangeModelContent(() => {
        files[activeFile] = editor.getValue();
        persistLocal();
      });

      renderFileTree(container.querySelector("#codeFileTree"));
      const projSel = container.querySelector("#codeProjSelect");
      await loadServerProjects(projSel);
      projSel?.addEventListener("change", async () => {
        const pid = projSel.value;
        if (!pid) return;
        try {
          const uid = await (global.ensureDeviceId ? global.ensureDeviceId() : "");
          const r = await fetch(`/studio/projects/${pid}?user_id=${encodeURIComponent(uid)}`);
          const data = await r.json();
          if (data.project?.files) {
            files = { ...data.project.files };
            projectId = "server-" + pid;
            activeFile = Object.keys(files)[0] || "index.html";
            if (editor) {
              editor.setValue(files[activeFile] || "");
              global.monaco.editor.setModelLanguage(editor.getModel(), langFor(activeFile));
            }
            renderFileTree(container.querySelector("#codeFileTree"));
            persistLocal();
            if (global.showToast) global.showToast("프로젝트 불러옴", "success");
          }
        } catch (e) {
          if (global.showToast) global.showToast("불러오기 실패", "error");
        }
      });

      container.querySelector("#codeRunBtn")?.addEventListener("click", () =>
        runCode(container.querySelector("#codeConsole"), container.querySelector("#codePreview"))
      );
      container.querySelector("#codeSaveProjBtn")?.addEventListener("click", () => this.saveProject());
      container.querySelector("#codeZipBtn")?.addEventListener("click", () => downloadZip());
      container.querySelector("#codeNewFileBtn")?.addEventListener("click", () => {
        const name = prompt("파일 이름", "script.js");
        if (!name) return;
        files[name.trim()] = "";
        activeFile = name.trim();
        if (editor) {
          editor.setValue("");
          global.monaco.editor.setModelLanguage(editor.getModel(), langFor(activeFile));
        }
        renderFileTree(container.querySelector("#codeFileTree"));
        persistLocal();
      });
    },

    destroy() {
      if (editor) { try { editor.dispose(); } catch (_) {} editor = null; }
      if (diffEditor) { try { diffEditor.dispose(); } catch (_) {} diffEditor = null; }
      containerEl = null;
    },

    getText() {
      if (editor) files[activeFile] = editor.getValue();
      return Object.entries(files).map(([k, v]) => "```" + k + "\n" + v + "\n```").join("\n\n");
    },

    async saveProject() {
      const name = prompt("프로젝트 이름", projectId.startsWith("server-") ? projectId : "my-project");
      if (!name) return;
      try {
        const uid = await (global.ensureDeviceId ? global.ensureDeviceId() : "");
        const r = await fetch("/studio/projects", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: uid, name, files }),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || "저장 실패");
        await persistLocal();
        if (global.showToast) global.showToast("프로젝트 저장됨", "success");
        await loadServerProjects(containerEl?.querySelector("#codeProjSelect"));
      } catch (e) {
        if (global.showToast) global.showToast(String(e.message || e), "error");
      }
    },
  };

  global.CodeStudio = CodeStudio;
})(typeof window !== "undefined" ? window : global);
