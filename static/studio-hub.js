/**
 * Studio Hub — tool selection grid with recent projects
 */
(function (global) {
  "use strict";

  const TOOLS = [
    { id: "image", icon: "🎨", color: "#ec4899", title: "이미지 생성", desc: "AI로 이미지·일러스트 생성" },
    { id: "video", icon: "🎬", color: "#ef4444", title: "영상 생성 & 편집", desc: "AI 영상 생성 + 브라우저 편집" },
    { id: "audio", icon: "🎵", color: "#8b5cf6", title: "오디오 생성", desc: "TTS·음성 합성" },
    { id: "code", icon: "💻", color: "#2563eb", title: "코드 / 앱 빌더", desc: "Monaco IDE · 실시간 프리뷰" },
    { id: "doc", icon: "📄", color: "#334155", title: "문서 편집기", desc: "리치 텍스트 · AI 글쓰기" },
    { id: "slide", icon: "📊", color: "#ea580c", title: "슬라이드(PPT)", desc: "캔버스 슬라이드 · PPTX 내보내기" },
  ];

  function thumbFor(project) {
    if (project.thumbnail) return project.thumbnail;
    const t = project.project_type || "code";
    const icons = { image: "🎨", video: "🎬", audio: "🎵", code: "💻", doc: "📄", slide: "📊", chat: "💬" };
    return icons[t] || "📁";
  }

  const StudioHub = {
    TOOLS,

    render(root, opts) {
      opts = opts || {};
      const projects = opts.projects || [];
      root.innerHTML = "";
      const hub = document.createElement("div");
      hub.className = "studio-hub";
      hub.innerHTML = `
        <div class="studio-hub-head">
          <h2 class="studio-hub-title">🎨 AI 작업 허브</h2>
          <p class="studio-hub-desc">도구를 선택해 전용 에디터에서 작업하세요.</p>
        </div>
        <div class="studio-hub-grid"></div>`;
      const grid = hub.querySelector(".studio-hub-grid");

      TOOLS.forEach((tool) => {
        const recent = projects.filter((p) => (p.project_type || "code") === tool.id).slice(0, 3);
        const card = document.createElement("button");
        card.type = "button";
        card.className = "studio-hub-card";
        card.style.setProperty("--hub-accent", tool.color);
        card.innerHTML = `
          <div class="studio-hub-card-icon">${tool.icon}</div>
          <div class="studio-hub-card-body">
            <div class="studio-hub-card-title">${tool.title}</div>
            <div class="studio-hub-card-desc">${tool.desc}</div>
            <div class="studio-hub-recent">${recent.length
              ? recent.map((p) => {
                  const thumb = thumbFor(p);
                  const shared = p.shared ? " 🤝" : "";
                  const inner = typeof thumb === "string" && thumb.length <= 2 ? thumb + shared : `<img src="${thumb}" alt="">`;
                  return `<span class="studio-hub-thumb" title="${(p.name || "").replace(/"/g, "&quot;")}${shared}">${inner}</span>`;
                }).join("")
              : "<span class='studio-hub-no-recent'>최근 작업 없음</span>"}</div>
          </div>`;
        card.addEventListener("click", () => opts.onSelect?.(tool.id));
        if (recent.length && opts.onOpenProject) {
          card.querySelectorAll(".studio-hub-thumb").forEach((el, i) => {
            el.addEventListener("click", (e) => {
              e.stopPropagation();
              opts.onOpenProject(recent[i]);
            });
          });
        }
        grid.appendChild(card);
      });

      root.appendChild(hub);
    },
  };

  global.StudioHub = StudioHub;
})(typeof window !== "undefined" ? window : global);
