import { useEffect, useMemo, useRef, useState } from 'react';
import { bridge } from './lib/api';
import AirfoilTab from './tabs/AirfoilTab';
import Wing3DTab from './tabs/Wing3DTab';
import AerodynamicsTab from './tabs/AerodynamicsTab';
import type {
  AppState,
  ProviderId,
  SaveSnapshotCompareResponse,
  SaveSnapshotRecord,
} from './types';

type TabId = 'airfoil' | 'wing3d' | 'aero';

type ChatMessage = {
  role: 'user' | 'assistant';
  content: string;
};

type ProviderConfig = {
  baseUrl: string;
  apiKey: string;
};

type ModelCard = {
  id: string;
  provider: ProviderId;
  title: string;
  subtitle: string;
  description: string;
};

const LS_CHAT_COLLAPSED = 'auav.chat.collapsed';
const LS_CHAT_WIDTH = 'auav.chat.width';
const LS_PROVIDER = 'auav.provider';
const LS_MODEL = 'auav.model';
const LS_API_KEY_PREFIX = 'auav.apiKey.';
const LS_BASE_URL_PREFIX = 'auav.baseUrl.';

const PROVIDER_META: Record<ProviderId, { label: string; defaultBase: string; mark: string }> = {
  gemini: { label: 'Google (Gemini)', defaultBase: 'https://generativelanguage.googleapis.com', mark: '✦' },
  openai: { label: 'OpenAI (ChatGPT)', defaultBase: 'https://api.openai.com/v1', mark: '◎' },
  anthropic: { label: 'Anthropic (Claude)', defaultBase: 'https://api.anthropic.com', mark: '◉' },
  grok: { label: 'xAI (Grok)', defaultBase: 'https://api.x.ai/v1', mark: '✕' },
};

