# Nasaro AI — QA Checklist (Master Prompt v2)

> Last updated: 2026-07-01 · Post B-2 / admin / AiHubSession deploy

## D. Dead-button audit

| Area | Status | Notes |
|------|--------|-------|
| Privacy header button | ✅ | `buildPrivacyButton` on `NasaroFeatures` |
| Collab → Studio CTA | ✅ | `openCollabInStudio()` |
| Compare card / summary actions | ✅ | Continue / Copy / Save / Collab / diff summary |
| Studio hub + shared projects | ✅ | Hub thumbs open project; 🤝 badge for shared |
| Studio invite UI | ✅ | Shell right panel · `/studio/projects/{id}/invite` |
| Notification → shared project | ✅ | `studio_invite` opens project in studio |
| Mobile bottom nav | ✅ | 5 tabs @ ≤767px |
| Admin popups list | ✅ | `GET /admin/popups` |
| Admin grant execute | ✅ | Scheduled grant → "지금 실행" → `/admin/coins/grant/{id}/execute` |
| Admin user search | ✅ | Sidebar uses `/admin/users/search` (≥2 chars) |
| Admin quota limit | ✅ | Per-feature 한도 button → `/admin/quota/limit` |
| Compare stray `});` | ✅ Fixed | Removed syntax error blocking script |

## A. AI Hub

- **AiHubSession** (`static/ai-hub-session.js`) records compare / debate / collab in sessionStorage
- Collab finalize → studio routing with `suggested_studio_tool`, `structured_payload`
- Compare summary via `/compare/summary`
- Debate round stepper + session history sync

## B. SNS / Chat (smoke)

- Comment like/post show toast on error
- Works feed like, tips, profile, explore — verify logged-in on production

## B-2 Studio sharing

- [ ] Owner saves project → invite friend by username → friend gets notification
- [ ] Friend taps notification → shared project opens in correct tool
- [ ] Shared project shows role in shell panel (viewer/editor)
- [ ] Hub lists shared projects with 🤝 on thumb

## Deploy verification

```bash
py -3 -c "import main"
# Hard refresh https://web-production-acc66.up.railway.app — console 0 errors on load
# /system/info deploy_ref matches latest commit
```

## Console-zero flows (manual)

1. Load home → AI compare 1 question
2. Debate 1 round
3. Collab template → finalize → open in studio
4. Studio hub → open doc + image tool
5. Works feed like + comment
6. Chat send 1 message
7. Admin login → popups list + grant + user search
