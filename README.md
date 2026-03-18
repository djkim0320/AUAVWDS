# AUAVWDS

AUAVWDS는 Windows 10/11용 Electron + FastAPI 기반 날개 설계 앱입니다.
채팅 중심 워크플로우로 에어포일 선택, 날개 형상 생성, 3D 미리보기, 공력 해석, 저장/비교, 내보내기를 한 앱 안에서 처리합니다.

## 핵심 기능
- 채팅 기반 설계
  - 자연어로 span, 목적, 형상 선호를 설명하면 `SetAirfoil`, `SetWing`, `BuildWingMesh` 흐름으로 상태를 구성합니다.
- 3D 미리보기
  - backend가 생성한 preview mesh를 `Wing 3D` 탭에서 표시합니다.
  - 축척 바는 카메라 줌에 따라 동적으로 바뀝니다.
- 두 해석 경로 지원
  - `OpenVSP/VSPAERO`: 실제 wing solver 기반 정밀 해석
  - `NeuralFoil`: 2D airfoil polar + finite-wing correction 기반 빠른 wing estimate
- 공통 해석 조건 관리
  - AoA 시작/종료/간격, Mach, Reynolds를 공통 조건으로 사용합니다.
  - 사용자가 Reynolds를 비워 둔 상태에서 AI에게 해석을 요청하면, 현재 Mach와 대표 chord를 바탕으로 AI가 먼저 Reynolds를 추정해 `SetAnalysisConditions`에 넣도록 유도합니다.
- 저장/비교
  - 현재 상태 저장, 불러오기, 스냅샷 비교를 지원합니다.
- 내보내기
  - `OBJ`, `JSON` 지원
  - `VSP3`는 실제 OpenVSP 결과가 있을 때만 지원
  - export 파일은 항상 앱 작업 디렉터리의 `exports/` 아래에 생성됩니다.

## Solver 의미와 비교 주의점

### OpenVSP / VSPAERO
- `vsp.exe -script` 기반으로 VSPAERO sweep을 수행합니다.
- `.polar`의 surface 계열과 wake/far-field 계열을 모두 읽고, 물리적 일관성과 유효 AoA 구간을 기준으로 주 계수 계열을 동적으로 선택합니다.
- solver provenance, raw script/stdout/stderr/polar/vsp3, solver-effective 조건을 유지합니다.
- UI Reynolds가 지원되는 경우 `ReCref` 입력으로 실제 solver에 반영합니다.

### NeuralFoil
- airfoil 좌표를 기반으로 2D 해석을 수행한 뒤 finite-wing correction으로 wing-level estimate를 만듭니다.
- OpenVSP와 같은 3D wing solver는 아니므로, 두 결과를 물리적으로 동일한 solver 결과처럼 해석하면 안 됩니다.
- Reynolds가 비어 있으면 Mach와 대표 chord로 내부 추정치를 사용합니다.

### 결과 비교 원칙
- 앱은 solver별 결과를 각각 표시합니다.
- backend는 요청 조건, solver-effective 조건, valid AoA range, reference values를 provenance로 남깁니다.
- 직접 비교는 solver-effective 조건과 유효 AoA overlap이 맞을 때만 신뢰할 수 있습니다.

## 상태와 런타임 구조
- `/state`
  - canonical full backend state
- `/state/client`
  - 일반 UI 갱신용 lightweight summary state
- Electron/web bridge는 기본적으로 summary state를 사용합니다.
- `Wing 3D`, `Aerodynamics`처럼 mesh/curve가 필요한 화면만 full state를 추가 hydrate합니다.

## 지원 명령
- `SetAirfoil`
- `SetWing`
- `BuildWingMesh`
- `SetAnalysisConditions`
- `SetActiveSolver`
- `RunOpenVspAnalysis`
- `RunNeuralFoilAnalysis`
- `RunPrecisionAnalysis`
  - 호환성 alias이며 내부적으로 `RunOpenVspAnalysis`로 정규화됩니다.
- `Explain`
- `Undo`
- `Reset`

## 설치
- 일반 사용자는 GitHub Releases의 설치 파일 사용을 권장합니다.
- 최신 릴리즈: [AUAVWDS Releases](https://github.com/djkim0320/AUAVWDS/releases/latest)

## 개발 실행

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

### 4. 브라우저 개발 실행
Electron 대신 Vite + FastAPI 조합으로 개발할 때 사용합니다.

```bash
npm run dev:web
```

접속 주소:

```text
http://127.0.0.1:5173
```

설명:
- renderer는 Vite dev server에서 동작합니다.
- backend는 `AUAV_ENABLE_WEB_BRIDGE=1`로 실행됩니다.
- frontend는 `/api/*` HTTP bridge를 통해 backend를 호출합니다.

## 빌드 / 패키징

### renderer 빌드
```bash
npm run build
```

### backend.exe 빌드
```bash
npm run backend:build
```

### 언팩 패키지 생성
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
`backend/app/analysis/openvsp_adapter.py` 기준으로 아래 순서로 solver 바이너리를 찾습니다.

1. `AUAV_SOLVER_BIN_DIR`
2. `AUAV_RESOURCES_PATH/bin/win64`
3. `third_party/openvsp/win64`

패키징된 앱에서는 `electron-builder.yml`의 `extraResources`를 통해 아래 경로가 포함됩니다.
- `resources/backend/backend.exe`
- `resources/bin/win64/*`

## 주요 API
- `/health`
- `/state`
- `/state/client`
- `/reset`
- `/chat`
- `/command`
- `/saves`
- `/saves/load`
- `/saves/compare`
- `/export/cfd`

참고:
- `/command`, `/chat`, `/reset`, `/saves/load`는 summary state를 반환합니다.
- `/llm/discover`는 backend utility endpoint로 남아 있지만 현재 renderer 기본 bridge surface에서는 사용하지 않습니다.

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
        command_specs.py
        llm_chat.py
        state_store.py
        state_summary.py
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