const MODEL_CATALOG: ModelCard[] = [
  {
    id: 'gemini-3.1-pro-preview',
    provider: 'gemini',
    title: 'Gemini 3.1 Pro Preview',
    subtitle: 'gemini-3.1-pro-preview',
    description: '현재 Gemini 계열의 최신 상위 추론 모델로, 복잡한 설계 해석과 긴 컨텍스트 작업에 적합합니다.',
  },
  {
    id: 'gemini-3.1-pro-preview-customtools',
    provider: 'gemini',
    title: 'Gemini 3.1 Pro Custom Tools',
    subtitle: 'gemini-3.1-pro-preview-customtools',
    description: '함수 호출과 에이전트형 워크플로우를 더 강하게 쓰는 경우에 맞춘 Gemini 3.1 Pro 변형입니다.',
  },
  {
    id: 'gemini-3-flash-preview',
    provider: 'gemini',
    title: 'Gemini 3 Flash',
    subtitle: 'gemini-3-flash-preview',
    description: '최신 Gemini 3 계열의 고속 모델로, 빠른 반복 대화와 실시간 설계 탐색에 적합합니다.',
  },
  {
    id: 'gemini-3.1-flash-lite-preview',
    provider: 'gemini',
    title: 'Gemini 3.1 Flash-Lite',
    subtitle: 'gemini-3.1-flash-lite-preview',
    description: '아주 가벼운 최신 Gemini 3.1 계열로, 대량 요청과 짧은 응답 위주 작업에 유리합니다.',
  },
  {
    id: 'gemini-2.5-flash',
    provider: 'gemini',
    title: 'Gemini 2.5 Flash',
    subtitle: 'gemini-2.5-flash',
    description: '안정성이 검증된 현재 세대의 균형형 모델로, 일반 대화와 툴 호출 균형이 좋습니다.',
  },
  {
    id: 'gemini-2.5-flash-lite',
    provider: 'gemini',
    title: 'Gemini 2.5 Flash-Lite',
    subtitle: 'gemini-2.5-flash-lite',
    description: '저비용 고속 응답이 필요한 경우에 유리한 안정형 경량 모델입니다.',
  },
  {
    id: 'gemini-2.5-pro',
    provider: 'gemini',
    title: 'Gemini 2.5 Pro',
    subtitle: 'gemini-2.5-pro',
    description: '프리뷰보다 안정적인 고성능 선택지가 필요할 때 쓰기 좋은 2.5 상위 모델입니다.',
  },
  {
    id: 'gpt-5.4',
    provider: 'openai',
    title: 'GPT-5.4',
    subtitle: 'gpt-5.4',
    description: 'OpenAI의 최신 전문 작업용 상위 모델로, 가장 높은 정확도와 긴 문맥 처리에 유리합니다.',
  },
  {
    id: 'gpt-5.2',
    provider: 'openai',
    title: 'GPT-5.2',
    subtitle: 'gpt-5.2',
    description: '현재 OpenAI의 주력 코딩·에이전트형 범용 모델로, 설계 대화와 툴 호출 모두에 잘 맞습니다.',
  },
  {
    id: 'gpt-5.3-codex',
    provider: 'openai',
    title: 'GPT-5.3 Codex',
    subtitle: 'gpt-5.3-codex',
    description: '현재 OpenAI 계열의 최신 코딩 특화 모델로, 도구 호출과 긴 코드 작업에 적합합니다.',
  },
  {
    id: 'gpt-5-mini',
    provider: 'openai',
    title: 'GPT-5 mini',
    subtitle: 'gpt-5-mini',
    description: '잘 정의된 반복 작업과 빠른 응답이 중요한 경우에 적합한 경량 GPT-5 모델입니다.',
  },
  {
    id: 'gpt-5-nano',
    provider: 'openai',
    title: 'GPT-5 nano',
    subtitle: 'gpt-5-nano',
    description: '가장 빠르고 저렴한 GPT-5 계열 모델로, 분류나 짧은 응답 위주 작업에 적합합니다.',
  },
  {
    id: 'claude-sonnet-4-6',
    provider: 'anthropic',
    title: 'Claude Sonnet 4.6',
    subtitle: 'claude-sonnet-4-6',
    description: '현재 Claude 계열의 균형형 최신 모델로, 속도와 추론 품질을 함께 챙기기 좋습니다.',
  },
  {
    id: 'claude-opus-4-6',
    provider: 'anthropic',
    title: 'Claude Opus 4.6',
    subtitle: 'claude-opus-4-6',
    description: '현재 Claude의 최고급 모델로, 가장 어려운 추론과 장문 설계 설명에 적합합니다.',
  },
  {
    id: 'claude-haiku-4-5',
    provider: 'anthropic',
    title: 'Claude Haiku 4.5',
    subtitle: 'claude-haiku-4-5',
    description: '최신 Claude 경량 라인으로, 빠른 응답과 낮은 비용이 중요한 대화에 유리합니다.',
  },
  {
    id: 'grok-4-1-fast-reasoning',
    provider: 'grok',
    title: 'Grok 4.1 Fast Reasoning',
    subtitle: 'grok-4-1-fast-reasoning',
    description: '최신 xAI 추론형 모델로, 빠른 응답과 reasoning 중심 작업에 가장 잘 맞습니다.',
  },
  {
    id: 'grok-4',
    provider: 'grok',
    title: 'Grok 4',
    subtitle: 'grok-4',
    description: '최신 Grok 상위 일반 모델로, 폭넓은 추론과 멀티모달 이해에 적합합니다.',
  },
  {
    id: 'grok-code-fast-1',
    provider: 'grok',
    title: 'Grok Code Fast 1',
    subtitle: 'grok-code-fast-1',
    description: '최신 xAI 코딩 특화 모델로, 코드 작성과 도구 호출이 많은 에이전트형 흐름에 맞춰져 있습니다.',
  },
];

const DEFAULT_MODEL_BY_PROVIDER: Record<ProviderId, string> = {
  gemini: 'gemini-3.1-pro-preview',
  openai: 'gpt-5.4',
  anthropic: 'claude-sonnet-4-6',
  grok: 'grok-4-1-fast-reasoning',
};

function defaultState(): AppState {
  return {
    airfoil: {
      coords: [],
      upper: [],
      lower: [],
      camber: [],
      summary: { code: '', thickness_percent: 0, max_camber_percent: 0, max_camber_x_percent: 0 },
    },
    wing: {
      params: { span_m: 1, aspect_ratio: 8, sweep_deg: 0, taper_ratio: 1, dihedral_deg: 5, twist_deg: 0 },
      preview_mesh: null,
      planform_2d: null,
    },
    analysis: { precision_result: null, mode: 'precision' },
    history: [],
  };
}

