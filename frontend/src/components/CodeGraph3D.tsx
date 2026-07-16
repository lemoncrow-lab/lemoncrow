import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { Focus } from "lucide-react";
import type { CodeMapActivityKind, CodeMapEdge, CodeMapNode } from "../api";
import {
  ACTIVITY_COLORS,
  KIND_COLORS,
  stableUnit,
  vivid,
} from "../lib/graphColors";

type P3 = {
  id: string;
  label: string;
  qualified: string;
  path: string;
  line: number;
  community: string;
  degree: number;
  raw: string;
  x: number;
  y: number;
  z: number;
};

const GOLDEN = Math.PI * (3 - Math.sqrt(5));

function hexToRgb(hex: string): [number, number, number] {
  const match = /^#?([\da-f]{6})$/i.exec(hex.trim());
  if (!match) return [0.64, 0.64, 0.64];
  const int = parseInt(match[1], 16);
  return [
    ((int >> 16) & 255) / 255,
    ((int >> 8) & 255) / 255,
    (int & 255) / 255,
  ];
}

// Tight clustered layout, computed once (no physics): each group is packed into
// its own small sphere, groups spread over an outer shell. Fast and readable
// instead of one diffuse cloud.
function layout(nodes: P3[]): void {
  const groups = new Map<string, P3[]>();
  for (const node of nodes) {
    const key = node.community || "root";
    const bucket = groups.get(key);
    if (bucket) bucket.push(node);
    else groups.set(key, [node]);
  }
  const names = [...groups.keys()].sort();
  const shell = 28 + Math.sqrt(nodes.length) * 0.32;
  names.forEach((name, gi) => {
    const members = groups.get(name) ?? [];
    const t = names.length <= 1 ? 0.5 : gi / (names.length - 1);
    const gy = 1 - t * 2;
    const gring = Math.sqrt(Math.max(0, 1 - gy * gy));
    const gtheta = GOLDEN * gi;
    const cx = Math.cos(gtheta) * gring * shell;
    const cy = gy * shell;
    const cz = Math.sin(gtheta) * gring * shell;
    const clusterR = 5 + Math.cbrt(members.length) * 3.8;
    members.forEach((node, li) => {
      const mt = (li + 0.5) / members.length;
      const my = 1 - mt * 2;
      const mring = Math.sqrt(Math.max(0, 1 - my * my));
      const mtheta = GOLDEN * li;
      const rad = clusterR * Math.cbrt(mt) * (0.6 + stableUnit(node.id) * 0.4);
      node.x = cx + Math.cos(mtheta) * mring * rad;
      node.y = cy + my * rad;
      node.z = cz + Math.sin(mtheta) * mring * rad;
    });
  });
  // recenter the whole cloud on the origin so the initial fit is centered
  let mx = 0;
  let my = 0;
  let mz = 0;
  for (const node of nodes) {
    mx += node.x;
    my += node.y;
    mz += node.z;
  }
  const inv = 1 / Math.max(1, nodes.length);
  for (const node of nodes) {
    node.x -= mx * inv;
    node.y -= my * inv;
    node.z -= mz * inv;
  }
}

