/**
 * Studio modality renderers (A-1, A-3, A-4, A-5)
 * mount(container, payload) / destroy()
 */
(function (global) {
  "use strict";

  const ACCENTS = { chat: "#7c3aed", code: "#2563eb", image: "#db2777", audio: "#ea580c", video: "#dc2626" };
  const VIDEO_STAGES = [
    { key: "queued", label: "대기열", pct: 10 },
    { key: "processing", label: "처리 중", pct: 45 },
    { key: "rendering", label: "렌더링", pct: 75 },
    { key: "done", label: "완료", pct: 100 },
    { key: "failed", label: "실패", pct: 100 },
  ];

  function mapJobStage(status, progressStage) {
    const s = (progressStage || status || "queued").toLowerCase();
    if (s === "completed" || s === "done") return "done";
    if (s === "failed") return "failed";
    if (s === "running" || s === "processing") return "processing";
    if (s === "rendering") return "rendering";
    return "queued";
  }

  function stageInfo(key) {
    return VIDEO_STAGES.find((x) => x.key === key) || VIDEO_STAGES[0];
  }

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  /* ── Chat / default text ── */
  const ChatRenderer = {
    _el: null,
    mount(container, payload) {
      this.destroy();
      const text = payload?.text || payload?.content || "";
      this._el = document.createElement("div");
      this._el.className = "studio-text-result";
      this._el.style.cssText = "white-space:pre-wrap;line-height:1.55;font-size:14px;";
      this._el.textContent = text || "결과 없음";
      container.innerHTML = "";
      container.appendChild(this._el);
    },
    destroy() {
      this._el = null;
    },
    getText() {
      return this._el?.textContent || "";
    },
  };

  /* ── Image ── */
  const ImageRenderer = {
    _urls: [],
    _container: null,
    mount(container, payload) {
      this.destroy();
      this._container = container;
      const urls = payload?.urls || (payload?.url ? [payload.url] : []);
      this._urls = urls.filter(Boolean);
      container.innerHTML = "";
      if (!this._urls.length) {
        container.textContent = payload?.text || "이미지 없음";
        return;
      }
      const grid = document.createElement("div");
      grid.className = "studio-image-grid";
      this._urls.forEach((url, i) => {
        const img = document.createElement("img");
        img.src = url;
        img.alt = "생성 이미지 " + (i + 1);
        img.loading = "lazy";
        img.addEventListener("click", () => this._openLightbox(url));
        grid.appendChild(img);
      });
      container.appendChild(grid);
    },
    _openLightbox(url) {
      const ov = document.createElement("div");
      ov.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:9000;display:flex;align-items:center;justify-content:center;padding:20px;cursor:pointer;";
      const img = document.createElement("img");
      img.src = url;
      img.style.cssText = "max-width:95%;max-height:95%;border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,.5);";
      ov.appendChild(img);
      ov.addEventListener("click", () => ov.remove());
      document.body.appendChild(ov);
    },
    destroy() {
      this._urls = [];
      this._container = null;
    },
    getUrls() {
      return this._urls.slice();
    },
    getPrimaryUrl() {
      return this._urls[0] || "";
    },
  };

  /* ── Audio (WaveSurfer) ── */
  const AudioRenderer = {
    _waves: [],
    _tracks: [],
    mount(container, payload) {
      this.destroy();
      const urls = payload?.urls || (payload?.url ? [payload.url] : []);
      container.innerHTML = "";
      if (!urls.length) {
        container.textContent = payload?.text || "오디오 없음";
        return;
      }
      urls.forEach((url, idx) => {
        const card = document.createElement("div");
        card.className = "studio-audio-track";
        card.innerHTML = `<div style="font-size:12px;font-weight:700;">트랙 ${idx + 1}</div><div class="studio-audio-wave" data-idx="${idx}"></div>`;
        container.appendChild(card);
        const waveEl = card.querySelector(".studio-audio-wave");
        if (global.WaveSurfer && waveEl) {
          try {
            const ws = global.WaveSurfer.create({
              container: waveEl,
              waveColor: "#cbd5e1",
              progressColor: ACCENTS.audio,
              height: 56,
              url,
              barWidth: 2,
              cursorWidth: 1,
            });
            this._waves.push(ws);
          } catch (e) {
            waveEl.innerHTML = `<audio controls src="${esc(url)}" style="width:100%"></audio>`;
          }
        } else {
          waveEl.innerHTML = `<audio controls src="${esc(url)}" style="width:100%"></audio>`;
        }
        this._tracks.push({ url, card });
      });
    },
    destroy() {
      this._waves.forEach((w) => {
        try { w.destroy(); } catch (_) {}
      });
      this._waves = [];
      this._tracks = [];
    },
    getPrimaryUrl() {
      return this._tracks[0]?.url || "";
    },
  };

  /* ── Video ── */
  const VideoRenderer = {
    _pollTimer: null,
    _jobId: null,
    _container: null,
    _onUpdate: null,
    mount(container, payload) {
      this.destroy();
      this._container = container;
      this._onUpdate = payload?.onUpdate || null;
      container.innerHTML = "";

      if (payload?.polling && payload?.jobId) {
        this._jobId = payload.jobId;
        this._renderProgress(container, "queued", 0, "영상 생성을 시작합니다…");
        this._startPoll(payload.jobId, payload.apiFetch);
        return;
      }

      const url = payload?.url || payload?.video_url || "";
      if (url) {
        this._renderPlayer(container, url, payload?.thumbnail);
      } else if (payload?.error) {
        this._renderError(container, payload.error, payload.refunded);
      } else {
        container.textContent = payload?.text || "동영상 결과 대기 중…";
      }
    },
    _renderProgress(container, stage, pct, msg) {
      const info = stageInfo(stage);
      const p = pct != null ? pct : info.pct;
      container.innerHTML = `
        <div class="studio-video-progress">
          <div class="studio-video-progress-bar"><div class="studio-video-progress-fill" style="width:${p}%"></div></div>
          <div class="studio-video-stage">${esc(info.label)} · ${esc(msg || "")}</div>
        </div>
        <div class="studio-skeleton" style="height:180px;margin-top:12px;border-radius:12px;"></div>`;
    },
    _renderPlayer(container, url, thumb) {
      container.innerHTML = "";
      const vid = document.createElement("video");
      vid.controls = true;
      vid.playsInline = true;
      vid.style.cssText = "width:100%;max-height:360px;border-radius:12px;background:#000;";
      vid.src = url;
      if (thumb) vid.poster = thumb;
      container.appendChild(vid);
      vid.addEventListener("loadeddata", () => {
        try {
          const c = document.createElement("canvas");
          c.width = vid.videoWidth || 320;
          c.height = vid.videoHeight || 180;
          c.getContext("2d").drawImage(vid, 0, 0, c.width, c.height);
          if (!vid.poster) vid.poster = c.toDataURL("image/jpeg", 0.7);
        } catch (_) {}
      });
    },
    _renderError(container, msg, refunded) {
      container.innerHTML = `
        <div style="padding:16px;border:1px solid #fecaca;border-radius:12px;background:#fef2f2;color:#991b1b;">
          <strong>생성 실패</strong><p style="margin:8px 0 0;font-size:13px;">${esc(msg)}</p>
          ${refunded ? '<p style="margin:8px 0 0;font-size:12px;color:#059669;">코인이 환불되었습니다.</p>' : ""}
          <button type="button" class="studio-video-retry" style="margin-top:12px;padding:8px 14px;border-radius:8px;border:none;background:#dc2626;color:#fff;cursor:pointer;font-weight:700;">다시 시도</button>
        </div>`;
      container.querySelector(".studio-video-retry")?.addEventListener("click", () => {
        global.dispatchEvent(new CustomEvent("studio:video-retry"));
      });
    },
    _startPoll(jobId, apiFetch) {
      const fetchFn = apiFetch || global.fetch.bind(global);
      let attempts = 0;
      const tick = async () => {
        attempts++;
        try {
          const r = await fetchFn(`/media/job/${encodeURIComponent(jobId)}`);
          if (!r.ok) throw new Error("상태 조회 실패");
          const job = await r.json();
          const stage = mapJobStage(job.status, job.progress_stage);
          const info = stageInfo(stage);
          if (this._container) {
            if (stage === "done" && (job.result_url || job.url)) {
              this.destroy();
              this.mount(this._container, { url: job.result_url || job.url });
              if (this._onUpdate) this._onUpdate({ done: true, job });
              return;
            }
            if (stage === "failed") {
              this.destroy();
              this.mount(this._container, {
                error: job.error || job.message || "알 수 없는 오류",
                refunded: job.refunded || job.coins_refunded,
              });
              if (this._onUpdate) this._onUpdate({ failed: true, job });
              return;
            }
            this._renderProgress(
              this._container,
              stage,
              Math.min(info.pct + attempts * 2, 90),
              job.message || "처리 중입니다…"
            );
          }
        } catch (e) {
          if (this._container && attempts > 5) {
            this._renderProgress(this._container, "processing", 50, "연결 재시도 중…");
          }
        }
        this._pollTimer = setTimeout(tick, 2500);
      };
      tick();
    },
    destroy() {
      if (this._pollTimer) clearTimeout(this._pollTimer);
      this._pollTimer = null;
      this._jobId = null;
      this._container = null;
      this._onUpdate = null;
    },
    getPrimaryUrl() {
      const v = this._container?.querySelector("video");
      return v?.src || "";
    },
  };

  /* ── Code (delegates to CodeStudio if loaded) ── */
  const CodeRenderer = {
    mount(container, payload) {
      if (global.CodeStudio && global.CodeStudio.mount) {
        global.CodeStudio.mount(container, payload);
        return;
      }
      ChatRenderer.mount(container, payload);
    },
    destroy() {
      if (global.CodeStudio?.destroy) global.CodeStudio.destroy();
    },
    getText() {
      return global.CodeStudio?.getText?.() || ChatRenderer.getText();
    },
  };

  const RENDERERS = {
    chat: ChatRenderer,
    code: CodeRenderer,
    image: ImageRenderer,
    audio: AudioRenderer,
    video: VideoRenderer,
  };

  const StudioApp = {
    modality: "chat",
    container: null,
    activeRenderer: null,
    lastPayload: null,

    init(containerEl) {
      this.container = containerEl;
    },

    setModality(mod) {
      mod = mod || "chat";
      if (this.modality === mod && !this.activeRenderer) return;
      this.destroyRenderer();
      this.modality = mod;
      const ws = document.getElementById("studioWorkspace");
      if (ws) ws.dataset.mod = mod;
      if (this.lastPayload) this.mount(this.lastPayload);
    },

    destroyRenderer() {
      if (this.activeRenderer?.destroy) this.activeRenderer.destroy();
      this.activeRenderer = null;
    },

    getRenderer() {
      return RENDERERS[this.modality] || ChatRenderer;
    },

    mount(payload) {
      if (!this.container) return;
      this.lastPayload = payload;
      this.destroyRenderer();
      const R = this.getRenderer();
      this.activeRenderer = R;
      R.mount(this.container, payload);
    },

    showSkeleton() {
      if (!this.container) return;
      this.destroyRenderer();
      const shapes = {
        chat: "height:120px",
        code: "height:200px",
        image: "height:160px;aspect-ratio:1;max-width:200px",
        audio: "height:80px",
        video: "height:180px",
      };
      const st = shapes[this.modality] || shapes.chat;
      this.container.innerHTML = `<div class="studio-skeleton" style="${st}"></div>`;
    },

    getExportData() {
      const R = this.activeRenderer;
      if (!R) return { type: "text", text: "" };
      if (this.modality === "image") return { type: "image", url: ImageRenderer.getPrimaryUrl(), urls: ImageRenderer.getUrls() };
      if (this.modality === "audio") return { type: "audio", url: AudioRenderer.getPrimaryUrl() };
      if (this.modality === "video") return { type: "video", url: VideoRenderer.getPrimaryUrl() };
      if (this.modality === "code") return { type: "code", text: CodeRenderer.getText() };
      return { type: "text", text: ChatRenderer.getText() };
    },

    /* Upload widget */
    bindUploadZone(zoneEl, inputEl, onUploaded) {
      if (!zoneEl) return;
      const pick = () => inputEl?.click();
      zoneEl.addEventListener("click", pick);
      zoneEl.addEventListener("dragover", (e) => { e.preventDefault(); zoneEl.classList.add("dragover"); });
      zoneEl.addEventListener("dragleave", () => zoneEl.classList.remove("dragover"));
      zoneEl.addEventListener("drop", async (e) => {
        e.preventDefault();
        zoneEl.classList.remove("dragover");
        const f = e.dataTransfer?.files?.[0];
        if (f) await this._uploadFile(f, zoneEl, onUploaded);
      });
      if (inputEl) {
        inputEl.addEventListener("change", async () => {
          const f = inputEl.files?.[0];
          if (f) await this._uploadFile(f, zoneEl, onUploaded);
          inputEl.value = "";
        });
      }
    },

    async _uploadFile(file, zoneEl, onUploaded) {
      zoneEl.textContent = "업로드 중…";
      try {
        const fd = new FormData();
        fd.append("file", file);
        const uid = await (global.ensureDeviceId ? global.ensureDeviceId() : Promise.resolve(""));
        if (uid) fd.append("user_id", uid);
        const r = await fetch("/media/upload", { method: "POST", body: fd });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || data.message || "업로드 실패");
        zoneEl.textContent = "업로드 완료 ✓";
        if (onUploaded) onUploaded(data.url || data.media_url, data);
      } catch (e) {
        zoneEl.textContent = "드래그 또는 클릭하여 업로드";
        if (global.showToast) global.showToast(String(e.message || e), "error");
      }
    },

    renderHistoryGrid(listEl, history) {
      if (!listEl) return;
      listEl.innerHTML = "";
      listEl.className = "studio-history-grid";
      (history || []).slice(0, 20).forEach((item) => {
        const cell = document.createElement("div");
        cell.className = "studio-history-thumb";
        cell.title = item.prompt || item.label || "";
        if (item.thumb || (item.mod === "image" && item.url)) {
          const img = document.createElement("img");
          img.src = item.thumb || item.url;
          img.alt = "";
          cell.appendChild(img);
        } else {
          const icons = { chat: "💬", code: "⌨", image: "🖼", audio: "🎵", video: "🎬" };
          cell.textContent = icons[item.mod] || "•";
        }
        cell.addEventListener("click", () => {
          global.dispatchEvent(new CustomEvent("studio:history-select", { detail: item }));
        });
        listEl.appendChild(cell);
      });
    },
  };

  global.StudioRenderers = RENDERERS;
  global.StudioApp = StudioApp;
})(typeof window !== "undefined" ? window : global);
