/**
 * Document editor — Quill + AI assistant + docx export
 */
(function (global) {
  "use strict";

  let quill = null;
  let saveTimer = null;
  let versions = [];
  let deps = {};
  let projectId = null;
  let shellApi = null;

  function loadScript(src) {
    return new Promise((res, rej) => {
      if (document.querySelector(`script[src="${src}"]`)) return res();
      const s = document.createElement("script");
      s.src = src;
      s.onload = res;
      s.onerror = rej;
      document.head.appendChild(s);
    });
  }

  function loadCss(href) {
    if (document.querySelector(`link[href="${href}"]`)) return;
    const l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = href;
    document.head.appendChild(l);
  }

  async function loadQuill() {
    loadCss("https://cdn.jsdelivr.net/npm/quill@2.0.2/dist/quill.snow.css");
    await loadScript("https://cdn.jsdelivr.net/npm/quill@2.0.2/dist/quill.js");
    return global.Quill;
  }

  async function loadDocx() {
    if (global.docx) return global.docx;
    await loadScript("https://cdn.jsdelivr.net/npm/docx@8.5.0/build/index.umd.js");
    return global.docx;
  }

  function getContent() {
    if (!quill) return { html: "", delta: null };
    return { html: quill.root.innerHTML, delta: quill.getContents() };
  }

  function setContent(html) {
    if (!quill) return;
    quill.root.innerHTML = html || "<p><br></p>";
  }

  function pushVersion() {
    const { html, delta } = getContent();
    versions.unshift({ html, delta: delta ? delta.ops : [], at: Date.now() });
    if (versions.length > 5) versions.length = 5;
  }

  async function saveProject(shell) {
    shell.setSaveStatus("saving");
    try {
      const uid = await deps.ensureDeviceId();
      const { html, delta } = getContent();
      const files = {
        "content.html": html,
        "content.json": JSON.stringify(delta ? delta.ops : []),
        "versions.json": JSON.stringify(versions),
      };
      const body = {
        user_id: uid,
        name: shell.getTitle(),
        project_type: "doc",
        files,
        thumbnail: "",
      };
      let res;
      if (projectId) {
        res = await deps.apiFetch(`/studio/projects/${projectId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } else {
        res = await deps.apiFetch("/studio/projects", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "저장 실패");
      if (data.project?.id) projectId = data.project.id;
      shell.setSaveStatus("saved");
    } catch (e) {
      shell.setSaveStatus("error", e.message || "저장 실패");
      deps.showToast?.(e.message || "저장 실패", "error");
    }
  }

  function scheduleSave(shell) {
    shell.setSaveStatus("dirty");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveProject(shell), 2000);
  }

  async function aiAssist(action, shell) {
    const sel = quill.getSelection(true);
    const full = quill.getText();
    const excerpt = sel ? quill.getText(sel.index, sel.length) : full.slice(0, 2000);
    const prompts = {
      polish: `다음 문단을 자연스럽고 명확하게 다듬어 주세요. 결과만 출력:\n\n${excerpt}`,
      expand: `다음 문단을 2~3배 길이로 늘려 주세요. 결과만 출력:\n\n${excerpt}`,
      shorten: `다음 문단을 핵심만 남기고 짧게 줄여 주세요. 결과만 출력:\n\n${excerpt}`,
      tone: `다음 문단을 전문적이고 정중한 톤으로 바꿔 주세요. 결과만 출력:\n\n${excerpt}`,
      spell: `맞춤법과 문법을 교정해 주세요. 결과만 출력:\n\n${excerpt}`,
    };
    const prompt = prompts[action];
    if (!prompt) return;
    const right = shellApi?.right;
    const status = right?.querySelector(".doc-ai-status");
    if (status) status.textContent = "AI 처리 중…";
    try {
      const uid = await deps.ensureDeviceId();
      const res = await deps.apiFetch("/studio/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: "DeepSeek", prompt, user_id: uid }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "AI 실패");
      const text = (data.text || "").trim();
      if (sel && sel.length) {
        quill.deleteText(sel.index, sel.length);
        quill.insertText(sel.index, text);
      } else {
        quill.insertText(quill.getLength() - 1, "\n" + text);
      }
      pushVersion();
      scheduleSave(shell);
      if (status) status.textContent = "완료";
    } catch (e) {
      if (status) status.textContent = "실패";
      deps.showToast?.(e.message || "AI 실패", "error");
    }
  }

  async function exportDocx(shell) {
    try {
      const docx = await loadDocx();
      const text = quill.getText().trim();
      const paras = text.split(/\n+/).filter(Boolean);
      const children = paras.map((p) => new docx.Paragraph({ children: [new docx.TextRun(p)] }));
      if (!children.length) children.push(new docx.Paragraph({ children: [new docx.TextRun("")] }));
      const doc = new docx.Document({ sections: [{ children }] });
      const blob = await docx.Packer.toBlob(doc);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = (shell.getTitle().replace(/[^\w\uAC00-\uD7A3\-]+/g, "_") || "document") + ".docx";
      a.click();
      URL.revokeObjectURL(a.href);
      deps.showToast?.("DOCX 다운로드", "success");
    } catch (e) {
      deps.showToast?.("DOCX 내보내기 실패", "error");
    }
  }

  function exportPrint() {
    const w = window.open("", "_blank");
    if (!w) return;
    w.document.write(`<!DOCTYPE html><html><head><meta charset="utf-8"><title>Print</title>
      <style>body{font-family:Georgia,serif;max-width:720px;margin:40px auto;line-height:1.7;padding:0 24px}</style></head>
      <body>${quill.root.innerHTML}</body></html>`);
    w.document.close();
    w.print();
  }

  function buildAiPanel(shell) {
    shell.right.innerHTML = `
      <div class="studio-doc-ai">
        <div class="studio-panel-label">AI 글쓰기 도우미</div>
        <p class="studio-panel-hint">문단을 선택한 뒤 버튼을 누르세요.</p>
        <div class="studio-doc-ai-btns">
          <button type="button" data-act="polish">다듬기</button>
          <button type="button" data-act="expand">늘리기</button>
          <button type="button" data-act="shorten">줄이기</button>
          <button type="button" data-act="tone">톤 변경</button>
          <button type="button" data-act="spell">맞춤법</button>
        </div>
        <div class="doc-ai-status"></div>
        <div class="studio-panel-label" style="margin-top:16px;">버전 (최근 5개)</div>
        <div class="doc-version-list"></div>
      </div>`;
    shell.right.querySelectorAll("[data-act]").forEach((btn) => {
      btn.addEventListener("click", () => aiAssist(btn.dataset.act, shell));
    });
    renderVersions(shell);
  }

  function renderVersions(shell) {
    const box = shell.right?.querySelector(".doc-version-list");
    if (!box) return;
    box.innerHTML = versions.map((v, i) =>
      `<button type="button" class="doc-version-item" data-i="${i}">${new Date(v.at).toLocaleString()}</button>`
    ).join("") || "<span class='studio-panel-hint'>아직 없음</span>";
    box.querySelectorAll(".doc-version-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        const v = versions[+btn.dataset.i];
        if (v) { pushVersion(); setContent(v.html); scheduleSave(shell); }
      });
    });
  }

  const DocumentEditor = {
    async mount(shell, options) {
      deps = options.deps || {};
      shellApi = shell;
      projectId = options.project?.id || null;
      shell.left.innerHTML = `<div class="studio-panel-label">문서</div><p class="studio-panel-hint">자동 저장 (2초)</p>`;
      shell.center.innerHTML = `<div id="docEditorMount" class="studio-doc-editor"></div>`;
      buildAiPanel(shell);

      await loadQuill();
      quill = new global.Quill("#docEditorMount", {
        theme: "snow",
        modules: {
          toolbar: [
            [{ header: [1, 2, 3, false] }],
            ["bold", "italic", "underline"],
            [{ list: "ordered" }, { list: "bullet" }],
            ["blockquote", "link", "image"],
            ["clean"],
          ],
        },
      });

      const files = options.project?.files || {};
      if (files["content.html"]) setContent(files["content.html"]);
      try {
        versions = JSON.parse(files["versions.json"] || "[]");
      } catch (_) { versions = []; }
      if (options.project?.name) shell.setTitle(options.project.name);

      quill.on("text-change", () => scheduleSave(shell));

      shell.setExportHandler((anchor) => {
        const pop = document.createElement("div");
        pop.className = "studio-export-pop";
        pop.innerHTML = `<button type="button" data-f="docx">DOCX (.docx)</button><button type="button" data-f="pdf">인쇄 → PDF</button>`;
        document.body.appendChild(pop);
        const r = anchor.getBoundingClientRect();
        pop.style.cssText = `position:fixed;top:${r.bottom + 4}px;right:${window.innerWidth - r.right}px;z-index:10050;`;
        const close = () => pop.remove();
        pop.querySelector('[data-f="docx"]')?.addEventListener("click", () => { close(); exportDocx(shell); });
        pop.querySelector('[data-f="pdf"]')?.addEventListener("click", () => { close(); exportPrint(); });
        setTimeout(() => document.addEventListener("click", close, { once: true }), 0);
      });

      shell.setSaveStatus("saved");
    },

    destroy() {
      clearTimeout(saveTimer);
      quill = null;
      shellApi = null;
      projectId = null;
    },
  };

  global.DocumentEditor = DocumentEditor;
})(typeof window !== "undefined" ? window : global);
