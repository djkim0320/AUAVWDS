import { useEffect, useMemo, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import type { AnalysisState, ExportFormat, WingState, WingtipStyle } from '../types';

type Props = {
  wing: WingState;
  analysis: AnalysisState;
  onExportCfd: (format: ExportFormat) => Promise<void>;
  isExporting: boolean;
};

const TXT_PREVIEW_RENDER = '프리뷰 렌더링';
const TXT_EXPORT_LIMIT = 'VSP3 내보내기는 실제 OpenVSP 결과가 있을 때만 제공됩니다.';
const TXT_OPENVSP_LINKED = 'OpenVSP 결과 연동';
const TXT_NEURALFOIL_LINKED = 'NeuralFoil 결과 연동';
const TXT_FALLBACK_RESULT = '대체 해석 결과';
const TXT_NO_VSP3_FOR_FALLBACK = '실제 OpenVSP 결과가 아니어서 VSP3 내보내기를 사용할 수 없습니다.';
const TXT_OPENVSP_WITH_VSP3 = 'VSP3 파일이 포함된 실제 OpenVSP 결과입니다.';
const TXT_OPENVSP_NO_VSP3 = '실제 OpenVSP 결과이지만 VSP3 파일을 찾을 수 없습니다.';
const TXT_NEURALFOIL_NOTE = 'NeuralFoil은 2D 에어포일 polar 기반의 날개 추정 결과를 제공합니다.';
const TXT_EXPORTING = '내보내는 중...';
const TXT_EXPORT = '내보내기';
const TXT_EMPTY_WING = '아직 날개 3D 모델이 없습니다. 채팅에서 설계를 요청해 주세요.';

function wingtipStyleLabel(style: WingtipStyle): string {
  return style === 'pinched' ? '조임형' : '직선형';
}

export default function Wing3DTab({ wing, analysis, onExportCfd, isExporting }: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [exportFormat, setExportFormat] = useState<ExportFormat>('obj');

  const precisionMeta = useMemo(() => {
    const openvspResult = analysis.results.openvsp;
    const activeResult = analysis.results[analysis.active_solver] || openvspResult || analysis.results.neuralfoil;

    if (!activeResult) {
      return {
        analysisMode: null,
        linked: false,
        canExportVsp3: false,
        pillText: TXT_PREVIEW_RENDER,
        note: TXT_EXPORT_LIMIT,
      };
    }

    const result = activeResult;
    const extra = result.extra_data as Record<string, unknown>;
    const solverMode = typeof extra?.solver_mode === 'string' ? extra.solver_mode : '';
    const openvspExtra = (openvspResult?.extra_data || {}) as Record<string, unknown>;
    const hasVsp3 =
      openvspResult?.analysis_mode === 'openvsp' &&
      typeof openvspExtra?.vsp3_path === 'string' &&
      openvspExtra.vsp3_path.length > 0;
    const linked = result.analysis_mode === 'openvsp' && (solverMode === 'openvsp-script' || hasVsp3);

    return {
      analysisMode: result.analysis_mode,
      linked,
      canExportVsp3: hasVsp3,
      pillText:
        result.analysis_mode === 'openvsp'
          ? TXT_OPENVSP_LINKED
          : result.analysis_mode === 'neuralfoil'
            ? TXT_NEURALFOIL_LINKED
            : TXT_FALLBACK_RESULT,
      note:
        result.analysis_mode === 'fallback'
          ? (result.fallback_reason || TXT_NO_VSP3_FOR_FALLBACK)
          : result.analysis_mode === 'neuralfoil'
            ? TXT_NEURALFOIL_NOTE
            : (hasVsp3 ? TXT_OPENVSP_WITH_VSP3 : TXT_OPENVSP_NO_VSP3),
    };
  }, [analysis.active_solver, analysis.results]);

  useEffect(() => {
    if (exportFormat === 'vsp3' && !precisionMeta.canExportVsp3) {
      setExportFormat('obj');
    }
  }, [exportFormat, precisionMeta.canExportVsp3]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color('#040a14');

    const camera = new THREE.PerspectiveCamera(52, host.clientWidth / host.clientHeight, 0.01, 1000);
    camera.position.set(1.9, 1.1, 2.4);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(host.clientWidth, host.clientHeight);
    host.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.06;
    controls.target.set(0, 0.05, 0);

    const grid = new THREE.GridHelper(12, 20, 0x2b5079, 0x14314d);
    grid.position.y = -0.05;
    scene.add(grid);

    const fill = new THREE.DirectionalLight(0xffffff, 1.15);
    fill.position.set(5, 5, 4);
    scene.add(fill);
    scene.add(new THREE.AmbientLight(0x7b9cc0, 0.5));

    let mesh: THREE.Mesh | null = null;
    const preview = wing.preview_mesh;
    if (preview && preview.vertices.length > 0 && preview.triangles.length > 0) {
      const geom = new THREE.BufferGeometry();
      const vertices = new Float32Array(preview.vertices.flat());
      const indices = new Uint32Array(preview.triangles.flat());
      geom.setAttribute('position', new THREE.BufferAttribute(vertices, 3));
      geom.setIndex(new THREE.BufferAttribute(indices, 1));
      // Backend mesh uses Y as span axis; rotate to render wing in horizontal attitude (Y-up scene).
      geom.rotateX(-Math.PI / 2);
      geom.computeVertexNormals();
      geom.computeBoundingBox();

      const material = new THREE.MeshStandardMaterial({
        color: 0x1977ff,
        roughness: 0.3,
        metalness: 0.08,
        side: THREE.DoubleSide,
      });

      mesh = new THREE.Mesh(geom, material);
      scene.add(mesh);

      const box = new THREE.Box3().setFromObject(mesh);
      const size = box.getSize(new THREE.Vector3());
      const center = box.getCenter(new THREE.Vector3());
      mesh.position.sub(center);

      camera.position.set(size.x * 1.65 + 0.8, size.y * 1.8 + 0.65, size.z * 1.55 + 0.9);
      controls.target.set(0, 0, 0);
      controls.update();
    }

    let raf = 0;
    const loop = () => {
      raf = requestAnimationFrame(loop);
      controls.update();
      renderer.render(scene, camera);
    };
    loop();

    const onResize = () => {
      camera.aspect = host.clientWidth / host.clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(host.clientWidth, host.clientHeight);
    };
    window.addEventListener('resize', onResize);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', onResize);
      controls.dispose();
      renderer.dispose();
      host.removeChild(renderer.domElement);
      scene.clear();
    };
  }, [wing.preview_mesh]);

  return (
    <div className="canvas-workspace wing-ui">
      <div className="panel-title-row">
        <div className="panel-title">날개 3D</div>
        <div className="wing-toolbar-actions">
          <div className="export-controls">
            <select
              value={exportFormat}
              disabled={isExporting}
              onChange={(e) => setExportFormat(e.target.value as ExportFormat)}
            >
              <option value="obj">OBJ</option>
              <option value="json">JSON</option>
              {precisionMeta.canExportVsp3 && <option value="vsp3">VSP3</option>}
            </select>
            <button className="ghost" disabled={isExporting} onClick={() => void onExportCfd(exportFormat)}>
              {isExporting ? TXT_EXPORTING : TXT_EXPORT}
            </button>
          </div>
          <span className={`solver-pill ${precisionMeta.linked ? 'ok' : precisionMeta.analysisMode === 'fallback' ? 'warn' : ''}`}>
            {precisionMeta.pillText}
          </span>
        </div>
      </div>

      <div className={`solver-note ${precisionMeta.analysisMode === 'fallback' ? 'fallback' : ''}`}>
        {precisionMeta.note}
      </div>

      <div className="wing-meta">
        <span>스팬: {wing.params.span_m.toFixed(2)}m</span>
        <span>세장비: {wing.params.aspect_ratio.toFixed(1)}</span>
        <span>스윕: {wing.params.sweep_deg.toFixed(1)}도</span>
        <span>윙팁: {wingtipStyleLabel(wing.params.wingtip_style)}</span>
      </div>

      <div className="three-host" ref={hostRef}>
        {!wing.preview_mesh && <div className="empty-state">{TXT_EMPTY_WING}</div>}
        <div className="scale-overlay">
          <div className="scale-line"></div>
          <div className="scale-text">0.2m</div>
        </div>
      </div>
    </div>
  );
}
