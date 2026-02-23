import { useEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import type { AnalysisState, WingState } from '../types';

type Props = {
  wing: WingState;
  analysis: AnalysisState;
  onExportCfd: () => Promise<void>;
  isExporting: boolean;
};

export default function Wing3DTab({ wing, analysis, onExportCfd, isExporting }: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);

  const precisionMeta = useMemo(() => {
    if (!analysis.precision_result) return null;
    const extra = analysis.precision_result.extra_data as Record<string, unknown>;
    const solverMode = typeof extra?.solver_mode === 'string' ? extra.solver_mode : '';
    const linked = solverMode === 'openvsp-script' || Boolean(extra?.vsp3_path);
    return { linked };
  }, [analysis.precision_result]);

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
        <div className="panel-title">Wing 3D</div>
        <div className="wing-toolbar-actions">
          <button className="ghost" disabled={isExporting} onClick={() => void onExportCfd()}>
            {isExporting ? '내보내는 중...' : 'CFD 내보내기'}
          </button>
          <span className={`solver-pill ${precisionMeta?.linked ? 'ok' : ''}`}>
            {precisionMeta?.linked ? 'OpenVSP 연동 렌더링' : '프리뷰 렌더링'}
          </span>
        </div>
      </div>

      <div className="wing-meta">
        <span>Span: {wing.params.span_m.toFixed(2)}m</span>
        <span>AR: {wing.params.aspect_ratio.toFixed(1)}</span>
        <span>Sweep: {wing.params.sweep_deg.toFixed(1)}°</span>
      </div>

      <div className="three-host" ref={hostRef}>
        {!wing.preview_mesh && <div className="empty-state">아직 날개 3D 모델이 없어요. 채팅에서 설계를 요청해 주세요.</div>}
        <div className="scale-overlay">
          <div className="scale-line"></div>
          <div className="scale-text">0.2m</div>
        </div>
      </div>
    </div>
  );
}