function findModelById(modelId: string): ModelCard | undefined {
  return MODEL_CATALOG.find((m) => m.id === modelId);
}

function firstModelForProvider(provider: ProviderId): string {
  const preferred = findModelById(DEFAULT_MODEL_BY_PROVIDER[provider]);
  if (preferred) return preferred.id;
  const hit = MODEL_CATALOG.find((m) => m.provider === provider);
  return hit ? hit.id : MODEL_CATALOG[0].id;
}

function modelById(modelId: string, providerFallback?: ProviderId): ModelCard {
  const hit = findModelById(modelId);
  if (hit) return hit;
  if (providerFallback) {
    const providerHit = MODEL_CATALOG.find((m) => m.provider === providerFallback);
    if (providerHit) return providerHit;
  }
  return MODEL_CATALOG[0];
}

function readProviderConfig(): Record<ProviderId, ProviderConfig> {
  return {
    gemini: {
      baseUrl: localStorage.getItem(`${LS_BASE_URL_PREFIX}gemini`) || PROVIDER_META.gemini.defaultBase,
      apiKey: localStorage.getItem(`${LS_API_KEY_PREFIX}gemini`) || '',
    },
    openai: {
      baseUrl: localStorage.getItem(`${LS_BASE_URL_PREFIX}openai`) || PROVIDER_META.openai.defaultBase,
      apiKey: localStorage.getItem(`${LS_API_KEY_PREFIX}openai`) || '',
    },
    anthropic: {
      baseUrl: localStorage.getItem(`${LS_BASE_URL_PREFIX}anthropic`) || PROVIDER_META.anthropic.defaultBase,
      apiKey: localStorage.getItem(`${LS_API_KEY_PREFIX}anthropic`) || '',
    },
    grok: {
      baseUrl: localStorage.getItem(`${LS_BASE_URL_PREFIX}grok`) || PROVIDER_META.grok.defaultBase,
      apiKey: localStorage.getItem(`${LS_API_KEY_PREFIX}grok`) || '',
    },
  };
}

