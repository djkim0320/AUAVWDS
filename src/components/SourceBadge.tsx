export default function SourceBadge({ label, mode }: { label?: string; mode?: string }) {
  const text = label || '정밀해석(OpenVSP+VSPAERO)';
  return <span className={`source-badge ${mode || 'precision'}`}>{text}</span>;
}
