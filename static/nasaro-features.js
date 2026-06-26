/** Nasaro AI — UI helpers (i18n, AI pick, voice, share, agent schedule) */
(function () {
    const MODELS = ["OpenAI", "Anthropic", "Google", "xAI", "Perplexity", "DeepSeek"];
    const FASTEST_MODEL = "DeepSeek";

    const I18N = {
        ko: {
            works_in_progress: "협업 작업 중",
            session_history: "기록",
            session_history_hint: "이번 세션에서 한 질문·답변 (자동 저장 없음)",
            session_empty: "아직 기록이 없습니다.",
            result_files: "결과물 파일",
            result_files_hint: "「결과물 저장」으로만 추가됩니다.",
            result_files_login: "로그인: 서버·오프라인 보관",
            result_files_guest: "비로그인: 이 탭에서만",
            save_result: "결과물 저장",
            save_result_done: "결과물에 저장했습니다.",
            voice_start: "음성",
            voice_unsupported: "음성 입력을 지원하지 않습니다.",
            privacy_on: "프라이버시 켬",
            privacy_off: "프라이버시 끔",
            privacy_tip: "자동 기록·동기화·공유 없음. 이 화면에서만 표시. 「결과물 저장」은 수동 가능.",
            ai_pick: "AI",
            ai_all: "전체",
            ai_collab_pick: "추천 AI",
            export_result: "결과물 생성",
            share_link: "공유 링크",
            work_complete: "작업 완료",
            delete_confirm: "정말 삭제할까요?",
            scheduled_done: "예약 임무 실행",
            scheduled_badge: "예약",
            lang: "언어",
            theme: "테마",
            theme_light: "라이트",
            theme_dark: "다크",
            login: "로그인",
            signup: "회원가입",
            logout: "로그아웃",
            username: "아이디",
            password: "비밀번호",
            remember_username: "아이디 기억",
            remember_password: "비밀번호 저장 (자동 로그인)",
            mode_compare: "비교",
            mode_debate: "토론",
            mode_collab: "협업",
            mode_agent: "에이전트",
            new_chat: "새 대화",
            ph_compare: "비교할 질문…",
            ph_debate: "토론 주제…",
            ph_debate_cont: "의견 추가…",
            ph_collab: "작업 설명…",
            ph_collab_done: "추가 요청…",
            ph_agent: "임무 지시 (예: 5분 뒤 네이버 열어줘)…",
            btn_compare: "비교 ▶",
            btn_debate: "토론 ▶",
            btn_debate_next: "다음 ▶",
            btn_collab: "AI 추천 ▶",
            btn_collab_busy: "분석…",
            settings: "설정",
            guide_title: "Nasaro AI 사용법",
            account: "계정",
            usage_today: "오늘 사용량",
            font_size: "글씨 크기",
            user_guide: "사용 가이드",
            install_ext: "PC 확장이 필요합니다. 설치 페이지로 이동합니다.",
        },
        en: {
            works_in_progress: "Collab in progress",
            session_history: "History",
            session_history_hint: "This session only (not auto-saved)",
            session_empty: "No history yet.",
            result_files: "Result files",
            result_files_hint: "Added via Save result only.",
            result_files_login: "Signed in: cloud backup",
            result_files_guest: "Guest: this tab only",
            save_result: "Save result",
            save_result_done: "Saved.",
            voice_start: "Voice",
            voice_unsupported: "Voice not supported.",
            privacy_on: "Privacy on",
            privacy_off: "Privacy off",
            privacy_tip: "No auto log/sync/share. Manual save works.",
            ai_pick: "AI",
            ai_all: "All",
            ai_collab_pick: "Recommend AI",
            export_result: "Export",
            share_link: "Share",
            work_complete: "Done",
            delete_confirm: "Delete permanently?",
            scheduled_done: "Scheduled task ran",
            scheduled_badge: "Scheduled",
            lang: "Language",
            theme: "Theme",
            theme_light: "Light",
            theme_dark: "Dark",
            login: "Log in",
            signup: "Sign up",
            logout: "Log out",
            username: "Username",
            password: "Password",
            remember_username: "Remember username",
            remember_password: "Save password (auto login)",
            mode_compare: "Compare",
            mode_debate: "Debate",
            mode_collab: "Collab",
            mode_agent: "Agent",
            new_chat: "New chat",
            ph_compare: "Question to compare…",
            ph_debate: "Debate topic…",
            ph_debate_cont: "Add opinion…",
            ph_collab: "Describe your task…",
            ph_collab_done: "Follow-up…",
            ph_agent: "Mission (e.g. open Naver in 5 min)…",
            btn_compare: "Compare ▶",
            btn_debate: "Debate ▶",
            btn_debate_next: "Next ▶",
            btn_collab: "Recommend ▶",
            btn_collab_busy: "Analyzing…",
            settings: "Settings",
            guide_title: "How to use Nasaro AI",
            account: "Account",
            usage_today: "Usage today",
            font_size: "Font size",
            user_guide: "User guide",
            install_ext: "Extension required. Opening install page.",
        },
    };

    const OFFLINE_DB = "nasaroai_offline_v1";
    const OFFLINE_STORE = "snapshots";
    const OFFLINE_MAX = 50;

    let lang = localStorage.getItem("nasaroai_lang") || "ko";
    let theme = localStorage.getItem("nasaroai_theme") || "light";
    let privacyMode = localStorage.getItem("nasaroai_privacy") === "1";
    let modelsCompare = JSON.parse(localStorage.getItem("nasaroai_models_compare") || "null") || MODELS.slice();
    let modelsDebate = JSON.parse(localStorage.getItem("nasaroai_models_debate") || "null") || MODELS.slice();
    let modelsAgent = JSON.parse(localStorage.getItem("nasaroai_models_agent") || "null") || MODELS.slice();
    let collabModel = localStorage.getItem("nasaroai_model_collab") || "Perplexity";
    let agentScheduledTasks = JSON.parse(localStorage.getItem("nasaroai_agent_scheduled") || "[]");
    let agentScheduleTimer = null;

    function t(key) {
        return (I18N[lang] && I18N[lang][key]) || I18N.ko[key] || key;
    }

    function isPrivacyMode() { return privacyMode; }

    function saveModels(mode) {
        if (mode === "collab") localStorage.setItem("nasaroai_model_collab", collabModel);
        else if (mode === "debate") localStorage.setItem("nasaroai_models_debate", JSON.stringify(modelsDebate));
        else if (mode === "agent") localStorage.setItem("nasaroai_models_agent", JSON.stringify(modelsAgent));
        else localStorage.setItem("nasaroai_models_compare", JSON.stringify(modelsCompare));
    }

    function getModelsForMode(mode) {
        if (mode === "collab") return [collabModel || FASTEST_MODEL];
        const list = mode === "debate" ? modelsDebate : mode === "agent" ? modelsAgent : modelsCompare;
        return list.length ? list : MODELS.slice();
    }

    function getSelectedModels(mode = "compare") {
        return getModelsForMode(mode);
    }

    function getPrimaryModel(mode = "compare") {
        const m = getModelsForMode(mode);
        if (!m.length || m.length >= MODELS.length) return FASTEST_MODEL;
        return m[0];
    }

    function isAllModels(mode) {
        return getModelsForMode(mode).length >= MODELS.length;
    }

    function applyTheme() {
        document.documentElement.dataset.theme = theme;
        localStorage.setItem("nasaroai_theme", theme);
    }

    function applyLang() {
        document.documentElement.lang = lang === "en" ? "en" : "ko";
        localStorage.setItem("nasaroai_lang", lang);
        document.querySelectorAll("[data-i18n]").forEach(el => {
            const k = el.getAttribute("data-i18n");
            if (k) el.textContent = t(k);
        });
        window.dispatchEvent(new CustomEvent("nasaroai:lang", { detail: { lang } }));
    }

    function buildModelPicker(mode, opts) {
        const wrap = document.createElement("div");
        wrap.className = "input-tool-group model-picker-wrap";

        if (mode === "collab") {
            const label = document.createElement("span");
            label.className = "input-tool-label";
            label.textContent = t("ai_collab_pick");
            const sel = document.createElement("select");
            sel.className = "input-tool-select";
            MODELS.forEach(m => {
                const o = document.createElement("option");
                o.value = m;
                o.textContent = m;
                if (m === collabModel) o.selected = true;
                sel.appendChild(o);
            });
            sel.addEventListener("change", () => {
                collabModel = sel.value;
                saveModels("collab");
                opts.onModelsChange?.(getModelsForMode("collab"));
            });
            wrap.append(label, sel);
            return wrap;
        }

        let current = mode === "debate" ? modelsDebate : mode === "agent" ? modelsAgent : modelsCompare;
        let pickerBtn = null;

        const updateLabel = () => {
            if (!pickerBtn) return;
            const label = current.length >= MODELS.length
                ? t("ai_all")
                : (current.length === 1 ? current[0] : current.join(" · "));
            const short = label.length > 24 ? label.slice(0, 24) + "…" : label;
            pickerBtn.textContent = `🤖 ${short}`;
            pickerBtn.title = current.length >= MODELS.length ? t("ai_all") : current.join(", ");
        };

        const sync = () => {
            if (mode === "debate") modelsDebate = current.slice();
            else if (mode === "agent") modelsAgent = current.slice();
            else modelsCompare = current.slice();
            saveModels(mode);
            opts.onModelsChange?.(getModelsForMode(mode));
            updateLabel();
        };

        const chips = document.createElement("div");
        chips.className = "ai-chips-row" + (mode === "agent" ? " agent-inline-chips" : "");

        const allBtn = document.createElement("button");
        allBtn.type = "button";
        allBtn.className = "ai-chip" + (current.length >= MODELS.length ? " active" : "");
        allBtn.textContent = t("ai_all");
        allBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            current = current.length >= MODELS.length ? [MODELS[0]] : MODELS.slice();
            renderChips();
            sync();
        });
        chips.appendChild(allBtn);

        const renderChips = () => {
            chips.querySelectorAll(".ai-chip:not(:first-child)").forEach(el => el.remove());
            MODELS.forEach(m => {
                const b = document.createElement("button");
                b.type = "button";
                b.className = "ai-chip" + (current.includes(m) ? " active" : "");
                b.textContent = m;
                b.addEventListener("click", (e) => {
                    e.stopPropagation();
                    if (current.includes(m)) current = current.filter(x => x !== m);
                    else current.push(m);
                    if (!current.length) current = MODELS.slice();
                    renderChips();
                    sync();
                });
                chips.appendChild(b);
            });
            allBtn.classList.toggle("active", current.length >= MODELS.length);
        };
        renderChips();

        const trigger = document.createElement("div");
        trigger.className = "input-tool-group ai-picker-trigger model-picker-wrap" +
            (mode === "agent" ? " agent-ai-picker-trigger" : "");

        pickerBtn = document.createElement("button");
        pickerBtn.type = "button";
        pickerBtn.className = "input-tool-btn ai-pick-trigger-btn";
        updateLabel();

        const pop = document.createElement("div");
        pop.className = "ai-picker-popover";
        const popHead = document.createElement("div");
        popHead.className = "ai-picker-popover-head";
        popHead.textContent = t("ai_pick") + " · " + (lang === "en" ? "multi-select" : "다중 선택");
        pop.append(popHead, chips);
        pickerBtn.addEventListener("click", e => {
            e.stopPropagation();
            document.querySelectorAll(".ai-picker-popover.open").forEach(p => { if (p !== pop) p.classList.remove("open"); });
            pop.classList.toggle("open");
        });
        if (!window._nasaroAiPickerClose) {
            window._nasaroAiPickerClose = true;
            document.addEventListener("click", (e) => {
                if (e.target.closest(".ai-picker-popover") || e.target.closest(".ai-picker-trigger")) return;
                document.querySelectorAll(".ai-picker-popover.open").forEach(p => p.classList.remove("open"));
            });
        }
        trigger.append(pickerBtn, pop);
        return trigger;
    }

    function buildPrivacyButton(textarea, opts = {}) {
        const privBtn = document.createElement("button");
        privBtn.type = "button";
        privBtn.className = "input-tool-btn privacy-btn privacy-btn-compact" + (privacyMode ? " active" : "");
        privBtn.title = t("privacy_tip");
        privBtn.innerHTML = `<span class="priv-text">${privacyMode ? "🔒" : "🔓"}</span><span class="priv-q" title="${t("privacy_tip")}">?</span>`;
        privBtn.addEventListener("click", e => {
            if (e.target.classList.contains("priv-q")) return;
            privacyMode = !privacyMode;
            localStorage.setItem("nasaroai_privacy", privacyMode ? "1" : "0");
            privBtn.classList.toggle("active", privacyMode);
            const pt = privBtn.querySelector(".priv-text");
            if (pt) pt.textContent = privacyMode ? "🔒" : "🔓";
            opts.onPrivacyChange?.(privacyMode);
        });
        privBtn.querySelector(".priv-q")?.addEventListener("click", e => {
            e.stopPropagation();
            opts.showToast?.(t("privacy_tip"), "info", 5000);
        });
        return privBtn;
    }

    function buildVoiceButton(textarea, opts = {}) {
        const voiceBtn = document.createElement("button");
        voiceBtn.type = "button";
        voiceBtn.className = "input-tool-btn icon-tool-btn";
        voiceBtn.textContent = "🎤";
        voiceBtn.title = t("voice_start");
        voiceBtn.addEventListener("click", () => startVoiceInput(textarea, voiceBtn, opts.showToast));
        return voiceBtn;
    }

    function buildDockToolbarParts(textarea, opts = {}) {
        const mode = opts.mode || "compare";
        return {
            ai: buildModelPicker(mode, opts),
            privacy: buildPrivacyButton(textarea, opts),
            voice: buildVoiceButton(textarea, opts),
        };
    }

    function buildAgentModelToolbar(opts = {}) {
        const wrap = document.createElement("div");
        wrap.className = "agent-ai-toolbar";
        wrap.appendChild(buildModelPicker("agent", opts));
        return wrap;
    }

    function buildInputToolbar(textarea, opts = {}) {
        const mode = opts.mode || "compare";
        const row = document.createElement("div");
        row.className = "input-tool-row";
        row.appendChild(buildModelPicker(mode, opts));
        row.append(buildPrivacyButton(textarea, opts), buildVoiceButton(textarea, opts));
        return row;
    }

    function startVoiceInput(textarea, btn, showToast) {
        const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SR) { showToast?.(t("voice_unsupported"), "warn"); return; }
        const rec = new SR();
        rec.lang = lang === "en" ? "en-US" : "ko-KR";
        rec.interimResults = false;
        btn.disabled = true;
        rec.onresult = e => {
            const text = e.results[0][0].transcript;
            textarea.value = (textarea.value ? textarea.value + " " : "") + text;
            textarea.dispatchEvent(new Event("input", { bubbles: true }));
        };
        rec.onerror = () => showToast?.(t("voice_unsupported"), "warn");
        rec.onend = () => { btn.disabled = false; };
        rec.start();
    }

    function parseScheduleFromText(text) {
        const raw = String(text || "").trim();
        if (!raw) return null;
        const ko = raw.match(/(\d+)\s*분\s*(?:뒤|후|후에|이\s*후)?\s*(.*)$/i) || raw.match(/(\d+)분(?:뒤|후)(.*)$/);
        if (ko) {
            const mins = parseInt(ko[1], 10);
            const mission = (ko[2] || raw).replace(/^(에|에\s*)/, "").trim() || raw;
            if (mins > 0 && mins <= 1440) return { delayMs: mins * 60000, mission: mission || raw, label: `${mins}분 후` };
        }
        const en = raw.match(/(?:in\s*)?(\d+)\s*min(?:ute)?s?\s*(?:later|after)?\s*(.*)$/i);
        if (en) {
            const mins = parseInt(en[1], 10);
            const mission = (en[2] || raw).trim() || raw;
            if (mins > 0) return { delayMs: mins * 60000, mission: mission || raw, label: `${mins} min later` };
        }
        return null;
    }

    function addAgentSchedule(task, onRun) {
        agentScheduledTasks.push(task);
        localStorage.setItem("nasaroai_agent_scheduled", JSON.stringify(agentScheduledTasks));
        startAgentScheduleChecker(onRun);
        return task;
    }

    function startAgentScheduleChecker(onRun) {
        if (agentScheduleTimer) clearInterval(agentScheduleTimer);
        agentScheduleTimer = setInterval(() => {
            const now = Date.now();
            const due = [];
            agentScheduledTasks = agentScheduledTasks.filter(task => {
                if (task.runAt <= now) { due.push(task); return false; }
                return true;
            });
            if (due.length) localStorage.setItem("nasaroai_agent_scheduled", JSON.stringify(agentScheduledTasks));
            due.forEach(task => onRun?.(task.mission, task));
        }, 3000);
    }

    function getUserGuideHtml() {
        if (lang === "en") {
            return `<p><strong>Compare</strong> — Ask once, see answers from multiple AIs side by side. Pick AIs with chips.</p>
            <p><strong>Debate</strong> — AIs discuss your topic in rounds.</p>
            <p><strong>Collab</strong> — 4-step workflow with one recommended AI per step.</p>
            <p><strong>Agent</strong> — Automate on PC (extension) or Android app. Say "in 5 minutes do X" to schedule.</p>
            <p><strong>Save results</strong> — Tap 💾 Save result after answers. Login to keep across devices.</p>
            <p><strong>Privacy 🔒</strong> — No auto logging. Answers stay on screen until you save.</p>`;
        }
        return `<p><strong>비교</strong> — 한 번 질문하면 여러 AI 답을 나란히 봅니다. AI 칩으로 선택하세요.</p>
            <p><strong>토론</strong> — AI들이 주제별로 라운드 토론합니다.</p>
            <p><strong>협업</strong> — 4단계 작업. 단계별 AI 추천·변경 가능.</p>
            <p><strong>에이전트</strong> — PC(확장) 또는 Android 앱으로 자동 실행. 「5분 뒤 ○○해줘」라고 하면 예약됩니다.</p>
            <p><strong>결과물 저장</strong> — 답변 후 💾 결과물 저장. 로그인하면 기기 간 유지.</p>
            <p><strong>프라이버시 🔒</strong> — 자동 기록 없음. 저장 버튼을 눌러야 보관됩니다.</p>`;
    }

    async function createShareLink(kind, title, payload, apiFetch, showToast, opts = {}) {
        if (isPrivacyMode()) {
            showToast?.(lang === "en" ? "Disabled in privacy mode" : "프라이버시 모드에서는 공유할 수 없습니다.", "warn");
            return null;
        }
        if (opts.requireLogin && !opts.loggedIn) {
            showToast?.(lang === "en" ? "Sign in to share via account." : "계정 로그인 후 공유할 수 있습니다.", "warn");
            return null;
        }
        try {
            const res = await apiFetch("/share/create", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ kind, title, payload }),
            });
            if (!res.ok) throw new Error("share failed");
            const data = await res.json();
            const url = location.origin + (data.url || "/?share=" + data.id);
            try {
                await navigator.clipboard.writeText(url);
                showToast?.((lang === "en" ? "Link: " : "링크: ") + url, "success", 5000);
            } catch { showToast?.(url, "info", 6000); }
            return url;
        } catch {
            showToast?.(lang === "en" ? "Share failed" : "공유 실패", "error");
            return null;
        }
    }

    function exportMarkdown(title, sections, format = "md") {
        const lines = [`# ${title}`, "", `> ${new Date().toLocaleString()}`, ""];
        sections.forEach(({ heading, body }) => {
            if (heading) lines.push(`## ${heading}`, "");
            lines.push(String(body || "").trim(), "");
        });
        const safe = title.replace(/[^\w\uAC00-\uD7A3\-]+/g, "_").slice(0, 40) || "nasaroai";
        const content = lines.join("\n");
        if (format === "html" || format === "pdf") {
            const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><title>${title}</title>
            <style>body{font-family:sans-serif;max-width:800px;margin:40px auto;line-height:1.6;padding:0 20px}
            h1{border-bottom:2px solid #333}h2{color:#444;margin-top:24px}pre{white-space:pre-wrap}</style></head>
            <body><h1>${title}</h1>${sections.map(s => `<h2>${s.heading || ""}</h2><pre>${String(s.body || "").replace(/</g,"&lt;")}</pre>`).join("")}</body></html>`;
            if (format === "pdf") {
                const w = window.open("", "_blank");
                if (w) { w.document.write(html); w.document.close(); w.print(); }
                return;
            }
            downloadTextFile(`${safe}.html`, html, "text/html;charset=utf-8");
            return;
        }
        const ext = format === "txt" ? "txt" : "md";
        downloadTextFile(`${safe}.${ext}`, content, format === "txt" ? "text/plain;charset=utf-8" : "text/markdown;charset=utf-8");
    }

    function promptExportFormat(title, sections) {
        const fmt = prompt(
            lang === "en"
                ? "Export format: md / txt / html / pdf\n(md=Markdown, txt=Plain, html=HTML, pdf=Print PDF)"
                : "내보내기 형식: md / txt / html / pdf\n(md=마크다운, txt=텍스트, html=HTML, pdf=인쇄→PDF)",
            "md"
        );
        if (!fmt) return;
        exportMarkdown(title, sections, fmt.trim().toLowerCase());
    }

    function downloadTextFile(filename, content, mime) {
        const blob = new Blob([content], { type: mime || "text/markdown;charset=utf-8" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        a.click();
        URL.revokeObjectURL(a.href);
    }

    function addResultActions(container, { title, sections, kind, payload, apiFetch, showToast, onSaveResult }) {
        if (!container || container.querySelector(".result-action-row")) return;
        const row = document.createElement("div");
        row.className = "result-action-row";
        const save = document.createElement("button");
        save.type = "button";
        save.className = "result-action-btn primary-save";
        save.textContent = "💾 " + t("save_result");
        save.addEventListener("click", () => onSaveResult?.({ title, sections, kind, payload }));
        const exp = document.createElement("button");
        exp.type = "button";
        exp.className = "result-action-btn";
        exp.textContent = "📄 " + t("export_result");
        exp.addEventListener("click", () => promptExportFormat(title, sections));
        const share = document.createElement("button");
        share.type = "button";
        share.className = "result-action-btn";
        share.textContent = "🔗 " + t("share_link");
        share.addEventListener("click", () => createShareLink(kind, title, payload, apiFetch, showToast, { requireLogin: true, loggedIn: !!opts.loggedIn }));
        row.append(save, exp, share);
        container.appendChild(row);
    }

    function loadShareFromUrl(apiFetch, showToast) {
        const id = new URLSearchParams(location.search).get("share");
        if (!id) return;
        apiFetch("/share/" + encodeURIComponent(id)).then(r => r.ok ? r.json() : null).then(data => {
            if (!data) return;
            const box = document.getElementById("shareViewBanner");
            if (box) { box.style.display = "block"; box.innerHTML = `<strong>${data.title || "Shared"}</strong>`; }
        }).catch(() => {});
    }

    function isNativeApp() {
        try {
            if (window.NasaroAndroidAgent) return true;
            return new URLSearchParams(location.search).get("source") === "app";
        } catch { return false; }
    }

    async function saveOfflineSnapshot(record, loggedIn) {
        if (!isNativeApp() || !loggedIn) return;
        try {
            const db = await openOfflineDb();
            const item = { id: record.id || `result_${Date.now()}`, kind: record.kind || "result", title: record.title || "결과물", saved_at: record.saved_at || new Date().toISOString(), payload: record };
            await new Promise((resolve, reject) => {
                const tx = db.transaction(OFFLINE_STORE, "readwrite");
                tx.objectStore(OFFLINE_STORE).put(item);
                tx.oncomplete = () => resolve();
                tx.onerror = () => reject(tx.error);
            });
            db.close();
        } catch {}
    }

    function openOfflineDb() {
        return new Promise((resolve, reject) => {
            const req = indexedDB.open(OFFLINE_DB, 1);
            req.onupgradeneeded = () => {
                const db = req.result;
                if (!db.objectStoreNames.contains(OFFLINE_STORE)) db.createObjectStore(OFFLINE_STORE, { keyPath: "id" });
            };
            req.onsuccess = () => resolve(req.result);
            req.onerror = () => reject(req.error);
        });
    }

    window.NasaroFeatures = {
        t, I18N, MODELS, FASTEST_MODEL,
        get lang() { return lang; },
        set lang(v) { lang = v; applyLang(); },
        get theme() { return theme; },
        set theme(v) { theme = v; applyTheme(); },
        isPrivacyMode, getSelectedModels, getPrimaryModel, getModelsForMode, isAllModels,
        applyTheme, applyLang, buildInputToolbar, buildDockToolbarParts, buildAgentModelToolbar,
        createShareLink, exportMarkdown, addResultActions, loadShareFromUrl,
        parseScheduleFromText, addAgentSchedule, startAgentScheduleChecker,
        getUserGuideHtml, isNativeApp, saveOfflineSnapshot,
        confirmDelete(msg) { return confirm(msg || t("delete_confirm")); },
    };

    applyTheme();
    applyLang();
})();