export default function App() {
  const initialProvider = (localStorage.getItem(LS_PROVIDER) as ProviderId) || 'gemini';
  const [state, setState] = useState<AppState>(defaultState);
  const [activeTab, setActiveTab] = useState<TabId>('wing3d');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [isBusy, setIsBusy] = useState(false);

  const [providerConfigs, setProviderConfigs] = useState<Record<ProviderId, ProviderConfig>>(readProviderConfig);
  const [provider, setProvider] = useState<ProviderId>(initialProvider);
  const [model, setModel] = useState(localStorage.getItem(LS_MODEL) || firstModelForProvider(initialProvider));

  const [chatCollapsed, setChatCollapsed] = useState(localStorage.getItem(LS_CHAT_COLLAPSED) === '1');
  const [chatWidth, setChatWidth] = useState(Number(localStorage.getItem(LS_CHAT_WIDTH) || 320));
  const [showModelDrawer, setShowModelDrawer] = useState(false);
  const [showSettingsModal, setShowSettingsModal] = useState(false);
  const [showHistoryDrawer, setShowHistoryDrawer] = useState(false);
  const [historyTab, setHistoryTab] = useState<'save' | 'compare'>('save');

  const [saves, setSaves] = useState<SaveSnapshotRecord[]>([]);
  const [saveName, setSaveName] = useState('');
  const [selectedSave, setSelectedSave] = useState('');
  const [compareA, setCompareA] = useState('');
  const [compareB, setCompareB] = useState('');
  const [compareSummary, setCompareSummary] = useState('');

  const [isApplyingAirfoil, setIsApplyingAirfoil] = useState(false);
  const [isExporting, setIsExporting] = useState(false);

  const dragRef = useRef<{ active: boolean; startX: number; startW: number }>({ active: false, startX: 0, startW: 320 });
  const chatListRef = useRef<HTMLDivElement | null>(null);

  const activeModel = useMemo(() => modelById(model, provider), [model, provider]);
  const activeProviderMeta = PROVIDER_META[provider];
  const providerOrder: ProviderId[] = ['gemini', 'openai', 'anthropic', 'grok'];

  useEffect(() => {
    localStorage.setItem(LS_PROVIDER, provider);
  }, [provider]);

  useEffect(() => {
    localStorage.setItem(LS_MODEL, model);
  }, [model]);

  useEffect(() => {
    const bounded = Math.min(480, Math.max(260, chatWidth || 320));
    if (bounded !== chatWidth) {
      setChatWidth(bounded);
      return;
    }
    localStorage.setItem(LS_CHAT_WIDTH, String(bounded));
  }, [chatWidth]);

  useEffect(() => {
    localStorage.setItem(LS_CHAT_COLLAPSED, chatCollapsed ? '1' : '0');
  }, [chatCollapsed]);

  useEffect(() => {
    if (chatCollapsed && showModelDrawer) {
      setShowModelDrawer(false);
    }
  }, [chatCollapsed, showModelDrawer]);

  useEffect(() => {
    (Object.keys(providerConfigs) as ProviderId[]).forEach((p) => {
      localStorage.setItem(`${LS_BASE_URL_PREFIX}${p}`, providerConfigs[p].baseUrl);
      localStorage.setItem(`${LS_API_KEY_PREFIX}${p}`, providerConfigs[p].apiKey);
    });
  }, [providerConfigs]);

  useEffect(() => {
    const active = findModelById(model);
    if (!active || active.provider !== provider) {
      setModel(firstModelForProvider(provider));
    }
  }, [provider, model]);

  useEffect(() => {
    void refreshStateAndSaves();
  }, []);

  useEffect(() => {
    if (!chatListRef.current) return;
    chatListRef.current.scrollTop = chatListRef.current.scrollHeight;
  }, [messages, isBusy]);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragRef.current.active) return;
      const delta = e.clientX - dragRef.current.startX;
      const width = Math.min(480, Math.max(260, dragRef.current.startW + delta));
      setChatWidth(width);
    };
    const onUp = () => {
      dragRef.current.active = false;
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, []);

  async function refreshStateAndSaves() {
    const [statePayload, savesPayload] = await Promise.all([bridge.getState(), bridge.listSaves()]);
    setState(statePayload);
    setSaves(savesPayload.saves || []);
  }

  function hasApiKey(p: ProviderId): boolean {
    return Boolean(providerConfigs[p].apiKey.trim());
  }

  function appendAssistantMessage(content: string) {
    setMessages((prev) => [...prev, { role: 'assistant', content }]);
  }

  async function sendMessage() {
    const message = input.trim();
    if (!message || isBusy) return;

    const cfg = providerConfigs[provider];
    if (!cfg.apiKey.trim()) {
      appendAssistantMessage('설정에서 API 키를 입력해 주세요. API 키가 없으면 모델 호출이 불가능합니다.');
      return;
    }

    setInput('');
    const nextHistory = [...messages, { role: 'user' as const, content: message }];
    setMessages(nextHistory);
    setIsBusy(true);

    try {
      const res = await bridge.chat({
        message,
        history: nextHistory.map((m) => ({ role: m.role, content: m.content })),
        provider,
        model,
        base_url: cfg.baseUrl,
        api_key: cfg.apiKey,
      });
      setState(res.state);
      const reply = (res.assistant_message || res.explanation || '모델 응답을 받지 못했어요. 다시 시도해 주세요.').trim();
      appendAssistantMessage(reply);
    } catch (err: any) {
      appendAssistantMessage(`모델 호출 오류: ${err?.message || String(err)}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function onResetState() {
    try {
      const res = await bridge.reset();
      setState(res.state);
      setMessages([]);
      setCompareSummary('');
    } catch (err: any) {
      appendAssistantMessage(`초기화 실패: ${err?.message || String(err)}`);
    }
  }

  async function onApplyCustomAirfoil(custom: {
    max_camber_percent: number;
    max_camber_x_percent: number;
    thickness_percent: number;
    reflex_percent: number;
  }) {
    setIsApplyingAirfoil(true);
    try {
      const hadPrecisionBefore = Boolean(state.analysis.precision_result);
      const setRes = await bridge.command({ command: { type: 'SetAirfoil', payload: { custom } } });
      const meshRes = await bridge.command({ command: { type: 'BuildWingMesh', payload: {} } });
      if (hadPrecisionBefore) {
        const precisionRes = await bridge.command({ command: { type: 'RunPrecisionAnalysis', payload: {} } });
        setState(precisionRes.state ?? meshRes.state ?? setRes.state);
        appendAssistantMessage('커스텀 에어포일을 적용했고, 기존 해석 이력이 있어서 정밀 공력해석까지 다시 갱신했어요.');
      } else {
        setState(meshRes.state ?? setRes.state);
        appendAssistantMessage('커스텀 에어포일을 적용해 3D 형상만 빠르게 갱신했어요. 필요하면 정밀 공력해석을 실행해 주세요.');
      }
    } catch (err: any) {
      appendAssistantMessage(`커스텀 에어포일 적용 실패: ${err?.message || String(err)}`);
    } finally {
      setIsApplyingAirfoil(false);
    }
  }

  async function onSaveCurrent() {
    try {
      const rec = await bridge.saveSnapshot({ name: saveName || null });
      setSaveName('');
      setSelectedSave(rec.id);
      await refreshStateAndSaves();
    } catch (err: any) {
      appendAssistantMessage(`저장 실패: ${err?.message || String(err)}`);
    }
  }

  async function onLoadSave() {
    if (!selectedSave) return;
    try {
      const res = await bridge.loadSnapshot({ save_id: selectedSave });
      setState(res.state);
      appendAssistantMessage(res.assistant_message || '저장 상태를 불러왔어요.');
    } catch (err: any) {
      appendAssistantMessage(`불러오기 실패: ${err?.message || String(err)}`);
    }
  }

  async function onCompareSaves() {
    if (!compareA || !compareB) return;
    try {
      const out: SaveSnapshotCompareResponse = await bridge.compareSnapshots({ left_id: compareA, right_id: compareB });
      const changed = out.diffs.filter((d) => d.left !== d.right).length;
      setCompareSummary(`${out.left.name} ↔ ${out.right.name} | 변경 항목 ${changed}개`);
    } catch (err: any) {
      setCompareSummary('비교 실패');
      appendAssistantMessage(`비교 실패: ${err?.message || String(err)}`);
    }
  }

  async function onExportCfd() {
    setIsExporting(true);
    try {
      const res = await bridge.exportCfd({});
      appendAssistantMessage(`CFD 모델을 내보냈어요. 경로: ${res.path}`);
    } catch (err: any) {
      appendAssistantMessage(`CFD 내보내기 실패: ${err?.message || String(err)}`);
    } finally {
      setIsExporting(false);
    }
  }

  function onSelectModel(card: ModelCard) {
    if (!hasApiKey(card.provider)) return;
    setProvider(card.provider);
    setModel(card.id);
  }

  function updateProviderConfig(p: ProviderId, patch: Partial<ProviderConfig>) {
    setProviderConfigs((prev) => ({
      ...prev,
      [p]: {
        ...prev[p],
        ...patch,
      },
    }));
  }

  const canSend = input.trim().length > 0 && !isBusy;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          AUAVWDS <span>Chat-first Wing Designer</span>
        </div>
        <div className="tabs">
          <button className={activeTab === 'airfoil' ? 'active' : ''} onClick={() => setActiveTab('airfoil')}>Airfoil</button>
          <button className={activeTab === 'wing3d' ? 'active' : ''} onClick={() => setActiveTab('wing3d')}>Wing 3D</button>
          <button className={activeTab === 'aero' ? 'active' : ''} onClick={() => setActiveTab('aero')}>Aerodynamics</button>
        </div>
      </header>

      <section className="history-bar">
        <div className="history-current">
          {selectedSave
            ? `선택된 저장: ${saves.find((s) => s.id === selectedSave)?.name || selectedSave}`
            : '저장 기록 없음'}
        </div>
        <button
          className="history-open-btn"
          onClick={() => {
            setHistoryTab('save');
            setShowHistoryDrawer(true);
          }}
        >
          저장
        </button>
        <button className="ghost" onClick={() => void refreshStateAndSaves()}>새로고침</button>
      </section>

      <div className="main-body">
        {!chatCollapsed && (
          <aside className="chat-panel" style={{ width: chatWidth }}>
            <div className="chat-header">
              <button
                className="icon-btn"
                onClick={() => {
                  setShowModelDrawer(false);
                  setChatCollapsed(true);
                }}
              >
                {'<'}
              </button>
              <div className="chat-head-text">
                <div className="chat-title">Design Chat</div>
                <div className="chat-sub">원하는 날개를 한 문장으로 말해줘</div>
              </div>
              <div className="chat-actions">
                <button className="icon-btn" onClick={() => setShowSettingsModal(true)}>⚙</button>
                <button className="icon-btn" onClick={onResetState}>↺</button>
              </div>
            </div>

            <div className="model-strip">
              <div className="model-label">Model</div>
              <div className="model-select-row">
                <button className="model-selector" onClick={() => setShowModelDrawer((v) => !v)}>
                  <span className="model-mark">{PROVIDER_META[activeModel.provider].mark}</span>
                  <span className="model-name">{activeModel.title}</span>
                </button>
                <button className="list-open-btn" onClick={() => setShowModelDrawer((v) => !v)}>목록 열기</button>
              </div>
            </div>

            <div className="chat-list" ref={chatListRef}>
              {messages.map((m, idx) => (
                <div key={idx} className={`msg ${m.role}`}>
                  <div className="msg-role">{m.role.toUpperCase()}</div>
                  <div className="msg-content">{m.content}</div>
                </div>
              ))}

              {isBusy && (
                <div className="msg assistant">
                  <div className="msg-role">ASSISTANT</div>
                  <div className="typing"><span></span><span></span><span></span></div>
                </div>
              )}
            </div>

            <div className="chat-input-row">
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    void sendMessage();
                  }
                }}
                placeholder="예: NACA 2412로 스팬 2m, AR 10, 스윕 15도 날개 만들고 폴라 보여줘"
              />
              <button disabled={!canSend} onClick={() => void sendMessage()}>
                ➤
              </button>
            </div>

            <div className={`model-drawer ${showModelDrawer ? 'open' : ''}`}>
              <div className="drawer-head">
                <div>
                  <div className="drawer-title">모델 선택</div>
                  <div className="drawer-sub">모델 설명을 보고 선택하세요.</div>
                </div>
                <button className="icon-btn" onClick={() => setShowModelDrawer(false)}>×</button>
              </div>

              <div className="drawer-list">
                {providerOrder.map((p) => (
                  <div key={p} className="drawer-group">
                    <div className="drawer-group-title">{PROVIDER_META[p].label}</div>
                    {MODEL_CATALOG.filter((m) => m.provider === p).map((item) => {
                      const selected = model === item.id;
                      const enabled = hasApiKey(item.provider);
                      return (
                        <button
                          key={item.id}
                          className={`model-card ${selected ? 'selected' : ''} ${enabled ? '' : 'disabled'}`}
                          onClick={() => onSelectModel(item)}
                          disabled={!enabled}
                        >
                          <div className="model-card-top">
                            <span className="model-mark">{PROVIDER_META[item.provider].mark}</span>
                            <span className="model-card-title">{item.title}</span>
                            {selected && <span className="selected-tag">선택됨</span>}
                          </div>
                          <div className="model-card-sub">{item.subtitle}</div>
                          <div className="model-card-desc">{item.description}</div>
                          {!enabled && <div className="model-card-disabled">API 키 입력 시 활성화</div>}
                        </button>
                      );
                    })}
                  </div>
                ))}
              </div>
            </div>
          </aside>
        )}

        {!chatCollapsed && (
          <div
            className="resize-handle"
            onMouseDown={(e) => {
              dragRef.current = { active: true, startX: e.clientX, startW: chatWidth };
            }}
          />
        )}

        {chatCollapsed && (
          <button className="chat-reopen" onClick={() => setChatCollapsed(false)}>{'>'}</button>
        )}

        <section className="canvas-panel">
          {activeTab === 'airfoil' && (
            <AirfoilTab
              airfoil={state.airfoil}
              onApplyCustom={onApplyCustomAirfoil}
              isApplying={isApplyingAirfoil}
            />
          )}
          {activeTab === 'wing3d' && (
            <Wing3DTab
              wing={state.wing}
              analysis={state.analysis}
              onExportCfd={onExportCfd}
              isExporting={isExporting}
            />
          )}
          {activeTab === 'aero' && <AerodynamicsTab analysis={state.analysis} />}
        </section>

        <aside className={`history-drawer ${showHistoryDrawer ? 'open' : ''}`}>
          <div className="drawer-head">
            <div>
              <div className="drawer-title">저장 히스토리</div>
              <div className="drawer-sub">에어포일/형상 저장과 비교를 관리하세요.</div>
            </div>
            <button className="icon-btn" onClick={() => setShowHistoryDrawer(false)}>×</button>
          </div>

          <div className="history-tabs">
            <button
              className={historyTab === 'save' ? 'active' : ''}
              onClick={() => setHistoryTab('save')}
            >
              저장
            </button>
            <button
              className={historyTab === 'compare' ? 'active' : ''}
              onClick={() => setHistoryTab('compare')}
            >
              비교
            </button>
          </div>

          <div className="history-drawer-body">
            {historyTab === 'save' && (
              <div className="history-section">
                <label>저장 이름</label>
                <input
                  value={saveName}
                  onChange={(e) => setSaveName(e.target.value)}
                  placeholder="저장 이름 (선택)"
                />
                <button className="primary" onClick={() => void onSaveCurrent()}>현재 상태 저장</button>

                <label>저장 목록</label>
                <select value={selectedSave} onChange={(e) => setSelectedSave(e.target.value)}>
                  <option value="">저장 기록 없음</option>
                  {saves.map((s) => (
                    <option key={s.id} value={s.id}>{s.name}</option>
                  ))}
                </select>
                <div className="history-actions-row">
                  <button onClick={() => void onLoadSave()}>불러오기</button>
                  <button className="ghost" onClick={() => void refreshStateAndSaves()}>새로고침</button>
                </div>
              </div>
            )}

            {historyTab === 'compare' && (
              <div className="history-section">
                <label>비교 A</label>
                <select value={compareA} onChange={(e) => setCompareA(e.target.value)}>
                  <option value="">비교 A 선택</option>
                  {saves.map((s) => (
                    <option key={`A-${s.id}`} value={s.id}>{s.name}</option>
                  ))}
                </select>

                <label>비교 B</label>
                <select value={compareB} onChange={(e) => setCompareB(e.target.value)}>
                  <option value="">비교 B 선택</option>
                  {saves.map((s) => (
                    <option key={`B-${s.id}`} value={s.id}>{s.name}</option>
                  ))}
                </select>

                <button className="primary" onClick={() => void onCompareSaves()}>비교 버튼</button>
                {compareSummary && <div className="compare-summary in-drawer">{compareSummary}</div>}
              </div>
            )}
          </div>
        </aside>
      </div>

      {showSettingsModal && (
        <div className="modal-overlay" onClick={() => setShowSettingsModal(false)}>
          <div className="settings-modal" onClick={(e) => e.stopPropagation()}>
            <div className="settings-head">
              <div className="settings-title">API Provider</div>
              <button className="icon-btn" onClick={() => setShowSettingsModal(false)}>×</button>
            </div>

            <div className="provider-grid">
              {providerOrder.map((p) => (
                <div className="provider-card" key={p}>
                  <div className="provider-card-head">
                    <strong>{PROVIDER_META[p].label}</strong>
                    <button className={provider === p ? 'active' : ''} onClick={() => setProvider(p)}>
                      사용하기
                    </button>
                  </div>
                  <label>API Base URL</label>
                  <input
                    value={providerConfigs[p].baseUrl}
                    onChange={(e) => updateProviderConfig(p, { baseUrl: e.target.value })}
                    placeholder={PROVIDER_META[p].defaultBase}
                  />
                  <label>API Key</label>
                  <input
                    type="password"
                    value={providerConfigs[p].apiKey}
                    onChange={(e) => updateProviderConfig(p, { apiKey: e.target.value })}
                    placeholder="API Key 입력"
                  />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
