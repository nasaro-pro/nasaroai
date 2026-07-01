/**
 * AI Hub — shared session state across Compare / Debate / Collab
 */
(function (global) {
  "use strict";

  const state = {
    mode: "compare",
    lastCompare: null,
    lastDebate: null,
    lastCollab: null,
    updatedAt: 0,
  };

  function touch() {
    state.updatedAt = Date.now();
    try {
      sessionStorage.setItem("nasaro_ai_hub", JSON.stringify({
        mode: state.mode,
        lastCompare: state.lastCompare,
        lastDebate: state.lastDebate,
        lastCollab: state.lastCollab,
        updatedAt: state.updatedAt,
      }));
    } catch (_) {}
  }

  try {
    const raw = sessionStorage.getItem("nasaro_ai_hub");
    if (raw) Object.assign(state, JSON.parse(raw));
  } catch (_) {}

  const AiHubSession = {
    get state() { return state; },
    setMode(m) { state.mode = m || "compare"; touch(); },
    recordCompare(payload) {
      state.lastCompare = { ...payload, at: Date.now() };
      state.mode = "compare";
      touch();
    },
    recordDebate(payload) {
      state.lastDebate = { ...payload, at: Date.now() };
      state.mode = "debate";
      touch();
    },
    recordCollab(payload) {
      state.lastCollab = { ...payload, at: Date.now() };
      state.mode = "collab";
      touch();
    },
    summary() {
      const parts = [];
      if (state.lastCompare?.query) parts.push(`비교: ${state.lastCompare.query.slice(0, 40)}`);
      if (state.lastDebate?.topic) parts.push(`토론: ${state.lastDebate.topic.slice(0, 40)}`);
      if (state.lastCollab?.task) parts.push(`협업: ${state.lastCollab.task.slice(0, 40)}`);
      return parts.join(" · ") || "AI 허브";
    },
  };

  global.AiHubSession = AiHubSession;
})(typeof window !== "undefined" ? window : global);