export default function CodeGraph3D({
  nodes,
  edges,
  selectedId,
  activityByNode,
  followNodeId,
  onSelect,
  onExpand,
}: {
  nodes: CodeMapNode[];
  edges: CodeMapEdge[];
  selectedId: string | null;
  activityByNode: Record<string, CodeMapActivityKind>;
  followNodeId: string | null;
  onSelect: (nodeId: string | null) => void;
  onExpand: (nodeId: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const tooltipRef = useRef<HTMLDivElement | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const pointsRef = useRef<THREE.Points | null>(null);
  const incidentRef = useRef<THREE.LineSegments | null>(null);
  const globalEdgesRef = useRef<THREE.LineSegments | null>(null);
  const dataRef = useRef<P3[]>([]);
  const indexRef = useRef<Map<string, number>>(new Map());
  const lightRef = useRef(document.documentElement.classList.contains("light"));
  const selectedRef = useRef<string | null>(selectedId);
  const neighborsRef = useRef<Set<string>>(new Set());
  const activityRef = useRef(activityByNode);
  const onSelectRef = useRef(onSelect);
  const onExpandRef = useRef(onExpand);
  onSelectRef.current = onSelect;
  onExpandRef.current = onExpand;
  const [failed, setFailed] = useState(false);
  const [count, setCount] = useState(0);

  const themeBg = () => (lightRef.current ? 0xf4f4f5 : 0x0a0a0a);

  // rewrite the node color buffer from current selection/activity/theme state
  const paint = () => {
    const points = pointsRef.current;
    const data = dataRef.current;
    if (!points || !data.length) return;
    const colorAttr = points.geometry.getAttribute(
      "color"
    ) as THREE.BufferAttribute;
    const sizeAttr = points.geometry.getAttribute(
      "size"
    ) as THREE.BufferAttribute;
    const light = lightRef.current;
    const selected = selectedRef.current;
    const arr = colorAttr.array as Float32Array;
    const sizes = sizeAttr.array as Float32Array;
    for (let i = 0; i < data.length; i += 1) {
      const node = data[i];
      let hex: string;
      let scale = 1;
      const kind = activityRef.current[node.id];
      if (kind) {
        hex = ACTIVITY_COLORS[kind][light ? 1 : 0];
        scale = 1.8;
      } else if (selected && node.id === selected) {
        hex = light ? "#7c3aed" : "#ddd6fe";
        scale = 2.2;
      } else if (selected && !neighborsRef.current.has(node.id)) {
        hex = light ? "#d4d4d4" : "#2f2f2f";
      } else {
        hex = vivid(node.raw, light);
      }
      const [r, g, b] = hexToRgb(hex);
      arr[i * 3] = r;
      arr[i * 3 + 1] = g;
      arr[i * 3 + 2] = b;
      sizes[i] = (2 + Math.sqrt(node.degree) * 0.9) * scale;
    }
    colorAttr.needsUpdate = true;
    sizeAttr.needsUpdate = true;
  };

  // rebuild the highlighted caller/callee lines for the current selection
  const paintIncident = () => {
    const scene = sceneRef.current;
    if (!scene) return;
    if (incidentRef.current) {
      scene.remove(incidentRef.current);
      incidentRef.current.geometry.dispose();
      (incidentRef.current.material as THREE.Material).dispose();
      incidentRef.current = null;
    }
    const selected = selectedRef.current;
    if (!selected) return;
    const index = indexRef.current;
    const data = dataRef.current;
    const positions: number[] = [];
    const colors: number[] = [];
    const light = lightRef.current;
    const callee = hexToRgb(light ? "#0e7490" : "#22d3ee");
    const caller = hexToRgb(light ? "#6d28d9" : "#a78bfa");
    for (const edge of edges) {
      if (edge.kind !== "calls") continue;
      const out = edge.source === selected;
      const inc = edge.target === selected;
      if (!out && !inc) continue;
      const a = index.get(edge.source);
      const b = index.get(edge.target);
      if (a === undefined || b === undefined) continue;
      const na = data[a];
      const nb = data[b];
      positions.push(na.x, na.y, na.z, nb.x, nb.y, nb.z);
      const c = out ? callee : caller;
      colors.push(c[0], c[1], c[2], c[0], c[1], c[2]);
    }
    if (!positions.length) return;
    const geo = new THREE.BufferGeometry();
    geo.setAttribute(
      "position",
      new THREE.Float32BufferAttribute(positions, 3)
    );
    geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
    const lines = new THREE.LineSegments(
      geo,
      new THREE.LineBasicMaterial({
        vertexColors: true,
        transparent: true,
        opacity: 0.85,
      })
    );
    incidentRef.current = lines;
    scene.add(lines);
  };

  // faint grey lines for every resolved call, drawn behind the points
  const buildGlobalEdges = () => {
    const scene = sceneRef.current;
    if (!scene) return;
    if (globalEdgesRef.current) {
      scene.remove(globalEdgesRef.current);
      globalEdgesRef.current.geometry.dispose();
      (globalEdgesRef.current.material as THREE.Material).dispose();
      globalEdgesRef.current = null;
    }
    const index = indexRef.current;
    const data = dataRef.current;
    const positions: number[] = [];
    for (const edge of edges) {
      if (edge.kind !== "calls") continue;
      const a = index.get(edge.source);
      const b = index.get(edge.target);
      if (a === undefined || b === undefined) continue;
      const na = data[a];
      const nb = data[b];
      positions.push(na.x, na.y, na.z, nb.x, nb.y, nb.z);
    }
    if (!positions.length) return;
    const light = lightRef.current;
    const geo = new THREE.BufferGeometry();
    geo.setAttribute(
      "position",
      new THREE.Float32BufferAttribute(positions, 3)
    );
    const lines = new THREE.LineSegments(
      geo,
      new THREE.LineBasicMaterial({
        color: light ? 0x9ca3af : 0x6b7280,
        transparent: true,
        opacity: light ? 0.26 : 0.36,
      })
    );
    lines.renderOrder = -1;
    globalEdgesRef.current = lines;
    scene.add(lines);
  };

  // create the scene once
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    THREE.ColorManagement.enabled = false;
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 20000);
    camera.position.set(0, 0, 600);
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    } catch {
      setFailed(true);
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor(themeBg(), 1);
    container.appendChild(renderer.domElement);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.12;
    controls.zoomToCursor = true;
    sceneRef.current = scene;
    cameraRef.current = camera;
    rendererRef.current = renderer;
    controlsRef.current = controls;

    const resize = () => {
      const w = container.clientWidth;
      const h = container.clientHeight;
      renderer.setSize(w, h);
      camera.aspect = w / Math.max(1, h);
      camera.updateProjectionMatrix();
    };
    resize();
    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(container);

    const themeObserver = new MutationObserver(() => {
      lightRef.current = document.documentElement.classList.contains("light");
      renderer.setClearColor(themeBg(), 1);
      const ge = globalEdgesRef.current;
      if (ge) {
        const m = ge.material as THREE.LineBasicMaterial;
        m.color.setHex(lightRef.current ? 0x9ca3af : 0x6b7280);
        m.opacity = lightRef.current ? 0.26 : 0.36;
      }
      paint();
      paintIncident();
    });
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });

    // picking
    const raycaster = new THREE.Raycaster();
    raycaster.params.Points = { threshold: 3.5 };
    const pointer = new THREE.Vector2();
    let downX = 0;
    let downY = 0;
    const pick = (clientX: number, clientY: number): number | null => {
      const points = pointsRef.current;
      if (!points) return null;
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hits = raycaster.intersectObject(points);
      if (!hits.length) return null;
      // pick the point nearest the cursor ray (not just the frontmost within
      // the threshold), so the selected node is the one under the pointer
      let best = hits[0];
      for (const hit of hits) {
        if ((hit.distanceToRay ?? Infinity) < (best.distanceToRay ?? Infinity))
          best = hit;
      }
      return best.index ?? null;
    };
    const onDown = (event: PointerEvent) => {
      downX = event.clientX;
      downY = event.clientY;
    };
    const onUp = (event: PointerEvent) => {
      if (Math.hypot(event.clientX - downX, event.clientY - downY) > 5) return;
      const idx = pick(event.clientX, event.clientY);
      if (idx === null) onSelectRef.current(null);
      else onSelectRef.current(dataRef.current[idx].id);
    };
    const onDouble = (event: MouseEvent) => {
      const idx = pick(event.clientX, event.clientY);
      if (idx !== null) onExpandRef.current(dataRef.current[idx].id);
    };
    const onMove = (event: PointerEvent) => {
      const tip = tooltipRef.current;
      if (!tip) return;
      const idx = pick(event.clientX, event.clientY);
      if (idx === null) {
        tip.style.display = "none";
        return;
      }
      const node = dataRef.current[idx];
      const rect = container.getBoundingClientRect();
      tip.style.display = "block";
      tip.style.left = `${event.clientX - rect.left + 12}px`;
      tip.style.top = `${event.clientY - rect.top + 12}px`;
      tip.textContent = `${node.label} — ${node.path}:${node.line}`;
    };
    renderer.domElement.addEventListener("pointerdown", onDown);
    renderer.domElement.addEventListener("pointerup", onUp);
    renderer.domElement.addEventListener("dblclick", onDouble);
    renderer.domElement.addEventListener("pointermove", onMove);

    let raf = 0;
    const animate = () => {
      raf = requestAnimationFrame(animate);
      controls.update();
      renderer.render(scene, camera);
    };
    animate();

    return () => {
      cancelAnimationFrame(raf);
      resizeObserver.disconnect();
      themeObserver.disconnect();
      renderer.domElement.removeEventListener("pointerdown", onDown);
      renderer.domElement.removeEventListener("pointerup", onUp);
      renderer.domElement.removeEventListener("dblclick", onDouble);
      renderer.domElement.removeEventListener("pointermove", onMove);
      controls.dispose();
      renderer.dispose();
      if (renderer.domElement.parentElement === container)
        container.removeChild(renderer.domElement);
      pointsRef.current = null;
      sceneRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // (re)build the point cloud when data changes
  useEffect(() => {
    const scene = sceneRef.current;
    const camera = cameraRef.current;
    const controls = controlsRef.current;
    if (!scene || !camera || !controls) return;

    if (pointsRef.current) {
      scene.remove(pointsRef.current);
      pointsRef.current.geometry.dispose();
      (pointsRef.current.material as THREE.Material).dispose();
      pointsRef.current = null;
    }

    const data: P3[] = nodes.map((node) => ({
      id: node.id,
      label: node.label,
      qualified: node.qualified_name,
      path: node.path,
      line: node.line,
      community: node.community || "root",
      degree: node.degree ?? 0,
      raw:
        node.color ||
        (node.focus ? "#c4b5fd" : (KIND_COLORS[node.kind] ?? "#a3a3a3")),
      x: 0,
      y: 0,
      z: 0,
    }));
    layout(data);
    dataRef.current = data;
    indexRef.current = new Map(data.map((node, i) => [node.id, i]));
    setCount(data.length);

    const positions = new Float32Array(data.length * 3);
    const colors = new Float32Array(data.length * 3);
    const sizes = new Float32Array(data.length);
    const light = lightRef.current;
    for (let i = 0; i < data.length; i += 1) {
      positions[i * 3] = data[i].x;
      positions[i * 3 + 1] = data[i].y;
      positions[i * 3 + 2] = data[i].z;
      const [r, g, b] = hexToRgb(vivid(data[i].raw, light));
      colors[i * 3] = r;
      colors[i * 3 + 1] = g;
      colors[i * 3 + 2] = b;
      sizes[i] = 2 + Math.sqrt(data[i].degree) * 0.9;
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    geo.setAttribute("size", new THREE.BufferAttribute(sizes, 1));
    const material = new THREE.ShaderMaterial({
      vertexShader: `
        attribute float size;
        attribute vec3 color;
        varying vec3 vColor;
        void main() {
          vColor = color;
          vec4 mv = modelViewMatrix * vec4(position, 1.0);
          gl_PointSize = size * (320.0 / -mv.z);
          gl_Position = projectionMatrix * mv;
        }`,
      fragmentShader: `
        varying vec3 vColor;
        void main() {
          vec2 d = gl_PointCoord - vec2(0.5);
          if (dot(d, d) > 0.25) discard;
          gl_FragColor = vec4(vColor, 1.0);
        }`,
    });
    const points = new THREE.Points(geo, material);
    pointsRef.current = points;
    scene.add(points);
    buildGlobalEdges();

    // reset selection scope + repaint
    const selected = selectedRef.current;
    const set = new Set<string>();
    if (selected) set.add(selected);
    neighborsRef.current = set;
    paint();
    paintIncident();

    // fit camera to the cloud on the next frame, once the container has its
    // final size (fitting synchronously here can use a stale aspect ratio)
    geo.computeBoundingSphere();
    requestAnimationFrame(fit);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges]);

  useEffect(() => {
    selectedRef.current = selectedId;
    const set = new Set<string>();
    if (selectedId) {
      set.add(selectedId);
      for (const edge of edges) {
        if (edge.kind !== "calls") continue;
        if (edge.source === selectedId) set.add(edge.target);
        if (edge.target === selectedId) set.add(edge.source);
      }
    }
    neighborsRef.current = set;
    paint();
    paintIncident();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, edges]);

  useEffect(() => {
    activityRef.current = activityByNode;
    paint();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activityByNode]);

  useEffect(() => {
    const camera = cameraRef.current;
    const controls = controlsRef.current;
    if (!camera || !controls || !followNodeId) return;
    const idx = indexRef.current.get(followNodeId);
    if (idx === undefined) return;
    const node = dataRef.current[idx];
    const target = new THREE.Vector3(node.x, node.y, node.z);
    controls.target.copy(target);
    const dir = camera.position.clone().sub(target).normalize();
    camera.position.copy(target.clone().add(dir.multiplyScalar(120)));
    controls.update();
  }, [followNodeId]);

  // distance so a sphere of `radius` fits BOTH viewport dimensions (the wide
  // aspect made the vertical extent overflow with a fixed multiplier)
  const fitDistance = (radius: number): number => {
    const camera = cameraRef.current;
    if (!camera) return radius * 2.4;
    const vFov = (camera.fov * Math.PI) / 180;
    const hFov = 2 * Math.atan(Math.tan(vFov / 2) * camera.aspect);
    return (radius / Math.sin(Math.min(vFov, hFov) / 2)) * 1.25;
  };

  const fit = () => {
    const camera = cameraRef.current;
    const controls = controlsRef.current;
    const points = pointsRef.current;
    if (!camera || !controls || !points) return;
    if (!points.geometry.boundingSphere)
      points.geometry.computeBoundingSphere();
    const sphere = points.geometry.boundingSphere;
    if (!sphere) return;
    controls.target.copy(sphere.center);
    const dist = fitDistance(sphere.radius);
    camera.position.set(
      sphere.center.x,
      sphere.center.y,
      sphere.center.z + dist
    );
    camera.near = Math.max(0.1, dist / 100);
    camera.far = dist * 10;
    camera.updateProjectionMatrix();
    controls.update();
  };

  if (failed) {
    return (
      <div className="flex h-full min-h-[420px] items-center justify-center bg-surface-sunken p-6 text-sm text-neutral-300">
        WebGL is unavailable, so the 3D map can’t render. Switch to the 2D view.
      </div>
    );
  }

  return (
    <div className="relative h-full min-h-[420px] overflow-hidden bg-surface-sunken">
      <div ref={containerRef} className="absolute inset-0" />
      <div
        ref={tooltipRef}
        className="pointer-events-none absolute z-30 hidden max-w-xs truncate border border-neutral-700 bg-neutral-950/95 px-2 py-1 text-[11px] text-neutral-100 shadow-lg"
      />
      <button
        type="button"
        onClick={fit}
        className="absolute bottom-4 left-4 border border-neutral-700 bg-neutral-950/90 p-2 text-neutral-300 shadow-lg transition hover:bg-neutral-800 hover:text-white"
        aria-label="Fit graph"
      >
        <Focus size={15} />
      </button>
      <div className="absolute bottom-4 right-4 border border-neutral-800 bg-neutral-950/85 px-3 py-2 text-[10px] uppercase tracking-widest text-neutral-300">
        {count.toLocaleString()} nodes · 3D
      </div>
    </div>
  );
}
