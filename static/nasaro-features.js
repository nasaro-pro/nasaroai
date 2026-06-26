/** Nasaro AI — 보완 아이디어 UI (음성·공유·작업물·i18n·다크모드 등) */
(function () {
    const MODELS = ["OpenAI", "Anthropic", "Google", "xAI", "Perplexity", "DeepSeek"];

    const I18N = {
        ko: {
            works_in_progress: "협업 작업 중",
            result_files: "결과물 파일",
            result_files_hint: "「결과물 저장」 버튼으로만 추가됩니다.",
            result_files_login: "로그인: 서버·오프라인 보관 지원",
            result_files_guest: "비로그인: 이 탭에서만 (데이터 삭제 시 초기화)",
            save_result: "결과물 저장",
            save_result_done: "결과물에 저장했습니다.",
            voice_start: "음성 입력",
            voice_listening: "듣는 중…",
            voice_unsupported: "이 브라우저는 음성 입력을 지원하지 않습니다.",
            privacy_on: "프라이버시 켬",
            privacy_off: "프라이버시 끔",
            privacy_tip: "자동 기록·동기화·공유 없음. 이 화면에서만 표시. 「결과물 저장」은 수동 가능.",
            ai_pick: "AI 선택",
            export_result: "결과물 생성",
            share_link: "공유 링크",
            work_complete: "작업 완료",
            delete_confirm: "정말 삭제할까요? 되돌릴 수 없습니다.",
            scheduled_done: "예약 임무 실행",
            lang: "언어",
            theme: "테마",
            theme_light: "라이트",
            theme_dark: "다크",
            schedule_agent: "예약 에이전트",
        },
        en: {
            works_in_progress: "Collab in progress",
            result_files: "Result files",
            result_files_hint: "Added only via Save result button.",
            result_files_login: "Signed in: cloud + offline backup",
            result_files_guest: "Guest: this tab only",
            save_result: "Save result",
            save_result_done: "Saved to result files.",
            voice_start: "Voice input",
            voice_listening: "Listening…",
            voice_unsupported: "Voice input is not supported in this browser.",
            privacy_on: "Privacy on",
            privacy_off: "Privacy off",
            privacy_tip: "No auto log, sync, or share. Manual save still works.",
            ai_pick: "AI",
            export_result: "Export",
            share_link: "Share link",
            work_complete: "Mark complete",
            delete_confirm: "Delete permanently? This cannot be undone.",
            scheduled_done: "Scheduled task ran",
            lang: "Language",
            theme: "Theme",
            theme_light: "Light",
            theme_dark: "Dark",
            schedule_agent: "Scheduled agent",
        },
    };

    const OFFLINE_DB = "nasaroai_offline_v1";
    const OFFLINE_STORE = "snapshots";
    const OFFLINE_MAX = 50;

    let lang = localStorage.getItem("nasaroai_lang") || "ko";
    let theme = localStorage.getItem("nasaroai_theme") || "light";
    let privacyMode = localStorage.getItem("nasaroai_privacy") === "1";
    let selectedModels = JSON.parse(localStorage.getItem("nasaroai_selected_models") || "null") || MODELS.slice();
    let scheduledTasks = JSON.parse(localStorage.getItem("nasaroai_scheduled_agent") || "[]");
    let scheduleTimer = null;

    function t(key) {
        return (I18N[lang] && I18N[lang][key]) || (I18N.ko[key]) || key;
    }

    function isPrivacyMode() {
        return privacyMode;
    }

    function getSelectedModels() {
        return selectedModels.length ? selectedModels : MODELS.slice();
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
    }

    function isNativeApp() {
        try {
            if (window.NasaroAndroidAgent) return true;
            return new URLSearchParams(location.search).get("source") === "app";
        } catch {
            return false;
        }
    }

    function openOfflineDb() {
        return new Promise((resolve, reject) => {
            if (!window.indexedDB) {
                reject(new Error("no idb"));
                return;
            }
            const req = indexedDB.open(OFFLINE_DB, 1);
            req.onupgradeneeded = () => {
                const db = req.result;
                if (!db.objectStoreNames.contains(OFFLINE_STORE)) {
                    db.createObjectStore(OFFLINE_STORE, { keyPath: "id" });
                }
            };
            req.onsuccess = () => resolve(req.result);
            req.onerror = () => reject(req.error);
        });
    }

    async function saveOfflineSnapshot(record, loggedIn) {
        if (!isNativeApp() || !loggedIn) return;
        try {
            const db = await openOfflineDb();
            const item = {
                id: record.id || `result_${Date.now()}`,
                kind: record.kind || "result",
                title: record.title || "결과물",
                saved_at: record.saved_at || new Date().toISOString(),
                payload: record,
            };
            await new Promise((resolve, reject) => {
                const tx = db.transaction(OFFLINE_STORE, "readwrite");
                tx.objectStore(OFFLINE_STORE).put(item);
                tx.oncomplete = () => resolve();
                tx.onerror = () => reject(tx.error);
            });
            db.close();
            await trimOfflineStore();
        } catch {}
    }

    async function trimOfflineStore() {
        const all = await listOfflineSnapshots();
        if (all.length <= OFFLINE_MAX) return;
        const drop = all.sort((a, b) => a.saved_at.localeCompare(b.saved_at)).slice(0, all.length - OFFLINE_MAX);
        const db = await openOfflineDb();
        await new Promise((resolve, reject) => {
            const tx = db.transaction(OFFLINE_STORE, "readwrite");
            const store = tx.objectStore(OFFLINE_STORE);
            drop.forEach(d => store.delete(d.id));
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
        db.close();
    }

    async function listOfflineSnapshots() {
        if (!isNativeApp()) return [];
        try {
            const db = await openOfflineDb();
            const items = await new Promise((resolve, reject) => {
                const tx = db.transaction(OFFLINE_STORE, "readonly");
                const req = tx.objectStore(OFFLINE_STORE).getAll();
                req.onsuccess = () => resolve(req.result || []);
                req.onerror = () => reject(req.error);
            });
            db.close();
            return items.sort((a, b) => (b.saved_at || "").localeCompare(a.saved_at || ""));
        } catch {
            return [];
        }
    }

    function escapeHtml(s) {
        return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    function downloadTextFile(filename, content) {
        const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        a.click();
        URL.revokeObjectURL(a.href);
    }

    function buildInputToolbar(textarea, opts = {}) {
        const row = document.createElement("div");
        row.className = "input-tool-row";

        const modelWrap = document.createElement("div");
        modelWrap.className = "input-tool-group";
        const modelLabel = document.createElement("span");
        modelLabel.className = "input-tool-label";
        modelLabel.textContent = t("ai_pick");
        const modelSelect = document.createElement("select");
        modelSelect.className = "input-tool-select";
        modelSelect.title = t("ai_pick");
        const optAll = document.createElement("option");
        optAll.value = "all";
        optAll.textContent = lang === "en" ? "All models" : "전체 모델";
        modelSelect.appendChild(optAll);
        MODELS.forEach(m => {
            const o = document.createElement("option");
            o.value = m;
            o.textContent = m;
            if (selectedModels.length === 1 && selectedModels[0] === m) o.selected = true;
            modelSelect.appendChild(o);
        });
        if (selectedModels.length !== 1) modelSelect.value = "all";
        modelSelect.addEventListener("change", () => {
            if (modelSelect.value === "all") {
                selectedModels = MODELS.slice();
            } else {
                selectedModels = [modelSelect.value];
            }
            localStorage.setItem("nasaroai_selected_models", JSON.stringify(selectedModels));
            opts.onModelsChange?.(getSelectedModels());
        });
        modelWrap.append(modelLabel, modelSelect);

        const privBtn = document.createElement("button");
        privBtn.type = "button";
        privBtn.className = "input-tool-btn privacy-btn" + (privacyMode ? " active" : "");
        privBtn.textContent = privacyMode ? "🔒" : "🔓";
        privBtn.title = privacyMode ? t("privacy_on") : t("privacy_off");
        const privHelp = document.createElement("button");
        privHelp.type = "button";
        privHelp.className = "input-tool-help";
        privHelp.textContent = "?";
        privHelp.setAttribute("aria-label", "Privacy help");
        const tip = document.createElement("span");
        tip.className = "input-tool-tip";
        tip.textContent = t("privacy_tip");
        privHelp.appendChild(tip);
        privBtn.addEventListener("click", () => {
            privacyMode = !privacyMode;
            localStorage.setItem("nasaroai_privacy", privacyMode ? "1" : "0");
            privBtn.classList.toggle("active", privacyMode);
            privBtn.textContent = privacyMode ? "🔒" : "🔓";
            privBtn.title = privacyMode ? t("privacy_on") : t("privacy_off");
            opts.onPrivacyChange?.(privacyMode);
        });

        const voiceBtn = document.createElement("button");
        voiceBtn.type = "button";
        voiceBtn.className = "input-tool-btn";
        voiceBtn.textContent = "🎤";
        voiceBtn.title = t("voice_start");
        voiceBtn.addEventListener("click", () => startVoiceInput(textarea, voiceBtn, opts.showToast));

        row.append(modelWrap, privBtn, privHelp, voiceBtn);
        return row;
    }

    function startVoiceInput(textarea, btn, showToast) {
        const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SR) {
            showToast?.(t("voice_unsupported"), "warn");
            return;
        }
        const rec = new SR();
        rec.lang = lang === "en" ? "en-US" : "ko-KR";
        rec.interimResults = false;
        rec.maxAlternatives = 1;
        btn.disabled = true;
        btn.textContent = "…";
        rec.onresult = e => {
            const text = e.results[0][0].transcript;
            textarea.value = (textarea.value ? textarea.value + " " : "") + text;
            textarea.dispatchEvent(new Event("input", { bubbles: true }));
        };
        rec.onerror = () => showToast?.(t("voice_unsupported"), "warn");
        rec.onend = () => {
            btn.disabled = false;
            btn.textContent = "🎤";
        };
        rec.start();
    }

    async function createShareLink(kind, title, payload, apiFetch, showToast) {
        if (isPrivacyMode()) {
            showToast?.(lang === "en" ? "Disabled in privacy mode" : "프라이버시 모드에서는 공유할 수 없습니다.", "warn");
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
                showToast?.((lang === "en" ? "Link copied: " : "링크 복사됨: ") + url, "success", 5000);
            } catch {
                showToast?.(url, "info", 6000);
            }
            return url;
        } catch {
            showToast?.(lang === "en" ? "Share failed" : "공유 링크 생성 실패", "error");
            return null;
        }
    }

    function exportMarkdown(title, sections) {
        const lines = [`# ${title}`, "", `> ${new Date().toLocaleString()}`, ""];
        sections.forEach(({ heading, body }) => {
            if (heading) lines.push(`## ${heading}`, "");
            lines.push(String(body || "").trim(), "");
        });
        const safe = title.replace(/[^\w\uAC00-\uD7A3\-]+/g, "_").slice(0, 40) || "nasaroai";
        downloadTextFile(`${safe}.md`, lines.join("\n"));
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
        exp.addEventListener("click", () => exportMarkdown(title, sections));
        const share = document.createElement("button");
        share.type = "button";
        share.className = "result-action-btn";
        share.textContent = "🔗 " + t("share_link");
        share.addEventListener("click", () => createShareLink(kind, title, payload, apiFetch, showToast));
        row.append(save, exp, share);
        container.appendChild(row);
    }

    function loadShareFromUrl(apiFetch, showToast) {
        const id = new URLSearchParams(location.search).get("share");
        if (!id) return;
        apiFetch("/share/" + encodeURIComponent(id))
            .then(r => r.ok ? r.json() : null)
            .then(data => {
                if (!data) return;
                const box = document.getElementById("shareViewBanner");
                if (!box) return;
                box.style.display = "block";
                box.innerHTML = `<strong>${data.title || "Shared"}</strong> (${data.kind})`;
                showToast?.((lang === "en" ? "Loaded shared: " : "공유 내용: ") + (data.title || id), "info", 4000);
            })
            .catch(() => {});
    }

    function startScheduleChecker(runAgentMission, showToast) {
        if (scheduleTimer) clearInterval(scheduleTimer);
        scheduleTimer = setInterval(() => {
            const now = Date.now();
            let changed = false;
            scheduledTasks = scheduledTasks.filter(task => {
                const at = new Date(task.at).getTime();
                if (Number.isNaN(at) || at > now) return true;
                showToast?.(`${t("scheduled_done")}: ${task.mission}`, "success", 5000);
                runAgentMission?.(task.mission);
                changed = true;
                return false;
            });
            if (changed) localStorage.setItem("nasaroai_scheduled_agent", JSON.stringify(scheduledTasks));
        }, 30000);
    }

    function renderScheduleList(container) {
        if (!container) return;
        container.innerHTML = "";
        if (!scheduledTasks.length) {
            container.textContent = lang === "en" ? "No scheduled tasks." : "예약된 임무가 없습니다.";
            return;
        }
        scheduledTasks.forEach((task, idx) => {
            const div = document.createElement("div");
            div.className = "schedule-item";
            div.textContent = `${task.at} — ${task.mission}`;
            const del = document.createElement("button");
            del.type = "button";
            del.textContent = "✕";
            del.addEventListener("click", () => {
                scheduledTasks.splice(idx, 1);
                localStorage.setItem("nasaroai_scheduled_agent", JSON.stringify(scheduledTasks));
                renderScheduleList(container);
            });
            div.appendChild(del);
            container.appendChild(div);
        });
    }

    function addScheduleForm(form, missionInput, showToast) {
        form?.addEventListener("submit", e => {
            e.preventDefault();
            const at = form.querySelector('[name="at"]')?.value;
            const mission = (missionInput?.value || form.querySelector('[name="mission"]')?.value || "").trim();
            if (!at || !mission) return;
            scheduledTasks.push({ at, mission, id: Date.now().toString() });
            localStorage.setItem("nasaroai_scheduled_agent", JSON.stringify(scheduledTasks));
            renderScheduleList(document.getElementById("scheduleList"));
            showToast?.(lang === "en" ? "Scheduled" : "예약 등록됨", "success");
            form.reset();
        });
    }

    window.NasaroFeatures = {
        t,
        I18N,
        get lang() { return lang; },
        set lang(v) { lang = v; applyLang(); },
        get theme() { return theme; },
        set theme(v) { theme = v; applyTheme(); },
        isPrivacyMode,
        getSelectedModels,
        applyTheme,
        applyLang,
        buildInputToolbar,
        createShareLink,
        exportMarkdown,
        addResultActions,
        loadShareFromUrl,
        startScheduleChecker,
        renderScheduleList,
        addScheduleForm,
        confirmDelete(message) {
            return confirm(message || t("delete_confirm"));
        },
        isNativeApp,
        saveOfflineSnapshot,
        listOfflineSnapshots,
    };

    applyTheme();
    applyLang();
})();
