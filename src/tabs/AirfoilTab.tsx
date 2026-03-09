import { useEffect, useMemo, useState } from 'react';
import type { AirfoilState } from '../types';

type Props = {
  airfoil: AirfoilState;
  onApplyCustom: (custom: {
    max_camber_percent: number;
    max_camber_x_percent: number;
    thickness_percent: number;
    reflex_percent: number;
  }) => Promise<void>;
  isApplying: boolean;
};

type Pt = [number, number];
const SX = 800;
const SY = 820;
const OX = 100;
const OY = 0;

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

function buildNacaLike(maxCamberPercent: number, camberPosPercent: number, thicknessPercent: number, samples = 220) {
  const m = clamp(maxCamberPercent, 0, 9) / 100;
  const p = clamp(camberPosPercent, 5, 90) / 100;
  const t = clamp(thicknessPercent, 4, 24) / 100;

  const xArr: number[] = [];
  const ycRawArr: number[] = [];
  const dycArr: number[] = [];

  for (let i = 0; i < samples; i += 1) {
    const beta = (Math.PI * i) / (samples - 1);
    const x = (1 - Math.cos(beta)) / 2;
    xArr.push(x);

    let ycRaw = 0;
    let dyc = 0;
    if (m > 0 && p > 0) {
      if (x < p) {
        ycRaw = (m / (p * p)) * (2 * p * x - x * x);
        dyc = (2 * m / (p * p)) * (p - x);
      } else {
        ycRaw = (m / ((1 - p) * (1 - p))) * ((1 - 2 * p) + 2 * p * x - x * x);
        dyc = (2 * m / ((1 - p) * (1 - p))) * (p - x);
      }
    }
    ycRawArr.push(ycRaw);
    dycArr.push(dyc);
  }

  // Keep an implicit anchor at camber-line center (x=0.5) instead of locking to the chord line.
  const centerIdx = xArr.reduce((best, x, idx) => (Math.abs(x - 0.5) < Math.abs(xArr[best] - 0.5) ? idx : best), 0);
  const centerOffset = ycRawArr[centerIdx] ?? 0;

  const upper: Pt[] = [];
  const lower: Pt[] = [];
  const camber: Pt[] = [];

  for (let i = 0; i < samples; i += 1) {
    const x = xArr[i];

    const yt =
      5 *
      t *
      (0.2969 * Math.sqrt(Math.max(x, 1e-9)) -
        0.1260 * x -
        0.3516 * x * x +
        0.2843 * x * x * x -
        0.1015 * x * x * x * x);

    const yc = ycRawArr[i] - centerOffset;
    const dyc = dycArr[i];

    const theta = Math.atan(dyc);
    const xu = x - yt * Math.sin(theta);
    const yu = yc + yt * Math.cos(theta);
    const xl = x + yt * Math.sin(theta);
    const yl = yc - yt * Math.cos(theta);

    upper.push([xu, yu]);
    lower.push([xl, yl]);
    camber.push([x, yc]);
  }

  return { upper, lower, camber };
}

function pathFromPoints(points: Pt[]) {
  return points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p[0] * SX + OX} ${-p[1] * SY + OY}`).join(' ');
}

function toCanvasPoint(p: Pt): Pt {
  return [p[0] * SX + OX, -p[1] * SY + OY];
}

export default function AirfoilTab({ airfoil, onApplyCustom, isApplying }: Props) {
  const initialThickness = airfoil.summary.thickness_percent || 12;
  const initialCamber = airfoil.summary.max_camber_percent || 2;
  const initialCamberPos = airfoil.summary.max_camber_x_percent || 40;

  const [thickness, setThickness] = useState(initialThickness);
  const [camber, setCamber] = useState(initialCamber);
  const [camberPos, setCamberPos] = useState(initialCamberPos);

  useEffect(() => {
    setThickness(initialThickness);
    setCamber(initialCamber);
    setCamberPos(initialCamberPos);
  }, [initialThickness, initialCamber, initialCamberPos]);

  const preview = useMemo(() => buildNacaLike(camber, camberPos, thickness), [camber, camberPos, thickness]);
  const chordStart = preview.camber[0] ?? [0, 0];
  const chordEnd = preview.camber[preview.camber.length - 1] ?? [1, 0];
  const [chordX1, chordY1] = toCanvasPoint(chordStart);
  const [chordX2, chordY2] = toCanvasPoint(chordEnd);

  return (
    <div className="canvas-workspace airfoil-ui">
      <div className="panel-title">에어포일</div>

      <div className="airfoil-controls">
        <Slider
          label="두께(%)"
          min={6}
          max={20}
          step={0.1}
          value={thickness}
          onChange={setThickness}
        />
        <Slider
          label="캠버(%)"
          min={0}
          max={8}
          step={0.1}
          value={camber}
          onChange={setCamber}
        />
        <Slider
          label="캠버 위치(%c)"
          min={10}
          max={80}
          step={1}
          value={camberPos}
          onChange={setCamberPos}
        />
        <div className="airfoil-apply-actions">
          <button
            className="primary"
            disabled={isApplying}
            onClick={() =>
              void onApplyCustom({
                max_camber_percent: camber,
                max_camber_x_percent: camberPos,
                thickness_percent: thickness,
                reflex_percent: 0,
              })
            }
          >
            {isApplying ? '적용 중...' : '커스텀 적용'}
          </button>
          <button
            className="ghost"
            disabled={isApplying}
            onClick={() => {
              setThickness(initialThickness);
              setCamber(initialCamber);
              setCamberPos(initialCamberPos);
            }}
          >
            초기화
          </button>
        </div>
      </div>

      <div className="airfoil-metrics">
        <div>
          <label>형상</label>
          <strong>{airfoil.summary.code || '커스텀 에어포일'}</strong>
        </div>
        <div>
          <label>두께</label>
          <strong>{thickness.toFixed(2)}%</strong>
        </div>
        <div>
          <label>최대 캠버</label>
          <strong>{camber.toFixed(2)}%</strong>
        </div>
        <div>
          <label>캠버 위치</label>
          <strong>{camberPos.toFixed(1)}% c</strong>
        </div>
      </div>

      <div className="svg-wrap">
        <svg viewBox="-140 -330 1280 660" preserveAspectRatio="xMidYMid meet">
          <line x1={chordX1} y1={chordY1} x2={chordX2} y2={chordY2} stroke="#19314a" strokeWidth={1} />
          <path d={pathFromPoints(preview.upper)} fill="none" stroke="#7dc1ff" strokeWidth={2.6} />
          <path d={pathFromPoints(preview.lower)} fill="none" stroke="#7dc1ff" strokeWidth={2.6} />
          <path d={pathFromPoints(preview.camber)} fill="none" stroke="#f4b862" strokeWidth={1.5} strokeDasharray="6 5" />
        </svg>
      </div>
    </div>
  );
}

type SliderProps = {
  label: string;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (v: number) => void;
};

function Slider({ label, min, max, step, value, onChange }: SliderProps) {
  return (
    <div className="slider-control">
      <div className="slider-label-row">
        <span>{label}</span>
        <span>{value.toFixed(step >= 1 ? 0 : 1)}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}
