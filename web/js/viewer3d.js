// 交互式 3D 装箱查看器（Three.js）。坐标单位：米；X = 长度（装货方向），Y = 宽度，Z = 高度。
// 每类货物一个 InstancedMesh，几千件货物也只需少量绘制调用；高亮 / 分段 / 透明等
// 交互只改本地数据，不需要请求服务器。
import {
  BoxGeometry, BufferGeometry, CanvasTexture, Color, DirectionalLight, EdgesGeometry,
  Float32BufferAttribute, GridHelper, Group, HemisphereLight, InstancedMesh, LineBasicMaterial,
  LineSegments, Matrix4, MeshLambertMaterial, OrbitControls, PerspectiveCamera, Raycaster, Scene,
  Sprite, SpriteMaterial, Vector2, Vector3, WebGLRenderer,
} from '../vendor/three-0.186.1.bundle.min.js';
import { categoryColor, escapeHtml, g, typeLabel } from './format.js';

const GHOST_COLOR = '#D8D9D5';
const BOX_EDGES = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]];
const VIEW_DIRECTIONS = {
  iso: [1.65, -1.55, 1.0],   // 与原 Plotly 视角一致
  top: [0, -0.001, 1],       // 俯视（略微倾斜，避免与竖直轴共线）
  side: [0, -1, 0.0001],     // 侧视：看到 长 × 高
  end: [-1, 0, 0.0001],      // 端视：沿长度方向看到 宽 × 高
};

function webglAvailable() {
  try {
    const canvas = document.createElement('canvas');
    return !!(window.WebGL2RenderingContext && canvas.getContext('webgl2'))
      || !!(window.WebGLRenderingContext && canvas.getContext('webgl'));
  } catch {
    return false;
  }
}

function makeLabel(text, worldHeight) {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  const font = '600 44px Inter, "PingFang SC", "Microsoft YaHei", sans-serif';
  ctx.font = font;
  const width = Math.ceil(ctx.measureText(text).width) + 24;
  canvas.width = width;
  canvas.height = 64;
  ctx.font = font;
  ctx.fillStyle = '#5B6472';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, 12, 34);
  const texture = new CanvasTexture(canvas);
  texture.anisotropy = 4;
  const sprite = new Sprite(new SpriteMaterial({ map: texture, depthTest: false, transparent: true }));
  sprite.scale.set(worldHeight * (width / 64), worldHeight, 1);
  sprite.renderOrder = 5;
  return sprite;
}

export class PlanViewer {
  /**
   * @param {HTMLElement} host   画布容器
   * @param {{tooltip: HTMLElement, info: HTMLElement}} ui
   */
  constructor(host, ui) {
    this.host = host;
    this.ui = ui;
    this.supported = webglAvailable();
    this.state = { highlight: new Set(), region: null, translucent: true, edges: true };
    this.categories = new Map();
    if (!this.supported) return;

    this.renderer = new WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    host.appendChild(this.renderer.domElement);

    this.scene = new Scene();
    this.scene.background = new Color('#F6F6F3');
    this.camera = new PerspectiveCamera(36, 1, 0.01, 1000);
    this.camera.up.set(0, 0, 1);

    const hemi = new HemisphereLight(0xffffff, 0x8f97a6, 2.1);
    hemi.position.set(0, 0, 1);
    const key = new DirectionalLight(0xffffff, 1.9);
    key.position.set(1, -1.2, 2);
    const fill = new DirectionalLight(0xffffff, 0.55);
    fill.position.set(-1.4, 1, 0.6);
    this.scene.add(hemi, key, fill);

    this.content = new Group();
    this.scene.add(this.content);
    this.unitBox = new BoxGeometry(1, 1, 1);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.14;
    this.controls.screenSpacePanning = true;
    this.controls.addEventListener('change', () => this.requestRender());

    this.raycaster = new Raycaster();
    this.pointer = new Vector2();
    this.pointerEvent = null;
    const el = this.renderer.domElement;
    el.addEventListener('pointermove', (e) => this.onPointerMove(e));
    el.addEventListener('pointerleave', () => this.hideTip());
    el.addEventListener('pointerdown', () => this.hideTip());

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(host);
    this.resize();
  }

