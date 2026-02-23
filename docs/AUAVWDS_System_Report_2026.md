# Automated UAV Wing Design System using Generative AI
## AUAVWDS 재구축 시스템 보고서 (v1.0)

- 프로젝트명: `AUAVWDS (Chat-first Wing Designer)`
- 대상 플랫폼: `Windows 10/11`
- 작성일: `2026-02-23`
- 개발 스택: `Electron + React + TypeScript + FastAPI + OpenVSP/VSPAERO + NeuralFoil(호환 경로)`

---

## 초록 (Abstract)
본 보고서는 채팅 기반 무인기(UAV) 날개 설계/해석 시스템 AUAVWDS의 재구축 결과를 정리한 문서이다.  
시스템은 초보 사용자도 자연어 대화만으로 2D 에어포일, 3D 날개 형상, 공력 해석 결과를 확인할 수 있도록 설계되었다.  
아키텍처는 Electron 데스크톱 앱과 Python FastAPI 백엔드로 분리되며, LLM의 도구 호출(tool-calling)을 통해 설계 명령을 실행한다.

핵심 해석 파이프라인은 2단계로 구성된다.
1. 즉답 해석: NeuralFoil 호환 경로(현재 환경에 따라 surrogate 동작 포함)
2. 정밀 해석: OpenVSP + VSPAERO 기반 스크립트 해석

또한 저장/불러오기/비교, CFD 내보내기(OBJ/JSON/VSP3), 설치형 배포(NSIS)까지 포함해 실제 사용 가능한 독립 실행형 설계 도구로 완성하였다.

---

## 1. 연구 배경 및 목표

### 1.1 배경
기존 항공 설계 툴은 전문 용어, 복잡한 입력 패널, 툴 간 분절된 워크플로우로 인해 초보자가 접근하기 어렵다.  
특히 “요구사항 대화 → 형상 생성 → 공력 확인 → 설계 반복”의 전 과정을 한 화면에서 연결하는 경험이 부족했다.

### 1.2 목표
AUAVWDS는 다음 목표를 갖는다.
1. 채팅 한 문장으로 날개 설계 파이프라인 실행
2. Airfoil / Wing 3D / Aerodynamics 탭 기반 단일 작업 흐름
3. 추정 해석과 정밀 해석을 구분 표시
4. 외부 설치 없이 배포 가능한 Windows EXE 제공

---

## 2. 시스템 범위 및 기능 정의

### 2.1 주요 사용자 시나리오
1. 사용자가 채팅으로 요구사항 입력 (예: 스팬, 목적, 비행 성향)
2. LLM이 필요한 도구 호출 (에어포일/날개/해석)
3. 탭에서 결과 확인
4. 필요 시 정밀 해석 또는 형상 변경 반복
5. 결과 저장/비교/내보내기

### 2.2 필수 기능
1. 에어포일 생성: NACA 4-digit + 커스텀 파라미터
2. 날개 파라미터 설정: 스팬/AR/스윕/테이퍼/상반각/트위스트
3. 3D 메시 빌드: three.js 렌더링용 메쉬 생성
4. 공력 해석:
   - Quick: NeuralFoil 호환 경로
   - Precision: OpenVSP + VSPAERO 스크립트 실행
5. 저장 기능: 스냅샷 저장, 불러오기, 비교
6. 내보내기: OBJ, JSON, (정밀해석 후) VSP3

---

## 3. 전체 아키텍처

### 3.1 런타임 구조

```text
[Electron Main]
  ├─ backend.exe spawn (windowsHide=true)
  ├─ health-check (/health)
  └─ IPC bridge
        │
        ▼
[React Renderer]
  ├─ Chat Panel
  ├─ Airfoil Tab
  ├─ Wing 3D Tab (three.js)
  └─ Aerodynamics Tab (ECharts)
        │
        ▼
[FastAPI Backend]
  ├─ /chat (LLM orchestrator + tool execution loop)
  ├─ /command, /state, /reset
  ├─ /saves, /saves/load, /saves/compare
  ├─ /export/cfd
  └─ Analysis Engines
       ├─ neuralfoil_adapter.py
       └─ openvsp_adapter.py
```

