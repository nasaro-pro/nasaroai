# Nasaro AI — QA Checklist (Master Prompt v2)

> Last updated: 2026-07-01 · Commit after deploy verification

## D. Dead-button audit (initial)

| Area | Status | Notes |
|------|--------|-------|
| Privacy header button | ✅ Fixed | `buildPrivacyButton` exported on `NasaroFeatures` |
| Collab → Studio CTA | ✅ Added | `collabStudioBtn`, `openCollabInStudio()` |
| Compare card actions | ✅ Added | Continue / Copy / Save / Collab per answer |
| Compare summary → Collab | ✅ Added | `compareStartCollabBtn` |
| Compare diff refresh | ✅ Added | `compareDiffSummaryBtn` → `/compare/summary` |
| Studio hub tools | ✅ Wired | `StudioHubApp` orchestrator |
| Mobile bottom nav | ✅ Added | 5 tabs @ ≤767px |
| Admin endpoints | ⚠️ Partial | Backend complete; `admin.html` has dashboard/support/quota UI — verify each tab after login |

### Known / deferred

- Studio project co-editing invite flow (B-2) — backend invite API not yet added
- Full admin UI for every `/admin/*` route — audit tab-by-tab in browser
- Monaco/ffmpeg on mobile — simplified message; full editor on desktop only

## A. AI Hub

### A-3 Collab → Studio routing

- [ ] Run 4-stage collab for **문서 제작** → finalize → **스튜디오에서 편집** opens doc editor
- [ ] Run collab for **PPT·발표자료** → structured JSON slides → slide editor
- [ ] Run collab for **앱·웹 개발** → code files in Monaco
- [ ] Rework banner shows when verification triggers rework
- [ ] `/collab/finalize` returns `suggested_studio_tool`, `structured_payload`

### A-1 Compare

- [ ] Multi-model compare renders without console errors
- [ ] Summary bar shows common/diff after `/compare/summary`
- [ ] Per-card: Continue / Copy / Save / Collab buttons work

### A-2 Debate

- [ ] Round stepper visible during debate (R1, R2…)
- [ ] Round summary card after 3 speakers via `/debate/round-summary`

## B. SNS / Chat (smoke)

- [ ] Like on work → count updates → refresh persists
- [ ] Post comment → visible after refresh
- [ ] Send DM → other session sees message (poll/SSE)
- [ ] Profile name save persists

## E. Mobile (375px)

- [ ] Bottom tab bar visible; rail hidden
- [ ] All 5 tabs switch workspace without console error
- [ ] Touch targets ≥44px on primary buttons

## Deploy verification

```bash
py -3 -c "import main"
# Browser: hard refresh production URL, console 0 errors on load
# /system/info deploy_ref matches latest commit
```

## Console-zero flows (manual)

1. Load home → AI compare 1 question
2. Debate 1 round
3. Collab template → finalize → open in studio
4. Studio hub → open doc + image tool
5. Works feed like
6. Chat send 1 message
