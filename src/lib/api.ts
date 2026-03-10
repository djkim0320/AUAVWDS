import type {
  AppState,
  SummaryAppState,
  SummaryBackendResponse,
  CommandEnvelope,
  ExportFormat,
  SaveSnapshotCompareResponse,
  SaveSnapshotRecord,
} from '../types';

type ChatRequest = {
  message: string;
  history: Array<{ role: string; content: string }>;
  provider: string;
  model: string;
  base_url: string;
  api_key: string;
};

type ExportResponse = {
  ok: boolean;
  path: string;
  format: string;
};

type Bridge = {
  getState: () => Promise<SummaryAppState>;
  getFullState: () => Promise<AppState>;
  chat: (req: ChatRequest) => Promise<SummaryBackendResponse>;
  command: (req: { command: CommandEnvelope }) => Promise<SummaryBackendResponse>;
  reset: () => Promise<SummaryBackendResponse>;
  listSaves: () => Promise<{ saves: SaveSnapshotRecord[] }>;
  saveSnapshot: (req: { name?: string | null }) => Promise<SaveSnapshotRecord>;
  loadSnapshot: (req: { save_id: string }) => Promise<SummaryBackendResponse>;
  compareSnapshots: (req: { left_id: string; right_id: string }) => Promise<SaveSnapshotCompareResponse>;
  exportCfd: (req: { format?: ExportFormat }) => Promise<ExportResponse>;
};

declare global {
  interface Window {
    auavApi?: Bridge;
  }
}

const WEB_BRIDGE_ENABLED = import.meta.env.VITE_ENABLE_WEB_BRIDGE === '1';
const WEB_BACKEND_BASE_URL = '/api';

function getElectronBridge(): Bridge {
  if (!window.auavApi) {
    throw new Error('Electron bridge is unavailable. Start with npm run dev:web for browser mode.');
  }
  return window.auavApi;
}

async function httpJson<T>(pathname: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${WEB_BACKEND_BASE_URL}${pathname}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
    ...init,
  });

  const text = await response.text();
  let payload: unknown = {};

  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = { detail: text || 'Invalid backend response' };
  }

  if (!response.ok) {
    const detail =
      typeof payload === 'object' && payload && 'detail' in payload
        ? (payload as { detail?: unknown }).detail
        : null;
    throw new Error(String(detail || `${response.status} ${response.statusText}`));
  }

  return payload as T;
}

function createHttpBridge(): Bridge {
  return {
    getState: () => httpJson<SummaryAppState>('/state/client'),
    getFullState: () => httpJson<AppState>('/state'),
    chat: (req) => httpJson<SummaryBackendResponse>('/chat', { method: 'POST', body: JSON.stringify(req) }),
    command: (req) => httpJson<SummaryBackendResponse>('/command', { method: 'POST', body: JSON.stringify(req) }),
    reset: () => httpJson<SummaryBackendResponse>('/reset', { method: 'POST' }),
    listSaves: () => httpJson<{ saves: SaveSnapshotRecord[] }>('/saves'),
    saveSnapshot: (req) => httpJson<SaveSnapshotRecord>('/saves', { method: 'POST', body: JSON.stringify(req) }),
    loadSnapshot: (req) => httpJson<SummaryBackendResponse>('/saves/load', { method: 'POST', body: JSON.stringify(req) }),
    compareSnapshots: (req) =>
      httpJson<SaveSnapshotCompareResponse>('/saves/compare', { method: 'POST', body: JSON.stringify(req) }),
    exportCfd: (req) => httpJson<ExportResponse>('/export/cfd', { method: 'POST', body: JSON.stringify(req) }),
  };
}

export const bridge: Bridge = WEB_BRIDGE_ENABLED ? createHttpBridge() : getElectronBridge();
