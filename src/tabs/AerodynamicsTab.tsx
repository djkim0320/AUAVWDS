import ReactECharts from 'echarts-for-react';
import SourceBadge from '../components/SourceBadge';
import type { AnalysisResult, AnalysisState } from '../types';

function clamp(v: number, lo: number, hi: number) {
  return Math.min(hi, Math.max(lo, v));
}

function fmt(v?: number | null, d = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-';
  return Number(v).toFixed(d);
}

function fmtInt(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-';
  return Math.round(Number(v)).toLocaleString('en-US');
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
  cliw: '와류 유도 양력 CLiw',
  cdiw: '와류 유도 항력 CDiw',
  lodwake: '와류 양항비 LoDwake',
  ewake: '와류 효율 Ewake',
  t_qs: '추력계수 T/QS',
  l2res: '수치 잔차 L2Res',
  maxres: '최대 잔차 MaxRes',
  wall_time: '해석 시간 Wall Time',
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

export default function AerodynamicsTab({ analysis }: { analysis: AnalysisState }) {
  const result: AnalysisResult | null = analysis.precision_result;

  if (!result) {
    return (
      <div className="canvas-workspace">
        <div className="panel-title">Aerodynamics</div>
        <div className="empty-state">아직 공력 데이터가 없어요. 채팅에서 해석을 요청해 주세요.</div>
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

  const aoaPlotMin = -10;
  const aoaPlotMax = 20;
  const plotIndices = aoa
    .map((a, idx) => ((a >= aoaPlotMin && a <= aoaPlotMax) ? idx : -1))
    .filter((idx) => idx >= 0);
  const aoaPlot = plotIndices.map((i) => aoa[i]);
  const clPlot = plotIndices.map((i) => c.cl[i]);
  const cdPlot = plotIndices.map((i) => c.cd[i]);
  const ldPlot = plotIndices.map((i) => clamp(ld[i], -200, 200));

  const chips = [
    { k: 'CD min', v: fmt(m?.cd_min, 4) },
    { k: '지구력 파라미터', v: fmt(m?.endurance_param, 1) },
    { k: 'Oswald e', v: fmt(m?.oswald_e, 2) },
    { k: 'Re', v: fmtInt(m?.reynolds) },
  ];

  const precisionData = (result.extra_data?.precision_data as Record<string, unknown>) || null;
  const allData = (result.extra_data?.vspaero_all_data as Record<string, unknown>) || null;
  const vspaeroRows: Array<{ key: string; label: string; value: string }> = [];
  if (allData && typeof allData === 'object') {
    Object.entries(allData)
      .sort(([a], [b]) => a.localeCompare(b))
      .forEach(([k, v]) => {
        const n = toNumber(v);
        if (n === null) return;
        const digits = Math.abs(n) < 1 ? 5 : 4;
        vspaeroRows.push({ key: k, label: humanizeVspaeroKey(k), value: fmt(n, digits) });
      });
  } else if (precisionData && typeof precisionData === 'object') {
    Object.entries(precisionData).forEach(([k, v]) => {
      const n = toNumber(v);
      if (n === null) return;
      const digits = Math.abs(n) < 1 ? 5 : 4;
      vspaeroRows.push({ key: k, label: humanizeVspaeroKey(k), value: fmt(n, digits) });
    });
  }

  return (
    <div className="canvas-workspace aero-ui">
      <div className="panel-title-row">
        <div className="panel-title">공력 해석 결과</div>
        <SourceBadge label={result.source_label} mode={result.analysis_mode} />
      </div>

      {result.analysis_mode === 'fallback' && (
        <div className="analysis-alert fallback">
          실제 OpenVSP/VSPAERO 결과가 아니라 근사 해석 결과입니다.
          {result.fallback_reason ? ` 사유: ${result.fallback_reason}` : ''}
        </div>
      )}

      <div className="aero-cards">
        <Metric title="최대 양항비 (L/D)" value={fmt(m?.ld_max, 1)} desc="높을수록 효율적" emphasize />
        <Metric title="최적 받음각" value={`${fmt(m?.ld_max_aoa, 1)}°`} desc="L/D 최대 지점" />
        <Metric title="실속 각도" value={`${fmt(m?.cl_max_aoa, 1)}°`} desc="CL max 지점" />
        <Metric title="최대 CL" value={fmt(m?.cl_max, 3)} desc="최대 양력계수" />
      </div>

      <div className="metric-chip-row">
        {chips.map((chip) => (
          <span key={chip.k} className="metric-chip">{chip.k} <strong>{chip.v}</strong></span>
        ))}
      </div>

      <div className="chart-grid">
        <Chart title="양력계수 (CL)" x={aoaPlot} y={clPlot} color="#70bbff" yName="CL" />
        <Chart title="양항비 (L/D)" x={aoaPlot} y={ldPlot} color="#efb35b" yName="L/D" />
        <Chart title="항력계수 (CD)" x={aoaPlot} y={cdPlot} color="#6ce8be" yName="CD" />
      </div>

      <div className="aero-detail-grid">
        <section className="detail-card">
          <h4>양력 특성</h4>
          <div className="kv"><span>양력곡선 기울기 (CLα)</span><strong>{fmt(m?.cl_alpha, 2)} /rad</strong></div>
          <div className="kv"><span>영양력 받음각 (α0)</span><strong>{fmt(m?.alpha_zero_lift, 2)}°</strong></div>
        </section>

        <section className="detail-card">
          <h4>안정성 / 모멘트</h4>
          <div className="kv"><span>Cm @ 영양력</span><strong>{fmt(m?.cm_zero_lift, 5)}</strong></div>
          <div className="kv"><span>Cm 기울기 (Cmα)</span><strong>{fmt(m?.cm_alpha, 4)} /rad</strong></div>
        </section>

        <section className="detail-card">
          <h4>항력 특성</h4>
          <div className="kv"><span>영항력 항력 (CD0)</span><strong>{fmt(m?.cd_zero, 4)}</strong></div>
          <div className="kv"><span>유도항력 효율 (e)</span><strong>{fmt(m?.oswald_e, 3)}</strong></div>
        </section>
      </div>

      <section className="vsp-extra-card">
        <h4>VSPAERO 전체 데이터</h4>
        <div className="vsp-extra-grid">
          {vspaeroRows.length === 0 && <div className="muted">표시할 VSPAERO 데이터가 없습니다.</div>}
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

function Chart({ title, x, y, color, yName }: { title: string; x: number[]; y: number[]; color: string; yName: string }) {
  if (!x.length || !y.length) {
    return (
      <div className="chart-card">
        <div className="chart-title">{title}</div>
        <div className="muted">데이터 없음</div>
      </div>
    );
  }

  const points = x.map((v, i) => [toNumber(v) ?? 0, toNumber(y[i]) ?? 0]);
  const xMin = -10;
  const xMax = 20;

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
            name: '받음각 (°)',
            min: xMin,
            max: xMax,
            splitNumber: 6,
            interval: 5,
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
            formatter: (params: any) => {
              const p = params?.[0]?.data;
              if (!p) return '';
              return `받음각: ${trimTrailingZeros(Number(p[0]).toFixed(1))}°<br/>${yName}: ${fmtAdaptive(Number(p[1]), axisSpan, 3, 6)}`;
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