  // ---------------- 方案 ---------------- //
  setPlan(plan) {
    if (!this.supported) return;
    this.resize(); // 结果区刚显示时，先同步画布尺寸再计算视角
    this.clearContent();
    const [L, W, H] = plan.container.map(Number);
    this.dims = [L, W, H];
    this.total = plan.packing_plan.length;
    // 方案 boxes 第 10 列为「不可压」（旧方案没有这一列）
    this.noStack = new Set((plan.boxes || []).flatMap((box, io) => (box[9] ? [io] : [])));
    this.categories = new Map();
    plan.packing_plan.forEach((item) => {
      const io = item.box_info.input_order;
      if (!this.categories.has(io)) this.categories.set(io, { io, items: [], slots: [] });
      this.categories.get(io).items.push(item);
    });
    for (const cat of this.categories.values()) {
      cat.material = new MeshLambertMaterial({
        color: categoryColor(cat.io), polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
      });
      cat.mesh = new InstancedMesh(this.unitBox, cat.material, cat.items.length);
      cat.mesh.userData.cat = cat;
      this.content.add(cat.mesh);
    }

    // 集装箱线框
    const frameGeom = new EdgesGeometry(new BoxGeometry(L, W, H));
    const frame = new LineSegments(frameGeom, new LineBasicMaterial({ color: 0x263142 }));
    frame.position.set(L / 2, W / 2, H / 2);
    this.content.add(frame);

    // 地面网格（XY 平面）
    const size = Math.ceil(Math.max(L, W) * 1.5);
    const grid = new GridHelper(size, Math.min(size * 2, 80), 0xC9CCC5, 0xE1E3DE);
    grid.rotation.x = Math.PI / 2;
    grid.position.set(L / 2, W / 2, -0.002);
    this.content.add(grid);

    // 坐标轴文字：明确长 / 宽 / 高 与装货方向
    const unit = Math.max(L, W, H) * 0.045;
    const lengthLabel = makeLabel(`长 Length ${g(L)} m →`, unit);
    lengthLabel.position.set(L / 2, -unit * 5, 0); // 放在柜子前方地面，避免压在货物上
    const widthLabel = makeLabel(`宽 Width ${g(W)} m`, unit);
    widthLabel.position.set(L + unit * 5, W / 2, 0);
    const heightLabel = makeLabel(`高 Height ${g(H)} m`, unit);
    heightLabel.position.set(-unit * 0.6, -unit * 0.6, H + unit * 0.9);
    this.content.add(lengthLabel, widthLabel, heightLabel);

    this.edgeLines = new LineSegments(new BufferGeometry(),
      new LineBasicMaterial({ color: 0x141B26, transparent: true, opacity: 0.5 }));
    this.content.add(this.edgeLines);

    this.hoverBox = new LineSegments(new EdgesGeometry(this.unitBox),
      new LineBasicMaterial({ color: 0xF59E0B, depthTest: false, transparent: true }));
    this.hoverBox.renderOrder = 10;
    this.hoverBox.visible = false;
    this.content.add(this.hoverBox);

    this.update();
    this.setView('iso');
  }

  clearContent() {
    this.hideTip();
    for (const child of [...this.content.children]) {
      child.traverse((obj) => {
        if (obj.geometry && obj.geometry !== this.unitBox) obj.geometry.dispose();
        if (obj.material) {
          if (obj.material.map) obj.material.map.dispose();
          obj.material.dispose();
        }
      });
      this.content.remove(child);
      if (child.isInstancedMesh) child.dispose();
    }
  }

  // ---------------- 显示状态 ---------------- //
  setHighlight(categories) { this.state.highlight = new Set(categories); this.update(); }
  setRegion(region) { this.state.region = region; this.update(); }
  setTranslucent(on) { this.state.translucent = on; this.update(); }
  setEdges(on) { this.state.edges = on; this.update(); }

  inRegion(pos) {
    const r = this.state.region;
    if (!r) return true;
    const eps = 1e-9;
    return pos[0] >= r[0] - eps && pos[0] <= r[1] + eps && pos[1] >= r[2] - eps && pos[1] <= r[3] + eps
      && pos[2] >= r[4] - eps && pos[2] <= r[5] + eps;
  }

  update() {
    if (!this.supported || !this.dims) return;
    const { highlight, translucent, edges } = this.state;
    const matrix = new Matrix4();
    const edgePositions = [];
    let shown = 0;
    for (const cat of this.categories.values()) {
      const selected = highlight.has(cat.io + 1);
      const opacity = selected ? (translucent ? 0.42 : 1) : 0.08;
      cat.material.color.set(selected ? categoryColor(cat.io) : GHOST_COLOR);
      const transparent = opacity < 1;
      if (cat.material.transparent !== transparent) cat.material.needsUpdate = true;
      cat.material.transparent = transparent;
      cat.material.opacity = opacity;
      cat.material.depthWrite = !transparent;
      cat.selected = selected;
      cat.slots = [];
      for (const item of cat.items) {
        if (!this.inRegion(item.position)) continue;
        const [x, y, z] = item.position;
        const [l, w, h] = item.dimensions;
        matrix.makeScale(l, w, h).setPosition(x + l / 2, y + w / 2, z + h / 2);
        cat.mesh.setMatrixAt(cat.slots.length, matrix);
        cat.slots.push(item);
        if (selected) {
          shown += 1;
          if (edges) pushBoxEdges(edgePositions, x, y, z, l, w, h);
        }
      }
      cat.mesh.count = cat.slots.length;
      cat.mesh.visible = cat.slots.length > 0;
      cat.mesh.instanceMatrix.needsUpdate = true;
      cat.mesh.computeBoundingBox();
      cat.mesh.computeBoundingSphere();
    }
    this.edgeLines.geometry.dispose();
    const geometry = new BufferGeometry();
    geometry.setAttribute('position', new Float32BufferAttribute(edgePositions, 3));
    this.edgeLines.geometry = geometry;
    this.edgeLines.visible = edgePositions.length > 0;
    this.hoverBox.visible = false;
    if (this.ui.info) this.ui.info.textContent = `当前显示 ${shown} / ${this.total} 件`;
    this.requestRender();
  }

