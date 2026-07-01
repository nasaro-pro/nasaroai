# Nasaro AI 플로팅 에이전트 버튼 (Android 앱)

Nasaro AI 앱에서 에이전트를 켜면 화면 위에 질문창 또는 보라색 런처가 떠 있습니다.

## 기능
- 앱의 에이전트 버튼 탭 → 네이티브 오버레이 질문창 열기
- 접기 탭 → 어디서나 보이는 Nasaro AI 런처로 최소화
- 런처 탭 → 질문창 다시 열기
- 접근성 권한 ON → 홈/뒤로/앱 실행/텍스트 클릭/입력/스크롤 실행
- 드래그로 위치 조절 (위치 자동 저장)
- 길게 누르기(600ms) → 버튼 닫기
- 에이전트가 켜진 상태면 부팅 후 런처 복원

## 빌드 방법

### 필요한 것
- Android Studio (최신 버전)  
  → https://developer.android.com/studio 에서 무료 다운로드

### 단계

1. **Android Studio 설치 후 이 폴더 열기**
   - `File → Open` → `android-agent` 폴더 선택

2. **Gradle 동기화 대기**
   - 자동으로 진행됨 (인터넷 필요, 첫 실행 시 5~10분)

3. **APK 빌드**
   - 메뉴: `Build → Build Bundle(s) / APK(s) → Build APK(s)`
   - 완료 후 우측 하단 알림에서 "locate" 클릭
   - `app/build/outputs/apk/debug/app-debug.apk` 생성됨

4. **폰에 설치**
   - `app-debug.apk`를 폰으로 전송 (카카오톡, USB, 구글 드라이브 등)
   - 폰에서 파일 탭 → 설치 허용 (출처를 알 수 없는 앱 허용)

5. **앱 실행 → 에이전트 켜기**
   - 앱 열기 → 사이트 하단 "에이전트" 탭
   - 오버레이 권한 요청이 나오면 ON → 앱으로 돌아오기
   - 에이전트 질문창이 열리고, 접기하면 Nasaro AI 런처가 남음
   - 실제 화면 조작을 요청하면 접근성 설정에서 `Nasaro AI 에이전트`를 ON

## 구조

```
android-agent/
├── app/src/main/
│   ├── AndroidManifest.xml
│   ├── java/com/nasaroai/agent/
│   │   ├── MainActivity.kt      ← 앱 메인 화면
│   │   ├── FloatingService.kt   ← 플로팅 버튼 서비스
│   │   └── BootReceiver.kt      ← 부팅 시 자동 시작
│   └── res/
│       ├── layout/              ← UI 레이아웃
│       └── drawable/            ← 아이콘
└── app/build.gradle
```

## Nasaro AI URL 변경

`FloatingService.kt` 12번째 줄:
```kotlin
private val NASAROAI_URL = "(Railway PUBLIC_APP_URL — 앱 첫 실행 시 설정)"
```
원하는 URL로 바꾸고 다시 빌드하면 됩니다.
