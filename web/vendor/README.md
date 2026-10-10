# 第三方库

| 文件 | 来源 | 许可证 |
| --- | --- | --- |
| `three-0.186.1.bundle.min.js` | [three.js](https://threejs.org/) 0.186.1（npm `three@0.186.1`）中 `web/js/viewer3d.js` 用到的模块与 `OrbitControls`，按需打包压缩 | MIT，见 `three-LICENSE.txt` |

网站运行时不需要任何构建步骤；只有升级 three.js 或在 `viewer3d.js` 中用到新的 Three.js 模块时，才需要重新生成该文件：

```bash
npm pack three@0.186.1 && tar xzf three-0.186.1.tgz        # 得到 package/
# 在 tools/three-bundle-entry.js 中补充需要导出的模块后：
npx esbuild@0.28.2 tools/three-bundle-entry.js --bundle --format=esm --minify \
  --target=es2020 --legal-comments=inline \
  --alias:three/examples=./package/examples --alias:three=./package/build/three.module.js \
  --outfile=web/vendor/three-0.186.1.bundle.min.js
```

文件名带版本号，服务器会让浏览器长期缓存它；升级版本时请同步修改文件名和 `viewer3d.js` 中的引用。
