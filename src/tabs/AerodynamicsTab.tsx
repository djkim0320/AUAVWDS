import { useEffect, useMemo, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import SourceBadge from '../components/SourceBadge';
import type { AnalysisConditions, AnalysisResult, AnalysisState, SolverId } from '../types';

function clamp(v: number, lo: number, hi: number) {
  return Math.min(hi, Math.max(lo, v));
}

function fmt(v?: number | null, d = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-';
  return Number(v).toFixed(d);
}

function fmtInt(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-';
  return Math.round(Number(v)).toLocaleString('ko-KR');
}

function toNumber(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v.trim()) {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

function trimTrailingZeros(value: string): string {
  return value.replace(/(\.\d*?[1-9])0+$/u, '$1').replace(/\.0+$/u, '').replace(/^-0$/u, '0');
}

function fmtAdaptive(value: number, span: number, floorDigits = 2, ceilingDigits = 5): string {
  const abs = Math.abs(value);
  const safeSpan = Math.max(Math.abs(span), abs, 1e-9);
  let digits = floorDigits;
  if (safeSpan < 0.01 || abs < 0.01) {
    digits = Math.max(digits, 5);
  } else if (safeSpan < 0.1 || abs < 0.1) {
    digits = Math.max(digits, 4);
  } else if (safeSpan < 1 || abs < 1) {
    digits = Math.max(digits, 3);
  }
  return trimTrailingZeros(value.toFixed(Math.min(ceilingDigits, digits)));
}

function fmtAoaRange(range?: Record<string, unknown> | null): string {
  const start = toNumber(range?.start ?? range?.aoa_start);
  const end = toNumber(range?.end ?? range?.aoa_end);
  if (start === null || end === null) return '-';
  return `${fmtAdaptive(start, Math.abs(end - start), 1, 4)}° ~ ${fmtAdaptive(end, Math.abs(end - start), 1, 4)}°`;
}

const BASE_LABELS: Record<string, string> = {
  aoa: '받음각 AoA',
  mach: '마하수 Mach',
  re_1e6: '레이놀즈수 Re/1e6',
  clo: '양력계수 CLo',
  cli: '유도 양력계수 CLi',
  cltot: '총 양력계수 CLtot',
  cdo: '기생 항력계수 CDo',
  cdi: '유도 항력계수 CDi',
  cdtot: '총 항력계수 CDtot',
  cso: '측력계수 CSo',
  csi: '유도 측력계수 CSi',
  cstot: '총 측력계수 CStot',
  l_d: '양항비 L/D',
  e: '오스왈드 효율 e',
  cmxtot: '롤 모멘트계수 CMx',
  cmytot: '피치 모멘트계수 CMy',
  cmztot: '요 모멘트계수 CMz',
  analysis_confidence: '해석 신뢰도',
  wall_time: '해석 시간',
};

function humanizeVspaeroKey(key: string): string {
  if (!key) return '-';
  let suffix = '';
  let base = key;
  if (base.endsWith('_ld_max')) {
    suffix = ' (L/D 최대 지점)';
    base = base.slice(0, -7);
  } else if (base.endsWith('_max')) {
    suffix = ' (최대)';
    base = base.slice(0, -4);
  } else if (base.endsWith('_min')) {
    suffix = ' (최소)';
    base = base.slice(0, -4);
  }
  const normalized = base.toLowerCase();
  const label = BASE_LABELS[normalized] || base;
  return `${label}${suffix}`;
}

function preferredResult(analysis: AnalysisState): { solver: SolverId | null; result: AnalysisResult | null } {
  const active = analysis.results[analysis.active_solver];
  if (active) return { solver: analysis.active_solver, result: active };
  if (analysis.results.openvsp) return { solver: 'openvsp', result: analysis.results.openvsp };
  if (analysis.results.neuralfoil) return { solver: 'neuralfoil', result: analysis.results.neuralfoil };
  return { solver: null, result: null };
}

type Props = {
  analysis: AnalysisState;
  onRunAnalysis: (solver: SolverId) => Promise<void>;
  onSelectSolver: (solver: SolverId) => Promise<void>;
  onUpdateConditions: (conditions: AnalysisConditions) => Promise<void>;
  isRunningAnalysis: boolean;
  isUpdatingConditions: boolean;
};

export default function AerodynamicsTab({
  analysis,
  onRunAnalysis,
  onSelectSolver,
  onUpdateConditions,
  isRunningAnalysis,
  isUpdatingConditions,
}: Props) {
  const { solver: resultSolver, result } = preferredResult(analysis);
  const [draft, setDraft] = useState<AnalysisConditions>(analysis.conditions);
  const appliedConditions = analysis.conditions;

  useEffect(() => {
    setDraft(analysis.conditions);
  }, [analysis.conditions]);

  const provenance = useMemo(() => {
    if (!result) return null;
    const extra = (result.extra_data || {}) as Record<string, unknown>;
    const availableArtifacts = Array.isArray(extra.available_artifacts)
      ? extra.available_artifacts.map(String)
      : [
          typeof extra.script_path === 'string' ? 'run_precision.vspscript' : null,
          typeof extra.stdout_log === 'string' ? 'solver_stdout.log' : null,
          typeof extra.stderr_log === 'string' ? 'solver_stderr.log' : null,
          typeof extra.outputs_path === 'string' ? 'outputs.json' : null,
          typeof extra.processed_result_path === 'string' ? 'processed_result.json' : null,
          typeof extra.vsp3_path === 'string' ? 'auav_case.vsp3' : null,
        ].filter(Boolean);

    return {
      solverLabel: String(extra.solver_label || (resultSolver === 'neuralfoil' ? 'NeuralFoil' : 'OpenVSP/VSPAERO')),
      solverId: String(extra.solver_id || resultSolver || '-'),
      resultLevel: String(extra.result_level || (result.analysis_mode === 'neuralfoil' ? 'wing_estimate_from_2d_solver' : 'wing_solver')),
      correctionModel: String(extra.correction_model || extra.wing_correction_model || '-'),
      limitationNote: String(extra.limitation_note || ''),
      availableArtifacts,
      solverAirfoil: extra.solver_airfoil as Record<string, unknown> | undefined,
      requestedAoaRange:
        (extra.requested_aoa_range as Record<string, unknown> | undefined) ||
        (extra.analysis_conditions as Record<string, unknown> | undefined) ||
        (analysis.conditions as unknown as Record<string, unknown>),
      validAoaRange:
        (extra.valid_aoa_range as Record<string, unknown> | undefined) ||
        ((extra.curve_filtering as Record<string, unknown> | undefined)?.used_aoa_range as Record<string, unknown> | undefined),
      droppedRowCount: toNumber((extra.curve_filtering as Record<string, unknown> | undefined)?.dropped_row_count),
    };
  }, [analysis.conditions, result, resultSolver]);

  if (!result) {
    return (
      <div className="canvas-workspace">
        <div className="panel-title-row">
          <div className="panel-title">공력 해석</div>
          <div className="solver-runner">
            <button disabled={isRunningAnalysis} onClick={() => void onRunAnalysis('openvsp')}>OpenVSP 실행</button>
            <button disabled={isRunningAnalysis} onClick={() => void onRunAnalysis('neuralfoil')}>NeuralFoil 실행</button>
          </div>
        </div>
        <ConditionsEditor
          draft={draft}
          applied={appliedConditions}
          onDraftChange={setDraft}
          onApply={onUpdateConditions}
          isUpdating={isUpdatingConditions}
        />
        <div className="empty-state">아직 공력 데이터가 없습니다. OpenVSP 또는 NeuralFoil 해석을 실행해 주세요.</div>
      </div>
    );
  }

  const c = result.curve;
  const m = result.metrics;
  const aoa = c.aoa_deg;
  const ld = c.cl.map((v, i) => {
    const cd = c.cd[i] || 0;
    if (Math.abs(cd) < 1e-6) return 0;
    return clamp(v / cd, -200, 200);
  });

  const aoaPlotMin = Math.min(appliedConditions.aoa_start, Math.min(...aoa));
  const aoaPlotMax = Math.max(appliedConditions.aoa_end, Math.max(...aoa));
  const plotIndices = aoa
    .map((a, idx) => (a >= aoaPlotMin && a <= aoaPlotMax ? idx : -1))
    .filter((idx) => idx >= 0);
  const aoaPlot = plotIndices.map((i) => aoa[i]);
  const clPlot = plotIndices.map((i) => c.cl[i]);
  const cdPlot = plotIndices.map((i) => c.cd[i]);
  const ldPlot = plotIndices.map((i) => clamp(ld[i], -200, 200));

  const chips = [
    { k: 'CD 최소', v: fmt(m?.cd_min, 4) },
    { k: '체공 지표 (CL^(3/2)/CD)', v: fmt(m?.endurance_param, 1) },
    { k: 'Oswald e', v: fmt(m?.oswald_e, 2) },
    { k: 'Re', v: fmtInt(m?.reynolds) },
  ];

  const precisionData = (result.extra_data?.precision_data as Record<string, unknown>) || null;
  const allData = (result.extra_data?.vspaero_all_data as Record<string, unknown>) || null;
  const rawNeuralFoil = (result.extra_data?.raw_neuralfoil_output as Record<string, unknown>) || null;
  const vspaeroRows: Array<{ key: string; label: string; value: string }> = [];
  const metricSource = allData || rawNeuralFoil || precisionData;

  if (metricSource && typeof metricSource === 'object') {
    Object.entries(metricSource)
      .sort(([a], [b]) => a.localeCompare(b))
      .forEach(([k, v]) => {
        const n = toNumber(v);
        if (n === null) return;
        const digits = Math.abs(n) < 1 ? 5 : 4;
        vspaeroRows.push({ key: k, label: humanizeVspaeroKey(k), value: fmt(n, digits) });
      });
  }

  const solverButtons: SolverId[] = ['openvsp', 'neuralfoil'];

  return (
    <div className="canvas-workspace aero-ui">
      <div className="panel-title-row">
        <div className="panel-title">공력 해석 결과</div>
        <div className="solver-runner">
          <button disabled={isRunningAnalysis} onClick={() => void onRunAnalysis('openvsp')}>OpenVSP 실행</button>
          <button disabled={isRunningAnalysis} onClick={() => void onRunAnalysis('neuralfoil')}>NeuralFoil 실행</button>
          <SourceBadge label={result.source_label} mode={result.analysis_mode} />
        </div>
      </div>

      <div className="solver-selector-row">
        {solverButtons.map((solver) => {
          const available = Boolean(analysis.results[solver]);
          const selected = analysis.active_solver === solver;
          return (
            <button
              key={solver}
              className={`solver-switch ${selected ? 'selected' : ''}`}
              disabled={!available}
              onClick={() => void onSelectSolver(solver)}
            >
              {solver === 'openvsp' ? 'OpenVSP/VSPAERO' : 'NeuralFoil'}
            </button>
          );
        })}
      </div>

      <ConditionsEditor
        draft={draft}
        applied={appliedConditions}
        onDraftChange={setDraft}
        onApply={onUpdateConditions}
        isUpdating={isUpdatingConditions}
      />

      {result.analysis_mode === 'fallback' && (
        <div className="analysis-alert fallback">
          선택한 solver 경로가 대체 해석으로 전환되었습니다.
          {result.fallback_reason ? ` 사유: ${result.fallback_reason}` : ''}
        </div>
      )}

      {provenance && (
        <div className="provenance-grid">
          <div className="provenance-card">
            <div className="provenance-title">Solver 출처 정보</div>
            <div className="kv"><span>Solver</span><strong>{provenance.solverLabel}</strong></div>
            <div className="kv"><span>Solver ID</span><strong>{provenance.solverId}</strong></div>
            <div className="kv"><span>결과 수준</span><strong>{provenance.resultLevel}</strong></div>
            <div className="kv"><span>보정 모델</span><strong>{provenance.correctionModel}</strong></div>
            <div className="kv"><span>에어포일 표현</span><strong>{String(provenance.solverAirfoil?.representation_label || provenance.solverAirfoil?.geometry_kind || '-')}</strong></div>
            <div className="kv"><span>요청 해석 범위</span><strong>{fmtAoaRange(provenance.requestedAoaRange)}</strong></div>
            <div className="kv"><span>채택 유효 범위</span><strong>{fmtAoaRange(provenance.validAoaRange)}</strong></div>
            <div className="kv"><span>제외된 행 수</span><strong>{fmtInt(provenance.droppedRowCount)}</strong></div>
          </div>
          <div className="provenance-card">
            <div className="provenance-title">산출물 및 메모</div>
            <div className="provenance-artifacts">
              {provenance.availableArtifacts.length > 0
                ? provenance.availableArtifacts.map((item) => <span key={item} className="metric-chip">{item}</span>)
                : <span className="muted">기록된 산출물이 없습니다.</span>}
            </div>
            {provenance.limitationNote && <div className="solver-note provenance-note">{provenance.limitationNote}</div>}
          </div>
        </div>
      )}

      <div className="aero-cards">
        <Metric title="최대 양항비 (L/D)" value={fmt(m?.ld_max, 1)} desc="높을수록 효율적입니다." emphasize />
        <Metric title="최적 받음각" value={`${fmt(m?.ld_max_aoa, 1)}도`} desc="L/D가 최대인 지점" />
        <Metric title="최대 양력 각도" value={`${fmt(m?.cl_max_aoa, 1)}도`} desc="CL이 최대인 지점" />
        <Metric title="최대 CL" value={fmt(m?.cl_max, 3)} desc="최대 양력계수" />
      </div>

      <div className="metric-chip-row">
        {chips.map((chip) => (
          <span key={chip.k} className="metric-chip">{chip.k} <strong>{chip.v}</strong></span>
        ))}
      </div>

      <div className="chart-grid">
        <Chart title="양력계수 (CL)" x={aoaPlot} y={clPlot} color="#70bbff" yName="CL" xMin={aoaPlotMin} xMax={aoaPlotMax} />
        <Chart title="양항비 (L/D)" x={aoaPlot} y={ldPlot} color="#efb35b" yName="L/D" xMin={aoaPlotMin} xMax={aoaPlotMax} />
        <Chart title="항력계수 (CD)" x={aoaPlot} y={cdPlot} color="#6ce8be" yName="CD" xMin={aoaPlotMin} xMax={aoaPlotMax} />
      </div>

      <div className="aero-detail-grid">
        <section className="detail-card">
          <h4>양력 특성</h4>
          <div className="kv"><span>양력 곡선 기울기 (CL_alpha)</span><strong>{fmt(m?.cl_alpha, 2)} /rad</strong></div>
          <div className="kv"><span>영양력 받음각</span><strong>{fmt(m?.alpha_zero_lift, 2)}도</strong></div>
        </section>

        <section className="detail-card">
          <h4>안정성 / 모멘트</h4>
          <div className="kv"><span>영양력 조건 Cm</span><strong>{fmt(m?.cm_zero_lift, 5)}</strong></div>
          <div className="kv"><span>Cm 기울기 (Cm_alpha)</span><strong>{fmt(m?.cm_alpha, 4)} /rad</strong></div>
        </section>

        <section className="detail-card">
          <h4>항력 특성</h4>
          <div className="kv"><span>영양력 항력 (CD0)</span><strong>{fmt(m?.cd_zero, 4)}</strong></div>
          <div className="kv"><span>유도 항력 효율 (e)</span><strong>{fmt(m?.oswald_e, 3)}</strong></div>
        </section>
      </div>

      <section className="vsp-extra-card">
        <h4>{resultSolver === 'neuralfoil' ? 'NeuralFoil / 보정 메타데이터' : 'VSPAERO 전체 데이터'}</h4>
        <div className="vsp-extra-grid">
          {vspaeroRows.length === 0 && <div className="muted">표시할 solver 데이터가 없습니다.</div>}
          {vspaeroRows.map((row) => (
            <div key={row.key} className="extra-item">
              <span>{row.label}</span>
              <strong>{row.value}</strong>
            </div>
          ))}
        </div>
      </section>

      <div className="result-note">{result.notes}</div>
    </div>
  );
}

function Metric({ title, value, desc, emphasize = false }: { title: string; value: string; desc: string; emphasize?: boolean }) {
  return (
    <div className={`metric-card ${emphasize ? 'emphasize' : ''}`}>
      <div className="metric-title">{title}</div>
      <div className="metric-value">{value}</div>
      <div className="metric-desc">{desc}</div>
    </div>
  );
}

function ConditionsEditor({
  draft,
  applied,
  onDraftChange,
  onApply,
  isUpdating,
}: {
  draft: AnalysisConditions;
  applied: AnalysisConditions;
  onDraftChange: (conditions: AnalysisConditions) => void;
  onApply: (conditions: AnalysisConditions) => Promise<void>;
  isUpdating: boolean;
}) {
  return (
    <div className="conditions-card">
      <div className="provenance-title">해석 조건</div>
      <div className="conditions-grid">
        <NumberField label="AoA 시작" value={draft.aoa_start} step={0.5} onChange={(value) => onDraftChange({ ...draft, aoa_start: value })} />
        <NumberField label="AoA 종료" value={draft.aoa_end} step={0.5} onChange={(value) => onDraftChange({ ...draft, aoa_end: value })} />
        <NumberField label="AoA 간격" value={draft.aoa_step} step={0.25} onChange={(value) => onDraftChange({ ...draft, aoa_step: value })} />
        <NumberField label="마하수" value={draft.mach} step={0.01} onChange={(value) => onDraftChange({ ...draft, mach: value })} />
        <NumberField
          label="레이놀즈수"
          value={draft.reynolds ?? 0}
          step={10000}
          allowEmpty
          onChange={(value) => onDraftChange({ ...draft, reynolds: value > 0 ? value : null })}
        />
      </div>
      <div className="conditions-actions">
        <button disabled={isUpdating} onClick={() => void onApply(draft)}>
          {isUpdating ? '조건 적용 중...' : '조건 적용'}
        </button>
      </div>
      <div className="solver-note">
        현재 적용된 계산 범위: {fmtAdaptive(applied.aoa_start, Math.abs(applied.aoa_end - applied.aoa_start), 1, 4)}°
        {' ~ '}
        {fmtAdaptive(applied.aoa_end, Math.abs(applied.aoa_end - applied.aoa_start), 1, 4)}°
        {' / 간격 '}
        {fmtAdaptive(applied.aoa_step, applied.aoa_step, 2, 4)}°.
      </div>
      <div className="solver-note">
        이 값은 그래프 축이 아니라 solver가 실제로 계산할 받음각 범위입니다. 변경 후 해석을 다시 실행해야 새 결과에 반영됩니다.
      </div>
    </div>
  );
}

function NumberField({
  label,
  value,
  step,
  onChange,
  allowEmpty = false,
}: {
  label: string;
  value: number;
  step: number;
  onChange: (value: number) => void;
  allowEmpty?: boolean;
}) {
  return (
    <label className="condition-field">
      <span>{label}</span>
      <input
        type="number"
        value={allowEmpty && !Number.isFinite(value) ? '' : value}
        step={step}
        onChange={(e) => {
          const next = e.target.value;
          if (allowEmpty && next === '') {
            onChange(0);
            return;
          }
          onChange(Number(next));
        }}
      />
    </label>
  );
}

function Chart({
  title,
  x,
  y,
  color,
  yName,
  xMin,
  xMax,
}: {
  title: string;
  x: number[];
  y: number[];
  color: string;
  yName: string;
  xMin: number;
  xMax: number;
}) {
  if (!x.length || !y.length) {
    return (
      <div className="chart-card">
        <div className="chart-title">{title}</div>
        <div className="muted">데이터 없음</div>
      </div>
    );
  }

  const points = x.map((v, i) => [toNumber(v) ?? 0, toNumber(y[i]) ?? 0] as [number, number]);
  const finiteY = y.filter((v) => Number.isFinite(v));
  const minY = finiteY.length ? Math.min(...finiteY) : 0;
  const maxY = finiteY.length ? Math.max(...finiteY) : 1;
  const rangeY = maxY - minY;
  const pad = rangeY > 0 ? rangeY * 0.1 : Math.max(0.01, Math.abs(maxY || minY) * 0.15, 0.01);

  let yMinAxis = minY - pad;
  let yMaxAxis = maxY + pad;
  if (Math.abs(yMinAxis - yMaxAxis) < 1e-9) {
    yMinAxis -= pad || 0.01;
    yMaxAxis += pad || 0.01;
  }
  const axisSpan = yMaxAxis - yMinAxis;

  return (
    <div className="chart-card">
      <div className="chart-title">{title}</div>
      <ReactECharts
        option={{
          backgroundColor: 'transparent',
          animation: false,
          grid: { left: 56, right: 20, top: 20, bottom: 44 },
          xAxis: {
            type: 'value',
            name: '받음각(도)',
            min: xMin,
            max: xMax,
            splitNumber: 6,
            axisLine: { lineStyle: { color: '#28425f' } },
            splitLine: { lineStyle: { color: '#162a42' } },
            axisLabel: {
              color: '#9cb0c8',
              formatter: (value: number) => fmtAdaptive(Number(value), axisSpan, 1, 5),
            },
            nameTextStyle: { color: '#8ea3bc' },
          },
          yAxis: {
            type: 'value',
            name: yName,
            min: yMinAxis,
            max: yMaxAxis,
            splitNumber: 6,
            axisLine: { lineStyle: { color: '#28425f' } },
            splitLine: { lineStyle: { color: '#162a42' } },
            axisLabel: {
              color: '#9cb0c8',
              formatter: (value: number) => fmtAdaptive(Number(value), axisSpan, 2, 5),
            },
            nameTextStyle: { color: '#8ea3bc' },
          },
          tooltip: {
            trigger: 'axis',
            backgroundColor: '#132237',
            borderColor: '#2a4f78',
            textStyle: { color: '#dce8fb' },
            formatter: (params: Array<{ data?: [number, number] }>) => {
              const p = params?.[0]?.data;
              if (!p) return '';
              return `받음각 ${trimTrailingZeros(Number(p[0]).toFixed(1))}도<br/>${yName}: ${fmtAdaptive(Number(p[1]), axisSpan, 3, 6)}`;
            },
          },
          series: [
            {
              type: 'line',
              data: points,
              smooth: false,
              showSymbol: false,
              lineStyle: { width: 2.6, color },
              areaStyle: { color: `${color}24` },
            },
          ],
        }}
        style={{ width: '100%', height: 250 }}
      />
    </div>
  );
}
