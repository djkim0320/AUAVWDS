export type ProviderId = 'openai' | 'anthropic' | 'gemini' | 'grok';

export interface AirfoilSummary {
  code: string;
  thickness_percent: number;
  max_camber_percent: number;
  max_camber_x_percent: number;
}

export interface AirfoilState {
  coords: [number, number][];
  upper: [number, number][];
  lower: [number, number][];
  camber: [number, number][];
  summary: AirfoilSummary;
}

export interface WingParams {
  span_m: number;
  aspect_ratio: number;
  sweep_deg: number;
  taper_ratio: number;
  dihedral_deg: number;
  twist_deg: number;
}

export interface WingMesh {
  vertices: [number, number, number][];
  triangles: [number, number, number][];
  pressure_overlay: number[];
}

export interface Planform2D {
  polygon: [number, number][];
  quarter_chord: [number, number][];
}

export interface WingState {
  params: WingParams;
  preview_mesh: WingMesh | null;
  planform_2d: Planform2D | null;
}

export interface AeroCurve {
  aoa_deg: number[];
  cl: number[];
  cd: number[];
  cm: number[];
}

export interface DerivedMetrics {
  ld_max: number;
  ld_max_aoa: number;
  cl_max: number;
  cl_max_aoa: number;
  cd_min: number;
  cd_min_aoa: number;
  cl_alpha: number;
  alpha_zero_lift: number;
  cm_zero_lift: number;
  cm_alpha: number;
  cd_zero: number;
  oswald_e: number;
  endurance_param: number;
  range_param: number;
  reynolds: number;
}

export interface AnalysisResult {
  source_label: string;
  curve: AeroCurve;
  metrics: DerivedMetrics | null;
  extra_data: Record<string, unknown>;
  notes: string;
  created_at: string;
}

export interface AnalysisState {
  precision_result: AnalysisResult | null;
  mode: 'precision';
}

export interface AppState {
  airfoil: AirfoilState;
  wing: WingState;
  analysis: AnalysisState;
  history: Record<string, unknown>[];
}

export interface CommandEnvelope {
  type:
    | 'SetAirfoil'
    | 'SetWing'
    | 'BuildWingMesh'
    | 'RunPrecisionAnalysis'
    | 'Explain'
    | 'Undo'
    | 'Reset';
  payload?: Record<string, unknown>;
}

export interface BackendResponse {
  state: AppState;
  applied_commands: CommandEnvelope[];
  explanation: string;
  warnings: string[];
  assistant_message?: string;
}

export interface SaveSnapshotRecord {
  id: string;
  name: string;
  created_at: string;
  summary: Record<string, unknown>;
}

export interface SaveSnapshotCompareItem {
  key: string;
  left?: unknown;
  right?: unknown;
  delta?: number | null;
}

export interface SaveSnapshotCompareResponse {
  left: SaveSnapshotRecord;
  right: SaveSnapshotRecord;
  diffs: SaveSnapshotCompareItem[];
  summary: string;
}

export interface ModelDiscoveryResponse {
  models: string[];
  source_url: string;
  error?: string | null;
}

