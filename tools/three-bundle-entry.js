// 仅导出 web/js/viewer3d.js 用到的 Three.js 模块（按需打包，减小首次加载体积）
export {
  BoxGeometry, BufferGeometry, CanvasTexture, Color, DirectionalLight, EdgesGeometry,
  Float32BufferAttribute, GridHelper, Group, HemisphereLight, InstancedMesh, LineBasicMaterial,
  LineSegments, Matrix4, MeshLambertMaterial, PerspectiveCamera, Raycaster, Scene,
  Sprite, SpriteMaterial, Vector2, Vector3, WebGLRenderer,
} from 'three';
export { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
