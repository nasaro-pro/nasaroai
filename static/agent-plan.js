/**
 * Agent plan UI + handoff banner (D-1, D-3)
 */
(function (global) {
  "use strict";

  const AgentPlanUI = {
    steps: [],
    container: null,
    onConfirm: null,

    mount(parent, steps, onConfirm) {
      this.destroy();
      this.steps = steps || [];
      this.onConfirm = onConfirm;
      this.container = document.createElement("div");
      this.container.className = "agent-plan-box";
      this.container.innerHTML = `
        <strong>실행 계획</strong>
        <p style="margin:6px 0 0;font-size:12px;color:#6b7280;">확인 후 실행하세요. 단계별로 진행됩니다.</p>
        <ul class="agent-plan-steps">${this.steps.map((s, i) =>
          `<li class="pending" data-i="${i}"><span>○</span><span>${escapeHtml(s)}</span></li>`
        ).join("")}</ul>
        <div style="display:flex;gap:8px;margin-top:8px;">
          <button type="button" class="agent-plan-run" style="padding:8px 16px;border-radius:8px;border:none;background:#7c3aed;color:#fff;font-weight:700;cursor:pointer;">실행</button>
          <button type="button" class="agent-plan-cancel" style="padding:8px 14px;border-radius:8px;border:1px solid #e5e7eb;background:#fff;cursor:pointer;">취소</button>
        </div>`;
      parent.prepend(this.container);
      this.container.querySelector(".agent-plan-run")?.addEventListener("click", () => {
        if (this.onConfirm) this.onConfirm(this.steps);
        this.setActive(0);
      });
      this.container.querySelector(".agent-plan-cancel")?.addEventListener("click", () => this.destroy());
    },

    setActive(index) {
      if (!this.container) return;
      this.container.querySelectorAll(".agent-plan-steps li").forEach((li, i) => {
        li.className = i < index ? "done" : i === index ? "active" : "pending";
        li.querySelector("span").textContent = i < index ? "✓" : i === index ? "▶" : "○";
      });
    },

    markDone(index) {
      this.setActive(index + 1);
    },

    destroy() {
      this.container?.remove();
      this.container = null;
      this.steps = [];
      this.onConfirm = null;
    },

    showHandoff(parent, message, onContinue) {
      const el = document.createElement("div");
      el.className = "agent-handoff-banner";
      el.style.cssText = "margin:10px 0;padding:14px;border:2px solid #f59e0b;border-radius:12px;background:#fffbeb;font-size:13px;line-height:1.5;";
      el.innerHTML = `
        <strong>🔒 사용자 개입 필요</strong>
        <p style="margin:8px 0 0;">${escapeHtml(message || "로그인·결제·캡차 등 민감 단계입니다. 브라우저에서 직접 진행한 뒤 계속하기를 눌러주세요.")}</p>
        <button type="button" style="margin-top:10px;padding:8px 16px;border-radius:8px;border:none;background:#f59e0b;color:#fff;font-weight:700;cursor:pointer;">계속하기</button>`;
      parent.prepend(el);
      el.querySelector("button")?.addEventListener("click", () => {
        el.remove();
        if (onContinue) onContinue();
      });
      return el;
    },
  };

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  async function fetchPlan(query, userId) {
    const r = await fetch("/agent/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, user_id: userId }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || "계획 생성 실패");
    return data.steps || [];
  }

  global.AgentPlanUI = AgentPlanUI;
  global.fetchAgentPlan = fetchPlan;
})(typeof window !== "undefined" ? window : global);