### 3.2 기술 스택
1. Desktop: Electron
2. Frontend: React + TypeScript + Vite
3. 3D: three.js
4. Chart: ECharts (`echarts-for-react`)
5. Backend: FastAPI + Pydantic
6. Packaging: PyInstaller + electron-builder(NSIS)

---

## 4. 공통 상태 모델(State Model)

시스템의 모든 변경은 `AppState`를 중심으로 동작한다.

```text
state.airfoil
  - coords / upper / lower / camber / summary
state.wing
  - params / preview_mesh / planform_2d
state.analysis
  - quick_result / precision_result / mode
state.history
  - undo snapshot stack
```

장점:
1. UI와 해석 파이프라인이 동일 상태를 참조
2. 저장/비교/복구 구현이 단순화
3. LLM 설명 단계에서 최신 상태 요약 전달 가능

---

## 5. LLM 오케스트레이션 및 도구 호출

### 5.1 지원 Provider
1. Gemini (Google)
2. OpenAI
3. Anthropic (Claude)
4. xAI (Grok)

### 5.2 도구 정의 (Tool Definitions)
LLM은 자연어를 직접 상태 변경하지 않고, 아래 명령 도구를 호출한다.

1. `SetAirfoil`
2. `SetWing`
3. `BuildWingMesh`
4. `RunQuickAnalysis`
5. `RunPrecisionAnalysis`
6. `Explain`
7. `Undo`
8. `Reset`

### 5.3 실행 루프
`/chat` 엔드포인트는 다음 루프로 동작한다.
1. LLM 응답 수신
2. tool_call 존재 시 `CommandEngine.command_from_tool` 변환
3. 명령 실행 후 결과를 모델에게 재전달
4. 최종 자연어 응답이 나오면 사용자에게 반환

이 구조로 “키워드 하드코딩 파싱” 없이 모델이 직접 도구 호출을 결정한다.

---

## 6. 형상 생성 파이프라인

### 6.1 Airfoil 생성
1. NACA 4-digit 생성 (`generate_naca4`)
2. 커스텀 파라미터 생성 (`generate_custom_airfoil`)
   - 최대 캠버(%)
   - 최대 캠버 위치(%c)
   - 두께(%)
   - reflex(%)

### 6.2 Wing 3D 생성
`build_wing_mesh`가 에어포일 단면 + 날개 파라미터로 메시를 생성한다.
생성 결과:
1. `preview_mesh.vertices`
2. `preview_mesh.triangles`
3. `planform_2d`

### 6.3 렌더링
Wing 탭은 three.js로 메시를 렌더링하며, 그리드/조명/카메라를 자동 배치한다.

---

## 7. 공력 해석 파이프라인

### 7.1 Quick Analysis (NeuralFoil 경로)
`neuralfoil_adapter.py`는 NeuralFoil import를 시도하고, 환경에 따라 surrogate를 사용한다.  
현재 기본 구현은 surrogate 기반 곡선 생성(`build_surrogate_curve`)을 사용하며 source label은 `추정(NeuralFoil)`로 표시된다.

기본 AoA 범위:
1. 시작: `-16 deg`
2. 종료: `20 deg`
3. 간격: `1 deg`

### 7.2 Precision Analysis (OpenVSP + VSPAERO)
`openvsp_adapter.py`는 `vsp.exe -script` 실행으로 해석을 수행한다.

동작 절차:
1. OpenVSP 해석 스크립트 생성
2. `VSPAEROSweep` 실행
3. stdout 테이블 파싱(CL/CD/CM/AoA)
4. 파생 지표 계산(L/D max, CL max, alpha0, Oswald e 등)

Solver 탐색 우선순위:
1. `AUAV_SOLVER_BIN_DIR`
2. `AUAV_RESOURCES_PATH/bin/win64`
3. `third_party/openvsp/win64`

실패 시 동작:
1. surrogate fallback
2. 실패 사유를 `extra_data.reason`에 기록
3. source label은 정밀해석 라벨 유지

---

## 8. UI/UX 설계

### 8.1 레이아웃
1. 상단 바: 앱 이름 + 탭 버튼
2. 본문 좌측: Chat Panel (리사이즈/접기 가능)
3. 본문 우측: 단일 활성 탭 캔버스

