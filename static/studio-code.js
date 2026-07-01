/**
 * Code studio — Monaco + virtual FS + Pyodide preview (A-2)
 */
(function (global) {
  "use strict";

  const DB_NAME = "nasaro_code_projects";
  const STORE = "files";
  let monacoReady = null;
  let editor = null;
  let containerEl = null;
  let files = { "index.html": "<!DOCTYPE html>\n<html><body><h1>Hello</h1></body></html>" };
  let activeFile = "index.html";
  let pyodide = null;

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
    treeEl.innerHTML = "";
    Object.keys(files).sort().forEach((name) => {
      const row = document.createElement("div");
      row.style.cssText = "padding:4px 6px;cursor:pointer;border-radius:6px;" + (name === activeFile ? "background:rgba(37,99,235,.15);font-weight:700;" : "");
      row.textContent = name;
      row.addEventListener("click", () => {
        activeFile = name;
        if (editor) {
          editor.setValue(files[name] || "");
          global.monaco.editor.setModelLanguage(editor.getModel(), langFor(name));
        }
        renderFileTree(treeEl);
      });
      treeEl.appendChild(row);
    });
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

  const CodeStudio = {
    async mount(container, payload) {
      containerEl = container;
      container.innerHTML = `
        <div class="studio-code-layout">
          <div class="studio-code-files" id="codeFileTree"></div>
          <div style="display:flex;flex-direction:column;gap:6px;">
            <div style="display:flex;gap:6px;">
              <button type="button" id="codeRunBtn" style="padding:6px 12px;border-radius:8px;border:none;background:#2563eb;color:#fff;font-weight:700;cursor:pointer;">▶ 실행</button>
              <button type="button" id="codeSaveProjBtn" style="padding:6px 12px;border-radius:8px;border:1px solid #e5e7eb;background:#fff;cursor:pointer;">저장</button>
            </div>
            <div class="studio-code-editor" id="codeEditorMount"></div>
            <iframe id="codePreview" sandbox="allow-scripts allow-modals" style="width:100%;height:120px;border:1px solid #e5e7eb;border-radius:8px;"></iframe>
          </div>
          <div class="studio-code-console" id="codeConsole"></div>
        </div>`;

      const parsed = payload?.text ? parseCodeBlocks(payload.text) : {};
      if (Object.keys(parsed).length) Object.assign(files, parsed);

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
      });

      renderFileTree(container.querySelector("#codeFileTree"));
      container.querySelector("#codeRunBtn")?.addEventListener("click", () =>
        runCode(container.querySelector("#codeConsole"), container.querySelector("#codePreview"))
      );
      container.querySelector("#codeSaveProjBtn")?.addEventListener("click", () => this.saveProject());
    },

    destroy() {
      if (editor) {
        try { editor.dispose(); } catch (_) {}
        editor = null;
      }
      containerEl = null;
    },

    getText() {
      if (editor) files[activeFile] = editor.getValue();
      return Object.entries(files).map(([k, v]) => "```" + k + "\n" + v + "\n```").join("\n\n");
    },

    async saveProject() {
      const name = prompt("프로젝트 이름", "my-project");
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
        if (global.showToast) global.showToast("프로젝트 저장됨", "success");
      } catch (e) {
        if (global.showToast) global.showToast(String(e.message || e), "error");
      }
    },
  };

  global.CodeStudio = CodeStudio;
})(typeof window !== "undefined" ? window : global);
