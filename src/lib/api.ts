import type {
  AppState,
  BackendResponse,
  CommandEnvelope,
  ExportFormat,
  ModelDiscoveryResponse,
  SaveSnapshotCompareResponse,
  SaveSnapshotRecord,
} from '../types';

declare global {
  interface Window {
    auavApi: {
      onBackendReady: (cb: (payload: { baseUrl: string }) => void) => () => void;
      getState: () => Promise<AppState>;
      chat: (req: any) => Promise<BackendResponse>;
      command: (req: { command: CommandEnvelope }) => Promise<BackendResponse>;
      reset: () => Promise<BackendResponse>;
      discoverModels: (req: any) => Promise<ModelDiscoveryResponse>;
      listSaves: () => Promise<{ saves: SaveSnapshotRecord[] }>;
      saveSnapshot: (req: { name?: string | null }) => Promise<SaveSnapshotRecord>;
      loadSnapshot: (req: { save_id: string }) => Promise<BackendResponse>;
      compareSnapshots: (req: { left_id: string; right_id: string }) => Promise<SaveSnapshotCompareResponse>;
      exportCfd: (req: { format?: ExportFormat; output_path?: string | null }) => Promise<any>;
    };
  }
}

export const bridge = {
  getState: () => window.auavApi.getState(),
  chat: (req: any) => window.auavApi.chat(req),
  command: (req: { command: CommandEnvelope }) => window.auavApi.command(req),
  reset: () => window.auavApi.reset(),
  discoverModels: (req: any) => window.auavApi.discoverModels(req),
  listSaves: () => window.auavApi.listSaves(),
  saveSnapshot: (req: { name?: string | null }) => window.auavApi.saveSnapshot(req),
  loadSnapshot: (req: { save_id: string }) => window.auavApi.loadSnapshot(req),
  compareSnapshots: (req: { left_id: string; right_id: string }) => window.auavApi.compareSnapshots(req),
  exportCfd: (req: { format?: ExportFormat; output_path?: string | null }) => window.auavApi.exportCfd(req),
};

