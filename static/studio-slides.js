/**
 * Slide editor — Fabric.js canvas + pptxgenjs export
 */
(function (global) {
  "use strict";

  const THEMES = [
    { id: "violet", bg: "#1e1b4b", accent: "#a78bfa", text: "#f5f3ff" },
    { id: "ocean", bg: "#0c4a6e", accent: "#38bdf8", text: "#f0f9ff" },
    { id: "sunset", bg: "#7c2d12", accent: "#fb923c", text: "#fff7ed" },
    { id: "forest", bg: "#14532d", accent: "#4ade80", text: "#ecfdf5" },
    { id: "mono", bg: "#18181b", accent: "#e4e4e7", text: "#fafafa" },
  ];

  let slides = [];
  let activeIdx = 0;
  let canvas = null;
  let deps = {};
  let projectId = null;
  let theme = THEMES[0];
  let saveTimer = null;

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

  async function loadFabric() {
    if (global.fabric) return global.fabric;
    await loadScript("https://cdn.jsdelivr.net/npm/fabric@5.3.0/dist/fabric.min.js");
    return global.fabric;
  }

  async function loadPptx() {
    if (global.PptxGenJS) return global.PptxGenJS;
    await loadScript("https://cdn.jsdelivr.net/npm/pptxgenjs@3.12.0/dist/pptxgen.bundle.js");
    return global.PptxGenJS;
  }

  function newSlide() {
    return { id: "s" + Date.now() + Math.random().toString(36).slice(2, 6), objects: [], bg: theme.bg };
  }

  function loadSlideToCanvas(idx) {
    if (!canvas) return;
    saveCanvasToSlide();
    activeIdx = idx;
    const s = slides[idx];
    canvas.clear();
    canvas.setBackgroundColor(s.bg || theme.bg, canvas.renderAll.bind(canvas));
    if (s.plain) {
      const title = new global.fabric.Textbox(s.plain.title || "", {
        left: 40, top: 40, width: 680, fontSize: 28, fontWeight: "bold", fill: theme.text,
      });
      canvas.add(title);
      (s.plain.bullets || []).forEach((b, i) => {
        canvas.add(new global.fabric.Textbox("• " + b, {
          left: 60, top: 120 + i * 36, width: 640, fontSize: 18, fill: theme.text,
        }));
      });
      canvas.renderAll();
    } else if (s.objects?.length) {
      global.fabric.util.enlivenObjects(s.objects, (objs) => {
        objs.forEach((o) => canvas.add(o));
        canvas.renderAll();
      });
    }
    renderSlideList();
  }

  function saveCanvasToSlide() {
    if (!canvas || !slides[activeIdx]) return;
    if (slides[activeIdx].plain) {
      delete slides[activeIdx].plain;
    }
    slides[activeIdx].objects = canvas.toJSON(["selectable"]).objects || [];
    slides[activeIdx].bg = theme.bg;
  }

  function renderSlideList(shell) {
    const list = shell.left?.querySelector(".slide-list");
    if (!list) return;
    list.innerHTML = slides.map((s, i) =>
      `<button type="button" class="slide-thumb${i === activeIdx ? " active" : ""}" data-i="${i}">${i + 1}</button>`
    ).join("");
    list.querySelectorAll(".slide-thumb").forEach((btn) => {
      btn.addEventListener("click", () => loadSlideToCanvas(+btn.dataset.i));
    });
  }

  async function saveProject(shell) {
    saveCanvasToSlide();
    shell.setSaveStatus("saving");
    try {
      const uid = await deps.ensureDeviceId();
      const files = {
        "slides.json": JSON.stringify({ slides, themeId: theme.id }),
      };
      const body = { user_id: uid, name: shell.getTitle(), project_type: "slide", files };
      let res;
      if (projectId) {
        res = await deps.apiFetch(`/studio/projects/${projectId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      } else {
        res = await deps.apiFetch("/studio/projects", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "저장 실패");
      if (data.project?.id) projectId = data.project.id;
      shell.setSaveStatus("saved");
    } catch (e) {
      shell.setSaveStatus("error");
      deps.showToast?.(e.message || "저장 실패", "error");
    }
  }

  function scheduleSave(shell) {
    shell.setSaveStatus("dirty");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveProject(shell), 2000);
  }

  async function aiGenerateSlides(shell, topic) {
    if (!topic) return;
    const status = shell.right?.querySelector(".slide-ai-status");
    if (status) status.textContent = "AI 슬라이드 생성 중…";
    try {
      const uid = await deps.ensureDeviceId();
      const prompt = `주제: "${topic}"\n5~8장 슬라이드 발표 초안을 JSON 배열로만 출력하세요. 각 항목: {"title":"제목","bullets":["항목1","항목2"]}. 다른 텍스트 없이 JSON만.`;
      const res = await deps.apiFetch("/studio/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: "DeepSeek", prompt, user_id: uid }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "AI 실패");
      const match = (data.text || "").match(/\[[\s\S]*\]/);
      const arr = match ? JSON.parse(match[0]) : [];
      saveCanvasToSlide();
      slides = [];
      arr.forEach((item) => {
        slides.push({ ...newSlide(), plain: { title: item.title || "", bullets: item.bullets || [] } });
      });
      if (!slides.length) slides.push(newSlide());
      loadSlideToCanvas(0);
      scheduleSave(shell);
      if (status) status.textContent = `${slides.length}장 생성됨`;
    } catch (e) {
      if (status) status.textContent = "실패";
      deps.showToast?.(e.message || "AI 실패", "error");
    }
  }

  async function exportPptx(shell) {
    saveCanvasToSlide();
    try {
      const PptxGenJS = await loadPptx();
      const pptx = new PptxGenJS();
      pptx.layout = "LAYOUT_16x9";
      for (const s of slides) {
        const slide = pptx.addSlide();
        slide.background = { color: (s.bg || theme.bg).replace("#", "") };
        (s.objects || []).forEach((o) => {
          if (o.type === "textbox" || o.type === "i-text" || o.type === "text") {
            slide.addText(o.text || "", {
              x: (o.left || 0) / 960 * 10,
              y: (o.top || 0) / 540 * 5.625,
              w: (o.width || 400) / 960 * 10,
              h: 1,
              fontSize: Math.round((o.fontSize || 18) * 0.75),
              color: (o.fill || theme.text).replace("#", ""),
              bold: o.fontWeight === "bold",
            });
          }
        });
      }
      await pptx.writeFile({ fileName: (shell.getTitle().replace(/[^\w\uAC00-\uD7A3\-]+/g, "_") || "slides") + ".pptx" });
      deps.showToast?.("PPTX 다운로드", "success");
    } catch (e) {
      deps.showToast?.("PPTX 내보내기 실패", "error");
    }
  }

  const SlideEditor = {
    async mount(shell, options) {
      deps = options.deps || {};
      projectId = options.project?.id || null;
      await loadFabric();

      const files = options.project?.files || {};
      try {
        const parsed = JSON.parse(files["slides.json"] || "{}");
        slides = parsed.slides?.length ? parsed.slides : [newSlide()];
        theme = THEMES.find((t) => t.id === parsed.themeId) || THEMES[0];
      } catch (_) {
        slides = [newSlide()];
      }
      if (options.project?.name) shell.setTitle(options.project.name);

      shell.left.innerHTML = `
        <div class="studio-panel-label">슬라이드</div>
        <div class="slide-list"></div>
        <div class="slide-list-actions">
          <button type="button" id="slideAdd">+ 추가</button>
          <button type="button" id="slideDup">복제</button>
          <button type="button" id="slideDel">삭제</button>
        </div>`;
      shell.center.innerHTML = `<canvas id="slideCanvas" width="960" height="540"></canvas>`;
      shell.right.innerHTML = `
        <div class="studio-panel-label">속성</div>
        <label class="studio-panel-hint">테마</label>
        <select id="slideThemeSel">${THEMES.map((t) => `<option value="${t.id}">${t.id}</option>`).join("")}</select>
        <button type="button" id="slideAddText" style="margin-top:8px;width:100%;">+ 텍스트</button>
        <button type="button" id="slideAddRect" style="margin-top:4px;width:100%;">+ 도형</button>
        <div class="studio-panel-label" style="margin-top:16px;">AI 초안</div>
        <input id="slideAiTopic" placeholder="발표 주제…" style="width:100%;margin-bottom:6px;padding:6px;border-radius:6px;border:1px solid #e5e7eb;">
        <button type="button" id="slideAiGen" style="width:100%;">AI 슬라이드 생성</button>
        <div class="slide-ai-status studio-panel-hint"></div>`;

      canvas = new global.fabric.Canvas("slideCanvas", { backgroundColor: theme.bg, preserveObjectStacking: true });
      canvas.on("object:modified", () => scheduleSave(shell));

      renderSlideList(shell);
      loadSlideToCanvas(0);

      shell.left.querySelector("#slideAdd")?.addEventListener("click", () => {
        saveCanvasToSlide();
        slides.push(newSlide());
        loadSlideToCanvas(slides.length - 1);
        scheduleSave(shell);
      });
      shell.left.querySelector("#slideDup")?.addEventListener("click", () => {
        saveCanvasToSlide();
        slides.splice(activeIdx + 1, 0, JSON.parse(JSON.stringify(slides[activeIdx])));
        loadSlideToCanvas(activeIdx + 1);
        scheduleSave(shell);
      });
      shell.left.querySelector("#slideDel")?.addEventListener("click", () => {
        if (slides.length <= 1) return;
        slides.splice(activeIdx, 1);
        loadSlideToCanvas(Math.max(0, activeIdx - 1));
        scheduleSave(shell);
      });

      const themeSel = shell.right.querySelector("#slideThemeSel");
      if (themeSel) themeSel.value = theme.id;
      themeSel?.addEventListener("change", () => {
        theme = THEMES.find((t) => t.id === themeSel.value) || THEMES[0];
        canvas.setBackgroundColor(theme.bg, canvas.renderAll.bind(canvas));
        scheduleSave(shell);
      });

      shell.right.querySelector("#slideAddText")?.addEventListener("click", () => {
        const tb = new global.fabric.Textbox("텍스트", { left: 80, top: 80, width: 300, fontSize: 22, fill: theme.text });
        canvas.add(tb);
        canvas.setActiveObject(tb);
      });
      shell.right.querySelector("#slideAddRect")?.addEventListener("click", () => {
        const r = new global.fabric.Rect({ left: 100, top: 100, width: 120, height: 80, fill: theme.accent });
        canvas.add(r);
      });
      shell.right.querySelector("#slideAiGen")?.addEventListener("click", () => {
        aiGenerateSlides(shell, shell.right.querySelector("#slideAiTopic")?.value?.trim());
      });

      shell.setExportHandler(() => exportPptx(shell));
      shell.setSaveStatus("saved");
    },

    destroy() {
      clearTimeout(saveTimer);
      if (canvas) { try { canvas.dispose(); } catch (_) {} canvas = null; }
      projectId = null;
    },
  };

  global.SlideEditor = SlideEditor;
})(typeof window !== "undefined" ? window : global);
