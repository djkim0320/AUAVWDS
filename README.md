# AUAVWDS

Windows 10/11 전용 Electron + FastAPI 기반 날개 설계 앱입니다.

## 설치(권장)
- 일반 사용자는 소스코드 ZIP이 아니라 GitHub `Releases`의 설치 파일(`AUAVWDS Setup *.exe`)로 설치하세요.
- 다운로드: `https://github.com/djkim0320/AUAVWDS/releases/latest`
- 소스코드는 개발/수정 목적일 때만 사용하세요.

## 핵심 기능
- Chat-first UI: 좌측 채팅 + 우측 단일 탭 캔버스(Airfoil / Wing 3D / Aerodynamics)
- LLM tool-calling 명령 파이프라인
  - `SetAirfoil`
  - `SetWing`
  - `BuildWingMesh`
  - `RunPrecisionAnalysis` (OpenVSP+VSPAERO 단일 해석)
  - `Explain`, `Undo`, `Reset`
- 저장 히스토리: 저장 / 불러오기 / 비교
- CFD 내보내기: OBJ / JSON / (정밀해석 후) VSP3

## 정밀해석 통합 방식
- `backend/app/analysis/openvsp_adapter.py`에서 `vsp.exe -script`로 실제 VSPAERO sweep를 수행합니다.
- OpenVSP 실행 파일 탐색 순서:
  1. `AUAV_SOLVER_BIN_DIR`
  2. `AUAV_RESOURCES_PATH/bin/win64`
  3. `third_party/openvsp/win64`
- 실행 실패 시에는 이유를 `extra_data.reason`에 남기고 내부 fallback(근사 곡선)으로 동작합니다.

## 개발 실행
1. Node 의존성 설치
```bash
npm install
```

2. Python 의존성 설치
```bash
python -m pip install -r backend/requirements.txt
```

3. 개발 실행
```bash
npm run dev
```

## 빌드/패키징
- 렌더러 빌드
```bash
npm run build
```

- backend.exe 빌드(PyInstaller)
```bash
npm run backend:build
```

- release 폴더에 unpacked 생성
```bash
npm run pack:unpacked
```

- setup(NSIS) 생성
```bash
npm run dist:setup
```

- unpacked + setup 동시 생성
```bash
npm run release:all
```

## 리소스 번들 경로(electron-builder)
- `resources/backend/backend.exe`
- `resources/bin/win64/*` (OpenVSP/VSPAERO)

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
    styles/app.css
    types.ts
  backend/
    main.py
    backend.spec
    requirements.txt
    app/
      api.py
      models/state.py
      services/
        command_engine.py
        llm_chat.py
        state_store.py
      analysis/
        naca.py
        common.py
        openvsp_adapter.py
      geometry/
        wing_builder.py
  third_party/
    openvsp/win64/
  scripts/
    build_backend.ps1
```