### 8.2 탭 구성
1. Airfoil: 단면 형상 + 요약 수치
2. Wing 3D: three.js 모델 + 파라미터 뱃지
3. Aerodynamics: 3개 그래프 + 핵심 지표 카드 + 해석 출처 배지

### 8.3 채팅 UX
1. 자동 스크롤 하단 고정
2. 전송 중 타이핑 인디케이터
3. Provider/Model/API 설정
4. Reset 버튼으로 상태 초기화

---

## 9. 데이터 관리 및 협업 기능

### 9.1 저장 히스토리
1. 현재 상태 저장
2. 저장 목록 조회
3. 특정 상태 불러오기
4. 상태 간 비교

### 9.2 CFD 내보내기
지원 포맷:
1. OBJ: 메시 기반 외부 툴 연계
2. JSON: 정점/삼각형 원시 데이터
3. VSP3: 정밀해석 수행 후 OpenVSP 파일 복사 내보내기

---

## 10. 배포/패키징 구조

### 10.1 백엔드 빌드
1. `PyInstaller`로 `backend.exe` 생성
2. 콘솔 숨김(`console=False`)

### 10.2 Electron 패키징
`electron-builder.yml` 기준 리소스 번들:
1. `resources/backend/*`
2. `resources/bin/win64/*` (OpenVSP/VSPAERO)
3. `resources/models/neuralfoil/*`

생성 산출물:
1. `release/win-unpacked`
2. `release/*Setup*.exe` (NSIS)

---

## 11. 검증 포인트

### 11.1 기능 검증
1. `/health` 성공 후 UI 로딩 여부
2. 자연어 입력 시 tool-calling 루프 정상 동작 여부
3. Quick/Precision 전환 시 source badge 일치 여부
4. 저장/불러오기/내보내기 정상 동작 여부

### 11.2 안정성 검증
1. 앱 종료 시 backend child process 종료
2. backend 포트 자동 할당 충돌 회피
3. Solver 부재 시 fallback 및 오류 메시지 기록

---

## 12. 한계 및 개선 로드맵

### 12.1 현재 한계
1. Quick 경로가 환경에 따라 surrogate 의존
2. Precision은 OpenVSP stdout 파싱 품질에 영향
3. 매우 극단적 형상 입력 시 시각/해석 안정성 저하 가능

### 12.2 개선 과제
1. NeuralFoil 런타임 번들/호환성 고도화
2. OpenVSP API 기반 직접 결과 추출 채널 추가
3. 메시 품질 검사(형상 sanity check) 자동화
4. 해석 조건(속도, 레이놀즈, 마하) 사용자 제어 고도화
5. 벤치마크 데이터셋 기반 정확도 리포트 자동 생성

---

## 13. 결론
AUAVWDS는 “초보자 중심 채팅 UX”와 “실제 공력 해석 파이프라인”을 결합한 독립 실행형 날개 설계 시스템으로 재구축되었다.  
현재 버전은 설계 생성-시각화-해석-저장-내보내기까지 End-to-End 경로를 제공하며, 추후 Quick 해석 고도화와 정밀해석 파싱 품질 개선을 통해 실사용 신뢰도를 더 높일 수 있다.

---

## 부록 A. 프로젝트 구조 (요약)

```text
AUAVWDS/
  electron/
    main.cjs
    preload.cjs
  src/
    App.tsx
    tabs/
      AirfoilTab.tsx
      Wing3DTab.tsx
      AerodynamicsTab.tsx
    lib/api.ts
    styles/app.css
  backend/
    main.py
    backend.spec
    app/
      api.py
      models/state.py
      services/
        command_engine.py
        llm_chat.py
        state_store.py
      analysis/
        naca.py
        neuralfoil_adapter.py
        openvsp_adapter.py
      geometry/
        wing_builder.py
  third_party/
    openvsp/win64/
    neuralfoil/
```

## 부록 B. 핵심 API 엔드포인트

1. `GET /health`
2. `GET /state`
3. `POST /reset`
4. `POST /chat`
5. `POST /command`
6. `POST /llm/discover`
7. `GET /saves`
8. `POST /saves`
9. `POST /saves/load`
10. `POST /saves/compare`
11. `POST /export/cfd`

