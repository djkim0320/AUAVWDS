# PROJECT GUIDE

이 문서는 AUAVWDS를 다른 세션/에이전트가 바로 이어서 작업할 수 있도록 현재 구조와 동작 원리를 정리한 문서입니다.

## 1) 런타임 구조
- Electron main(`electron/main.cjs`)가 Python backend를 child process로 실행
- backend는 localhost FastAPI 서버로 동작
- preload(`electron/preload.cjs`)가 renderer에 IPC bridge 노출
- renderer(`src/App.tsx`)는 bridge를 통해 상태/채팅/명령/저장/내보내기 호출
- 상태 읽기 경로는 두 단계
  - `/state/client`: 일반 UI 갱신용 summary state
  - `/state`: `Wing 3D` / `Aerodynamics` 탭에서만 추가 hydrate하는 canonical full state

## 2) 상태 모델
`backend/app/models/state.py`의 `AppState`가 단일 상태 원본이다.
- `state.airfoil`
- `state.wing`
- `state.analysis`
- `state.history`

summary state에서는 아래 무거운 필드를 비운다.
- airfoil 좌표 배열
- wing preview mesh
- 2D planform
- solver curve 배열
- history

## 3) 명령 엔진
`backend/app/services/command_engine.py`
- 모든 상태 변경은 command 단위로 처리
- alias 정규화 + payload 검증은 prepare 단계에서 한 번만 수행
- 지원 명령:
  - `SetAirfoil`
  - `SetWing`
  - `BuildWingMesh`
  - `SetAnalysisConditions`
  - `SetActiveSolver`
  - `RunOpenVspAnalysis`
  - `RunNeuralFoilAnalysis`
  - `RunPrecisionAnalysis` (`RunOpenVspAnalysis` alias)
  - `Explain`
  - `Undo`
  - `Reset`
- undo history는 실제 상태가 변한 명령에서만 추가된다.
- `SetAirfoil`, `SetWing`는 geometry 변경 시 mesh/planform을 invalidate한다.
- `SetAnalysisConditions`는 해석 조건 변경 시 solver 결과를 invalidate한다.

## 4) LLM 오케스트레이션
`backend/app/services/llm_chat.py`
- provider별 API 호출(gemini/openai-like/anthropic)
- function/tool calling 결과를 명령 엔진에 전달
- 키워드 하드코딩 파서는 쓰지 않음
- tool turn에 전달하는 상태는 full state가 아니라 compact summary다
- summary에는 `recommended_reynolds` 힌트가 포함될 수 있다
  - 조건: 사용자가 Reynolds를 지정하지 않았고, Mach + 대표 chord로 추정 가능할 때
  - 목적: AI가 해석 전에 `SetAnalysisConditions`로 Reynolds를 채운 뒤 solver를 실행하도록 유도

## 5) OpenVSP / VSPAERO
`backend/app/analysis/openvsp_adapter.py`
- `vsp.exe -script` 기반으로 VSPAERO sweep 수행
- solver 탐색 순서:
  1. `AUAV_SOLVER_BIN_DIR`
  2. `AUAV_RESOURCES_PATH/bin/win64`
  3. `third_party/openvsp/win64`
- `.polar`에서 surface 계열과 wake/far-field 계열을 모두 파싱
- 물리적 일관성과 연속 유효 구간을 기준으로 주 coefficient family를 동적으로 선택
- `ReCref`를 실제 solver 입력으로 넣고, 결과 메타데이터에 solver-effective Reynolds를 기록
- raw script/log/stdout/stderr/polar/vsp3 provenance는 유지
- comparison readiness / blockers / comparison window도 backend 메타데이터로 계산
  - 현재 renderer는 전용 comparison 카드를 크게 노출하지 않지만, 데이터는 provenance에 남는다

## 6) NeuralFoil
`backend/app/analysis/neuralfoil_adapter.py`
- airfoil 좌표 기반 2D 해석 후 finite-wing correction으로 wing estimate 생성
- 결과는 `analysis.results.neuralfoil`
- Reynolds가 비어 있으면 Mach와 대표 chord로 내부 추정값 사용
- OpenVSP와 동일한 물리 모델이 아니므로 직접 비교는 solver-effective 조건과 유효 AoA overlap이 맞을 때만 신중하게 해석해야 한다

## 7) API 계약
`backend/app/api.py`
- `/health`, `/state`, `/state/client`, `/reset`
- `/chat`, `/command`, `/llm/discover`
- `/saves`, `/saves/load`, `/saves/compare`
- `/export/cfd`

주의:
- `/command`, `/chat`, `/reset`, `/saves/load`는 summary state를 반환
- `/state`만 full canonical state
- `export/cfd`는 사용자 지정 경로를 받지 않고, 항상 앱 작업 디렉터리의 `exports/` 아래에 생성

## 8) 저장/스냅샷
`backend/app/services/state_store.py`
- snapshot JSON이 canonical source
- `.meta.json` sidecar + in-process cache로 save list 비용을 줄임
- load/compare는 full snapshot 기준으로 동작

## 9) renderer bridge surface
현재 renderer가 쓰는 bridge surface:
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

현재 renderer에서 쓰지 않는 것:
- backend-ready 이벤트 surface
- `discoverModels`

## 10) 패키징
- `electron-builder.yml` 기준
- extraResources:
  - `backend/dist` -> `resources/backend`
  - `third_party/openvsp/win64` -> `resources/bin/win64`
  - `third_party/neuralfoil` -> `resources/models/neuralfoil`

주요 스크립트:
- `npm run dev`
- `npm run dev:web`
- `npm run backend:build`
- `npm run pack:unpacked`
- `npm run dist:setup`
- `npm run release:all`

## 11) 최근 문서 반영 사항
- summary state와 full state 계약 분리
- OpenVSP coefficient family 동적 선택
- solver-effective Reynolds / `ReCref` 기록
- AI Reynolds 추론 힌트(`recommended_reynolds`)
- export 경로 계약 정리
- no-op history 방지 및 invalidation 흐름 반영
