# PROJECT GUIDE

이 문서는 AUAVWDS를 다른 세션/에이전트가 바로 이어서 작업할 수 있도록 구조와 동작 원리를 정리한 문서입니다.

## 1) 런타임 구조
- Electron main(`electron/main.cjs`)가 Python backend를 child process로 실행
- backend는 localhost FastAPI 서버로 동작
- preload(`electron/preload.cjs`)가 renderer에 IPC bridge 노출
- renderer(`src/App.tsx`)는 bridge를 통해 상태/채팅/명령/저장/내보내기 호출
- 상태 읽기 경로는 두 단계로 나뉨
  - `/state/client`: 일반 UI 갱신용 summary state
  - `/state`: `Wing 3D` / `Aerodynamics` 탭에서만 추가 hydrate하는 canonical full state

## 2) 상태 모델
`backend/app/models/state.py`의 `AppState`가 단일 상태 원본
- `state.airfoil`
- `state.wing`
- `state.analysis`
- `state.history` (undo 스냅샷)

요약 상태에서는 아래 큰 필드를 비워서 전달한다.
- airfoil 좌표 배열
- wing preview mesh
- 2D planform
- solver curve 배열

## 3) 명령 엔진
`backend/app/services/command_engine.py`
- 모든 상태 변경은 Command 단위로 처리
- alias 정규화 + payload 검증은 prepare 단계에서 한 번만 수행
- 지원 명령:
  - `SetAirfoil`
  - `SetWing`
  - `BuildWingMesh`
  - `SetAnalysisConditions`
  - `SetActiveSolver`
  - `RunOpenVspAnalysis`
  - `RunNeuralFoilAnalysis`
  - `RunPrecisionAnalysis` (호환성 alias, 내부적으로 `RunOpenVspAnalysis`)
  - `Explain`
  - `Undo`
  - `Reset`
- undo history는 실제 상태가 변한 명령에서만 추가된다. 반복 no-op 명령은 history를 오염시키지 않는다.

## 4) LLM 오케스트레이션
`backend/app/services/llm_chat.py`
- provider별 API 호출(gemini/openai-like/anthropic)
- function/tool calling 결과를 명령 엔진에 전달
- 키워드 하드코딩 파서는 사용하지 않음
- tool turn에 전달하는 상태는 full state가 아니라 compact summary다.

## 5) 정밀해석(OpenVSP)
`backend/app/analysis/openvsp_adapter.py`
- `vsp.exe -script` 기반으로 VSPAERO sweep 수행
- solver 탐색 순서:
  1. `AUAV_SOLVER_BIN_DIR`
  2. `AUAV_RESOURCES_PATH/bin/win64`
  3. `third_party/openvsp/win64`
- 실행 실패 시 surrogate fallback + 실패 사유 기록
- raw script/log/stdout/stderr/polar/vsp3 provenance는 유지한다.

## 6) 주요 API
`backend/app/api.py`
- `/health`, `/state`, `/state/client`, `/reset`
- `/chat`, `/command`, `/llm/discover`
- `/saves`, `/saves/load`, `/saves/compare`
- `/export/cfd` (OBJ/JSON/VSP3)

주의:
- `/command`, `/chat`, `/reset`, `/saves/load`는 모두 summary state를 반환한다.
- `export/cfd`는 사용자 지정 경로를 받지 않고, 항상 앱 작업 디렉터리의 `exports/` 아래에 생성한다.

## 7) 패키징
- `electron-builder.yml` 기준
- extraResources:
  - `backend/dist` -> `resources/backend`
  - `third_party/openvsp/win64` -> `resources/bin/win64`
  - `third_party/neuralfoil` -> `resources/models/neuralfoil`

## 8) 현재 renderer bridge surface
- `getState`
- `getFullState`
- `chat`
- `command`
- `reset`
- `listSaves`
- `saveSnapshot`
- `loadSnapshot`
- `compareSnapshots`
- `exportCfd`

`discoverModels`와 backend-ready 이벤트 surface는 현재 renderer에서 쓰지 않는다.
