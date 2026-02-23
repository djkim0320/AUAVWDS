# PROJECT GUIDE

이 문서는 AUAVWDS를 다른 세션/에이전트가 바로 이어서 작업할 수 있도록 구조와 동작 원리를 정리한 문서입니다.

## 1) 런타임 구조
- Electron main(`electron/main.cjs`)가 Python backend를 child process로 실행
- backend는 localhost FastAPI 서버로 동작
- preload(`electron/preload.cjs`)가 renderer에 IPC bridge 노출
- renderer(`src/App.tsx`)는 bridge를 통해 상태/채팅/명령/저장/내보내기 호출

## 2) 상태 모델
`backend/app/models/state.py`의 `AppState`가 단일 상태 원본
- `state.airfoil`
- `state.wing`
- `state.analysis`
- `state.history` (undo 스냅샷)

## 3) 명령 엔진
`backend/app/services/command_engine.py`
- 모든 상태 변경은 Command 단위로 처리
- 지원 명령: `SetAirfoil`, `SetWing`, `BuildWingMesh`, `RunQuickAnalysis`, `RunPrecisionAnalysis`, `Explain`, `Undo`, `Reset`

## 4) LLM 오케스트레이션
`backend/app/services/llm_chat.py`
- provider별 API 호출(gemini/openai-like/anthropic)
- function/tool calling 결과를 명령 엔진에 전달
- 키워드 하드코딩 파서는 사용하지 않음

## 5) 정밀해석(OpenVSP)
`backend/app/analysis/openvsp_adapter.py`
- `vsp.exe -script` 기반으로 VSPAERO sweep 수행
- solver 탐색 순서:
  1. `AUAV_SOLVER_BIN_DIR`
  2. `AUAV_RESOURCES_PATH/bin/win64`
  3. `third_party/openvsp/win64`
- 실행 실패 시 surrogate fallback + 실패 사유 기록

## 6) 주요 API
`backend/app/api.py`
- `/health`, `/state`, `/reset`
- `/chat`, `/command`, `/llm/discover`
- `/saves`, `/saves/load`, `/saves/compare`
- `/export/cfd` (OBJ/JSON/VSP3)

## 7) 패키징
- `electron-builder.yml` 기준
- extraResources:
  - `backend/dist` -> `resources/backend`
  - `third_party/openvsp/win64` -> `resources/bin/win64`
  - `third_party/neuralfoil` -> `resources/models/neuralfoil`

## 8) 권장 다음 작업
1. OpenVSP 스크립트에서 에어포일 단면 반영 로직 강화
2. VSPAERO 결과 파싱 정밀화(ResultsVec 직접 추출 포함)
3. Aerodynamics 탭에 solver 로그/실행 모드 표시
4. 대용량 번들 최적화(code-splitting, 리소스 분리)
