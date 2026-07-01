# Contributing / 작업 완료 체크리스트

Nasaro AI 저장소에서 코드를 수정할 때 아래를 **작업 완료 전 필수**로 확인합니다.

## 1. 의존성 (`requirements.txt`)

코드에 **표준 라이브러리가 아닌** 새 `import`가 하나라도 추가되면, 즉시 `requirements.txt`에 해당 패키지가 있는지 확인하고 없으면 추가합니다. 이 확인 없이 "작업 완료"로 보고하지 않습니다.

| 사용 패턴 | 필요 패키지 |
|-----------|-------------|
| FastAPI `File` / `Form` / `UploadFile` | `python-multipart` |
| 이미지 처리 | `Pillow` |
| JWT / OAuth | 해당 라이브러리 (예: `python-jose`, `PyJWT`) |
| PDF / 문서 | `pypdf`, `python-docx` 등 |

`requirements.txt`는 추측으로 수정하지 말고, **코드의 import 목록**과 **`pip freeze` 결과**를 대조해 누락을 점검합니다.

## 2. 커밋 전 로컬 기동 테스트

`main.py` 또는 라우트/모델/의존성을 변경한 커밋은 **push 전** 반드시 로컬에서 앱을 기동합니다:

```bash
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8765
```

import 단계에서 예외 없이 서버가 뜨는지 확인합니다. FastAPI는 **모듈 import 시점**에 라우트 시그니처를 검사하므로, `python-multipart` 누락처럼 배포 직후 크래시나는 문제는 로컬 기동 한 번으로 잡을 수 있습니다.

## 3. 배포 후 확인 (Railway)

GitHub `main` push 후 몇 분 안에 Railway 배포 상태·로그를 확인합니다.

- `/health` 응답 확인
- 크래시 시 로그 **맨 아래 줄**의 실제 원인부터 확인 (`RuntimeError: ...` 등). 스택 트레이스는 위에서부터 읽지 말고 **가장 아래 에러 메시지**를 먼저 봅니다.

## 4. 배포 환경

- **주 배포**: Railway (`railway.json`, `RAILWAY_PUBLIC_DOMAIN` / `PUBLIC_APP_URL`)
- Render URL 하드코딩 금지 — 동적 `/api/config`, `deploy-hint.js` 사용

## 5. 프론트엔드 모듈 초기화

- `window.NasaroFeatures`, `StudioApp`, `SocialFeatures`, `AgentPlanUI` 등 외부 `<script>` 모듈 **로드 실패 시에도 핵심 부트스트랩은 반드시 실행**한다.
- **`if (!window.SomeModule) return;`으로 전체 앱 초기화를 중단하는 패턴 금지.** 부가 모듈만 `if (window.X)` / `X?.` 가드 + `console.warn`.
- 새 `/static/*.js` 추가 시 git 커밋·배포 산출물 포함 여부를 확인한다.
