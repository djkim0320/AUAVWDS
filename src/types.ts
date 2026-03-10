export type ProviderId = 'openai' | 'anthropic' | 'gemini' | 'grok';
export type SolverId = 'openvsp' | 'neuralfoil';
export type AnalysisMode = 'openvsp' | 'neuralfoil' | 'fallback';
export type ExportFormat = 'obj' | 'json' | 'vsp3';
export type WingtipStyle = 'straight' | 'pinched';

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
  wingtip_style: WingtipStyle;
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

export interface AnalysisConditions {
  aoa_start: number;
  aoa_end: number;
  aoa_step: number;
  mach: number;
  reynolds: number | null;
}

export interface AoaRange {
  start: number;
  end: number;
}

export interface CurveFilteringInfo {
  raw_row_count?: number;
  plausible_row_count?: number;
  valid_row_count?: number;
  dropped_row_count?: number;
  dropped_aoa?: number[];
  used_aoa_range?: AoaRange | null;
  requested_aoa_range?: AoaRange | null;
  exclusion_reason_summary?: Record<string, number>;
}

export interface SolverEffectiveConditions {
  source?: string;
  requested_reynolds?: number | null;
  re_cref?: number | null;
  reynolds_applied?: boolean;
  reynolds_note?: string;
  mach?: number | null;
  wake_iterations?: number | null;
  aoa_range?: AoaRange | null;
  aoa_count?: number | null;
}

export interface CoefficientFamilyCandidate {
  label?: string;
  available?: boolean;
  selected?: boolean;
  raw_row_count?: number;
  plausible_row_count?: number;
  valid_row_count?: number;
  dropped_row_count?: number;
  used_aoa_range?: AoaRange | null;
  score?: number;
  columns?: Record<string, string>;
  exclusion_reason_summary?: Record<string, number>;
}

export interface AnalysisExtraData extends Record<string, unknown> {
  available_artifacts?: string[];
  solver_label?: string;
  solver_id?: string;
  result_level?: string;
  correction_model?: string;
  wing_correction_model?: string;
  limitation_note?: string;
  solver_airfoil?: Record<string, unknown>;
  solver_wingtip?: Record<string, unknown>;
  requested_aoa_range?: AoaRange | null;
  valid_aoa_range?: AoaRange | null;
  curve_filtering?: CurveFilteringInfo;
  precision_data?: Record<string, unknown>;
  vspaero_all_data?: Record<string, unknown>;
  solver_scalar_data?: Record<string, unknown>;
  selected_coefficient_family?: string;
  selected_coefficient_family_label?: string;
  coefficient_family_selection?: string;
  selected_coefficient_columns?: Record<string, string>;
  coefficient_family_candidates?: Record<string, CoefficientFamilyCandidate>;
  solver_effective_conditions?: SolverEffectiveConditions;
}

export interface AnalysisResult {
  source_label: string;
  curve: AeroCurve;
  metrics: DerivedMetrics | null;
  analysis_mode: AnalysisMode;
  fallback_reason: string | null;
  extra_data: AnalysisExtraData;
  notes: string;
  created_at: string;
}

export interface SolverResults {
  openvsp: AnalysisResult | null;
  neuralfoil: AnalysisResult | null;
}

export interface AnalysisState {
  results: SolverResults;
  active_solver: SolverId;
  conditions: AnalysisConditions;
}

export interface AppState {
  airfoil: AirfoilState;
  wing: WingState;
  analysis: AnalysisState;
  history: Record<string, unknown>[];
}

export interface SummaryAirfoilState {
  coords: [];
  upper: [];
  lower: [];
  camber: [];
  summary: AirfoilSummary;
}

export interface SummaryWingState {
  params: WingParams;
  preview_mesh: null;
  planform_2d: null;
}

export interface SummaryAeroCurve {
  aoa_deg: [];
  cl: [];
  cd: [];
  cm: [];
}

export interface SummaryAnalysisResult {
  source_label: string;
  curve: SummaryAeroCurve;
  metrics: DerivedMetrics | null;
  analysis_mode: AnalysisMode;
  fallback_reason: string | null;
  extra_data: AnalysisExtraData;
  notes: string;
  created_at: string;
}

export interface SummarySolverResults {
  openvsp: SummaryAnalysisResult | null;
  neuralfoil: SummaryAnalysisResult | null;
}

export interface SummaryAnalysisState {
  results: SummarySolverResults;
  active_solver: SolverId;
  conditions: AnalysisConditions;
}

export interface SummaryAppState {
  airfoil: SummaryAirfoilState;
  wing: SummaryWingState;
  analysis: SummaryAnalysisState;
  history: [];
}

export interface CommandEnvelope {
  type:
    | 'SetAirfoil'
    | 'SetWing'
    | 'BuildWingMesh'
    | 'SetAnalysisConditions'
    | 'SetActiveSolver'
    | 'RunOpenVspAnalysis'
    | 'RunNeuralFoilAnalysis'
    | 'RunPrecisionAnalysis'
    | 'Explain'
    | 'Undo'
    | 'Reset';
  payload?: Record<string, unknown>;
}

export interface BackendResponse<TState = SummaryAppState> {
  state: TState;
  applied_commands: CommandEnvelope[];
  explanation: string;
  warnings: string[];
  assistant_message?: string;
}

export type SummaryBackendResponse = BackendResponse<SummaryAppState>;

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

