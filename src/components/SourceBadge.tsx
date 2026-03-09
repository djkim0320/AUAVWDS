import type { AnalysisMode } from '../types';

const DEFAULT_PRECISION_LABEL = '\uc815\ubc00 \ud574\uc11d(OpenVSP/VSPAERO)';

export default function SourceBadge({ label, mode }: { label?: string; mode?: AnalysisMode | 'precision' | string }) {
  const text = label || DEFAULT_PRECISION_LABEL;
  const badgeMode = mode === 'fallback' ? 'fallback' : 'precision';
  return <span className={`source-badge ${badgeMode}`}>{text}</span>;
}