  // ---------------- 相机 ---------------- //
  setView(name) {
    if (!this.supported || !this.dims) return;
    const [L, W, H] = this.dims;
    const center = new Vector3(L / 2, W / 2, H / 2);
    const radius = 0.5 * Math.hypot(L, W, H);
    const vfov = (this.camera.fov * Math.PI) / 180;
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * this.camera.aspect);
    const distance = (radius / Math.sin(Math.min(vfov, hfov) / 2)) * 1.12; // 留出坐标文字的位置
    const dir = new Vector3(...(VIEW_DIRECTIONS[name] || VIEW_DIRECTIONS.iso)).normalize();
    this.camera.position.copy(center).addScaledVector(dir, distance);
    this.camera.near = distance / 200;
    this.camera.far = distance * 20;
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(center);
    this.controls.minDistance = radius * 0.15;
    this.controls.maxDistance = distance * 6;
    this.controls.update();
    this.requestRender();
  }

  resize() {
    if (!this.supported) return;
    const width = this.host.clientWidth || 1;
    const height = this.host.clientHeight || 1;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.requestRender();
  }

  requestRender() {
    if (!this.supported || this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.controls.update(); // 阻尼未停止时会再次触发 change → 继续下一帧
      this.renderer.render(this.scene, this.camera);
      if (this.pointerEvent) this.pick();
    });
  }

  /** 返回当前画面的 PNG（Blob）。 */
  snapshot() {
    return new Promise((resolve) => {
      this.renderer.render(this.scene, this.camera);
      this.renderer.domElement.toBlob(resolve, 'image/png');
    });
  }

  // ---------------- 悬停提示 ---------------- //
  onPointerMove(event) {
    if (event.buttons) { this.hideTip(); return; } // 拖动旋转时不做拾取
    this.pointerEvent = event;
    this.requestRender();
  }

  pick() {
    const event = this.pointerEvent;
    this.pointerEvent = null;
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const meshes = [...this.categories.values()].filter((c) => c.selected && c.mesh.visible).map((c) => c.mesh);
    const hit = this.raycaster.intersectObjects(meshes, false)[0];
    if (!hit || hit.instanceId === undefined) { this.hideTip(); return; }
    const item = hit.object.userData.cat.slots[hit.instanceId];
    if (!item) { this.hideTip(); return; }
    const [x, y, z] = item.position;
    const [l, w, h] = item.dimensions;
    this.hoverBox.scale.set(l * 1.002, w * 1.002, h * 1.002);
    this.hoverBox.position.set(x + l / 2, y + w / 2, z + h / 2);
    this.hoverBox.visible = true;
    this.renderer.render(this.scene, this.camera);
    this.showTip(item, event.clientX - rect.left, event.clientY - rect.top, rect);
  }

  showTip(item, px, py, rect) {
    const tip = this.ui.tooltip;
    if (!tip) return;
    const info = item.box_info;
    const [dl, dw, dh] = info.original_dimensions;
    const [x, y, z] = item.position;
    const desc = info.desc ? `${escapeHtml(info.desc)}<br>` : '';
    const noStack = this.noStack && this.noStack.has(info.input_order) ? '｜不可压' : '';
    tip.innerHTML = `<b>箱号 ${info.number}</b>｜类别 ${info.input_order + 1}｜${escapeHtml(typeLabel(info.type))}${noStack}<br>`
      + `${desc}尺寸 ${g(dl)}×${g(dw)}×${g(dh)} m｜重量 ${g(info.weight)} kg<br>`
      + `摆放 ${g(item.dimensions[0])}×${g(item.dimensions[1])}×${g(item.dimensions[2])} m｜位置 (${g(x)}, ${g(y)}, ${g(z)})`;
    tip.hidden = false;
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    const left = px + 16 + tw > rect.width ? px - tw - 12 : px + 16;
    const top = py + 16 + th > rect.height ? py - th - 12 : py + 16;
    tip.style.left = `${Math.max(4, left)}px`;
    tip.style.top = `${Math.max(4, top)}px`;
  }

  hideTip() {
    if (this.ui.tooltip) this.ui.tooltip.hidden = true;
    if (this.hoverBox && this.hoverBox.visible) {
      this.hoverBox.visible = false;
      this.requestRender();
    }
  }
}

function pushBoxEdges(out, x, y, z, l, w, h) {
  const v = [[x, y, z], [x + l, y, z], [x + l, y + w, z], [x, y + w, z],
    [x, y, z + h], [x + l, y, z + h], [x + l, y + w, z + h], [x, y + w, z + h]];
  for (const [a, b] of BOX_EDGES) out.push(...v[a], ...v[b]);
}
