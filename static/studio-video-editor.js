/**
 * Video editor — ffmpeg.wasm trim/merge + timeline UI
 */
(function (global) {
  "use strict";

  let deps = {};
  let clips = [];
  let ffmpeg = null;
  let loaded = false;
  let abortRender = false;

  async function ensureFfmpeg(onProgress) {
    if (loaded && ffmpeg) return ffmpeg;
    onProgress?.(0, "ffmpeg 로딩…");
    const { FFmpeg } = await import("https://cdn.jsdelivr.net/npm/@ffmpeg/ffmpeg@0.12.10/dist/esm/index.js");
    const { fetchFile, toBlobURL } = await import("https://cdn.jsdelivr.net/npm/@ffmpeg/util@0.12.1/dist/esm/index.js");
    ffmpeg = new FFmpeg();
    ffmpeg.on("progress", ({ progress }) => onProgress?.(Math.round((progress || 0) * 100), "렌더링…"));
    const base = "https://cdn.jsdelivr.net/npm/@ffmpeg/core@0.12.6/dist/esm";
    await ffmpeg.load({
      coreURL: await toBlobURL(`${base}/ffmpeg-core.js`, "text/javascript"),
      wasmURL: await toBlobURL(`${base}/ffmpeg-core.wasm`, "application/wasm"),
    });
    global.__ffmpegFetchFile = fetchFile;
    loaded = true;
    return ffmpeg;
  }

  function renderTimeline(shell) {
    const track = shell.bottom?.querySelector(".video-timeline-track");
    if (!track) return;
    track.innerHTML = clips.map((c, i) =>
      `<div class="video-clip" data-i="${i}" style="flex:${Math.max(1, c.duration || 5)}">
        <span>${c.name || "클립 " + (i + 1)}</span>
        <button type="button" class="video-clip-del" data-i="${i}">×</button>
      </div>`
    ).join("") || "<span class='studio-panel-hint'>클립을 추가하세요</span>";
    track.querySelectorAll(".video-clip-del").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        clips.splice(+btn.dataset.i, 1);
        renderTimeline(shell);
        updatePreview(shell);
      });
    });
  }

  function updatePreview(shell) {
    const vid = shell.center?.querySelector("#videoPreview");
    if (vid && clips[0]?.url) { vid.src = clips[0].url; }
  }

  async function addClipFromFile(file, shell) {
    const url = URL.createObjectURL(file);
    const dur = await new Promise((res) => {
      const v = document.createElement("video");
      v.preload = "metadata";
      v.onloadedmetadata = () => res(v.duration || 5);
      v.onerror = () => res(5);
      v.src = url;
    });
    if (dur > 120) deps.showToast?.("2분 이상 영상은 브라우저 처리가 느릴 수 있습니다.", "warn", 5000);
    clips.push({ name: file.name, url, blob: file, duration: dur, trimStart: 0, trimEnd: dur });
    renderTimeline(shell);
    updatePreview(shell);
  }

  async function renderExport(shell) {
    if (!clips.length) { deps.showToast?.("클립이 없습니다.", "warn"); return; }
    const prog = shell.center?.querySelector(".video-render-progress");
    const cancelBtn = shell.center?.querySelector("#videoRenderCancel");
    abortRender = false;
    if (prog) { prog.style.display = ""; prog.textContent = "준비 중…"; }
    try {
      const ff = await ensureFfmpeg((pct, msg) => {
        if (prog) prog.textContent = `${msg} ${pct}%`;
      });
      if (abortRender) return;
      const fetchFile = global.__ffmpegFetchFile;
      for (let i = 0; i < clips.length; i++) {
        await ff.writeFile(`in${i}.mp4`, await fetchFile(clips[i].blob || clips[i].url));
      }
      if (clips.length === 1) {
        const c = clips[0];
        const start = c.trimStart || 0;
        const len = (c.trimEnd || c.duration) - start;
        await ff.exec(["-ss", String(start), "-i", "in0.mp4", "-t", String(len), "-c", "copy", "out.mp4"]);
      } else {
        const list = clips.map((_, i) => `file 'in${i}.mp4'`).join("\n");
        await ff.writeFile("list.txt", list);
        await ff.exec(["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "out.mp4"]);
      }
      const data = await ff.readFile("out.mp4");
      const blob = new Blob([data.buffer], { type: "video/mp4" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "edited.mp4";
      a.click();
      URL.revokeObjectURL(a.href);
      deps.showToast?.("MP4 다운로드", "success");
    } catch (e) {
      deps.showToast?.(e.message || "렌더 실패", "error");
    } finally {
      if (prog) prog.style.display = "none";
    }
  }

  const VideoEditor = {
    async mount(shell, options) {
      deps = options.deps || {};
      clips = options.clips || [];
      shell.showBottom(true);
      shell.bottom.innerHTML = `
        <div class="video-timeline">
          <div class="video-timeline-label">타임라인</div>
          <div class="video-timeline-track"></div>
        </div>`;

      shell.left.innerHTML = `
        <div class="studio-panel-label">미디어</div>
        <input type="file" id="videoFileIn" accept="video/*" hidden>
        <button type="button" id="videoAddClip" style="width:100%;">+ 영상 추가</button>
        <button type="button" id="videoGenTab" style="width:100%;margin-top:6px;">AI 영상 생성</button>
        <p class="studio-panel-hint">스튜디오 오디오를 배경음으로 추가할 수 있습니다.</p>`;

      shell.center.innerHTML = `
        <div class="video-editor-center">
          <video id="videoPreview" controls playsinline style="max-width:100%;max-height:360px;border-radius:8px;background:#000;"></video>
          <div class="video-render-progress" style="display:none;margin-top:8px;padding:8px;background:#f3f4f6;border-radius:8px;"></div>
          <button type="button" id="videoRenderCancel" style="display:none;margin-top:4px;">취소</button>
        </div>`;

      shell.right.innerHTML = `
        <div class="studio-panel-label">편집</div>
        <label class="studio-panel-hint">트림 시작(초)</label>
        <input type="number" id="videoTrimStart" min="0" step="0.1" value="0" style="width:100%;margin-bottom:8px;">
        <label class="studio-panel-hint">트림 끝(초)</label>
        <input type="number" id="videoTrimEnd" min="0" step="0.1" value="5" style="width:100%;margin-bottom:8px;">
        <button type="button" id="videoApplyTrim" style="width:100%;">트림 적용</button>
        <button type="button" id="videoRenderBtn" style="width:100%;margin-top:12px;background:#ef4444;color:#fff;border:none;padding:10px;border-radius:8px;font-weight:700;cursor:pointer;">MP4 내보내기</button>`;

      renderTimeline(shell);
      updatePreview(shell);

      const fileIn = shell.left.querySelector("#videoFileIn");
      shell.left.querySelector("#videoAddClip")?.addEventListener("click", () => fileIn?.click());
      fileIn?.addEventListener("change", () => {
        const f = fileIn.files?.[0];
        if (f) addClipFromFile(f, shell);
        fileIn.value = "";
      });

      shell.right.querySelector("#videoApplyTrim")?.addEventListener("click", () => {
        if (!clips[0]) return;
        clips[0].trimStart = parseFloat(shell.right.querySelector("#videoTrimStart")?.value) || 0;
        clips[0].trimEnd = parseFloat(shell.right.querySelector("#videoTrimEnd")?.value) || clips[0].duration;
        deps.showToast?.("트림 설정 적용", "success");
      });

      shell.right.querySelector("#videoRenderBtn")?.addEventListener("click", () => renderExport(shell));
      shell.left.querySelector("#videoGenTab")?.addEventListener("click", () => options.onSwitchGenerate?.());

      shell.setExportHandler(() => renderExport(shell));
    },

    destroy() {
      clips.forEach((c) => { if (c.url?.startsWith("blob:")) URL.revokeObjectURL(c.url); });
      clips = [];
      abortRender = true;
    },

    addGeneratedClip(url, name) {
      clips.push({ name: name || "AI 영상", url, duration: 5, trimStart: 0, trimEnd: 5 });
    },
  };

  global.VideoEditor = VideoEditor;
})(typeof window !== "undefined" ? window : global);
