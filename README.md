# AUAVWDS

AUAVWDS는 Windows 10/11용 Electron + FastAPI 기반 날개 설계 앱입니다.  
채팅을 중심으로 에어포일 설정, 날개 형상 생성, 공력 해석, 저장/비교, 내보내기까지 한 흐름으로 수행할 수 있습니다.

## 핵심 특징
- 채팅 중심 설계 워크플로우
  - 좌측 채팅 패널에서 설계를 요청하고, 우측 탭에서 결과를 바로 확인합니다.
- 멀티 solver 공력 해석
  - `OpenVSP/VSPAERO`: 실제 wing solver 기반 정밀 해석
  - `NeuralFoil`: 2D airfoil polar + 명시적 finite-wing correction 기반 날개 추정 해석
- 공통 해석 조건 편집
  - AoA 시작/종료/간격, Mach, Reynolds를 UI와 상태에서 함께 관리합니다.
- 저장 히스토리
  - 현재 상태 저장, 불러오기, 두 스냅샷 비교를 지원합니다.
- CFD/연구용 내보내기
  - `OBJ`, `JSON`은 공통 지원
  - `VSP3`는 실제 `OpenVSP` 결과가 있을 때만 지원
- 브라우저 개발/테스트 루트
  - Electron을 띄우지 않고도 Vite + FastAPI 조합으로 UI를 브라우저에서 테스트할 수 있습니다.

## 현재 해석 구조

### 1. OpenVSP / VSPAERO
- 실제 OpenVSP 스크립트를 생성해 `vsp.exe -script`로 실행합니다.
- solver 실행 결과는 `analysis.results.openvsp`에 저장됩니다.
- `VSP3` 파일은 이 경로에서만 생성됩니다.
- solver provenance, 산출물 경로, fallback 사유를 메타데이터에 남깁니다.

### 2. NeuralFoil
- airfoil 좌표를 직접 사용해 NeuralFoil 2D 해석을 수행합니다.
- 이후 backend에서 finite-wing correction을 적용해 wing-level estimate를 만듭니다.
- 결과는 `analysis.results.neuralfoil`에 저장됩니다.
- 중요:
  - NeuralFoil 결과는 OpenVSP와 같은 물리 수준의 3D solver 결과로 가장하지 않습니다.
  - `VSP3` export는 제공하지 않습니다.

### 3. 결과 표시와 provenance
- 활성 solver는 `analysis.active_solver`로 관리됩니다.
- UI에서 `OpenVSP`와 `NeuralFoil` 결과를 전환해서 볼 수 있습니다.
- 모든 결과는 `analysis_mode`, `fallback_reason`, `source_label`을 포함합니다.
  - `openvsp`
  - `neuralfoil`
  - `fallback`

## 지원 명령
backend 명령 엔진은 현재 아래 명령을 지원합니다.

- `SetAirfoil`
- `SetWing`
- `BuildWingMesh`
- `SetAnalysisConditions`
- `SetActiveSolver`
- `RunOpenVspAnalysis`
- `RunNeuralFoilAnalysis`
- `RunPrecisionAnalysis`
  - 호환성용 alias이며 내부적으로 `RunOpenVspAnalysis`로 매핑됩니다.
- `Explain`
- `Undo`
- `Reset`

## 설치(권장)
- 일반 사용자는 소스코드 ZIP보다 GitHub `Releases`의 설치 파일을 사용하는 편이 편합니다.
- 최신 릴리즈: [AUAVWDS Releases](https://github.com/djkim0320/AUAVWDS/releases/latest)

## 소스코드 개발 실행

### 1. Node 의존성 설치
```bash
npm install
```

### 2. Python 의존성 설치
```bash
python -m pip install -r backend/requirements.txt
```

### 3. Electron 개발 실행
```bash
npm run dev
```

## 브라우저 개발/테스트 실행
이 모드는 로컬 개발과 Playwright 테스트용입니다. Electron 패키징을 대체하지 않습니다.

```bash
npm run dev:web
```

브라우저에서 아래 주소로 접속합니다.

```text
http://127.0.0.1:5173
```

설명:
- renderer는 Vite dev server에서 동작합니다.
- backend는 `AUAV_ENABLE_WEB_BRIDGE=1`로 실행됩니다.
- frontend는 HTTP bridge를 통해 `/api/*`로 backend를 호출합니다.
- 저장/불러오기/비교/내보내기 데이터는 Electron 모드와 같은 backend 작업 디렉터리를 사용합니다.

## 빌드 / 패키징

### 렌더러 빌드
```bash
npm run build
```

### backend.exe 빌드(PyInstaller)
```bash
npm run backend:build
```

### 언팩 버전 생성
```bash
npm run pack:unpacked
```

### 설치 파일 생성(NSIS)
```bash
npm run dist:setup
```

### 언팩 + 설치 파일 동시 생성
```bash
npm run release:all
```

## OpenVSP 실행 파일 탐색 순서
`backend/app/analysis/openvsp_adapter.py` 기준으로 아래 순서대로 solver 실행 파일을 찾습니다.

1. `AUAV_SOLVER_BIN_DIR`
2. `AUAV_RESOURCES_PATH/bin/win64`
3. `third_party/openvsp/win64`

패키징된 앱에서는 `electron-builder.yml`의 `extraResources`를 통해 아래 경로로 포함됩니다.
- `resources/backend/backend.exe`
- `resources/bin/win64/*`

## 주요 개발 스크립트
- `npm run dev`
- `npm run dev:web`
- `npm run backend:dev`
- `npm run backend:dev:web`
- `npm run build`
- `npm run backend:build`
- `npm run pack:unpacked`
- `npm run dist:setup`

## 주요 API
- `/health`
- `/state`
- `/reset`
- `/chat`
- `/command`
- `/llm/discover`
- `/saves`
- `/saves/load`
- `/saves/compare`
- `/export/cfd`

## 프로젝트 구조
```text
AUAVWDS/
  electron/
    main.cjs
    preload.cjs
  src/
    App.tsx
    lib/api.ts
    tabs/
      AirfoilTab.tsx
      Wing3DTab.tsx
      AerodynamicsTab.tsx
    components/
      SourceBadge.tsx
    styles/
      app.css
    types.ts
  backend/
    main.py
    backend.spec
    requirements.txt
    app/
      api.py
      models/
        state.py
      services/
        command_engine.py
        llm_chat.py
        state_store.py
      analysis/
        common.py
        naca.py
        openvsp_adapter.py
        neuralfoil_adapter.py
      geometry/
        wing_builder.py
  third_party/
    openvsp/win64/
  scripts/
    build_backend.ps1
```

## 검증에 유용한 명령
```bash
python -m compileall backend
python -m unittest discover -s backend/tests -v
npm run build
```

## 현재 범위에서 알아둘 점
- OpenVSP는 여전히 가장 신뢰도가 높은 wing solver 경로입니다.
- NeuralFoil은 연구/비교에 유용한 1급 solver 결과로 저장되지만, 물리적으로는 `2D 기반 날개 추정 해석`입니다.
- fallback은 숨겨지지 않으며, UI와 메타데이터 모두에 이유가 남습니다.
