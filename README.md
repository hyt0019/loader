<p align="center">
  <img src="docs/assets/readme-hero.svg" alt="HYT Container Load Planner" width="100%" />
</p>

<p align="center">
  面向真实装柜约束的三维装箱系统。把货物清单转化为可执行、可复核、可导出的装载方案。
</p>

<p align="center">
  <a href="https://loader-hyt.streamlit.app/"><strong>打开在线工作台 →</strong></a>
  &nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#本地运行">本地运行</a>
  &nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#算法与约束">算法说明</a>
</p>

---

## 产品概览

HYT Container Load Planner 接收集装箱尺寸与货物清单，计算每件货物的摆放位置和朝向，并在同一工作台内完成数据录入、实时预估、三维复核与方案交付。

<p align="center">
  <img src="docs/assets/product-preview.png" alt="HYT Container Load Planner 网页工作台" width="100%" />
</p>

| 计算 | 复核 | 交付 |
| :--- | :--- | :--- |
| 标准版快速生成稳定方案；增强版通过混合列模式、二维排样与有界束搜索持续改进 | 整数毫米几何内核逐件校验边界、碰撞、朝向与支撑 | Excel / JSON 双格式导出；保存的方案可重新导入网站查看 |

## 为什么是它

- **以空间利用率为第一目标**：先最大化已装货物体积，同体积下再比较件数与底层偏好。
- **增强版不劣于标准版**：标准版结果始终作为精英下界，新候选只有更优时才会替换。
- **为真实操作设计**：支持六向旋转、禁止倒放、重货/密度底层优先以及至少 60% 的底面支撑。
- **计算结果可解释**：三维视图支持类别高亮、透明模式和长/宽/高分段查看。
- **输入输出闭环**：网页表格、TXT、XLSX 均可作为清单入口；方案可导出为 XLSX 或 JSON 后再次载入。

## 算法与约束

```mermaid
flowchart LR
    A[货物清单] --> B[整数毫米归一化]
    B --> C{求解模式}
    C -->|标准版| D[极点 + DBL]
    C -->|增强版| E[同型 / 混合列模式生成]
    E --> F[二维货架排样 + MaxRects]
    F --> G[有界束搜索 + 记忆化剪枝]
    G --> J[列 / 层续装 + 遗传重启]
    D --> H[统一几何校验]
    J --> H
    H --> I[3D 方案与 XLSX / JSON]
```

所有候选最终都经过同一套硬约束内核：

1. **边界**：货物六个面均不得超出集装箱内尺寸。
2. **碰撞**：任意两件货物的三维实体不得相交。
3. **支撑**：非落地货物至少 60% 的底面积由下方货物直接支撑。
4. **朝向**：普通货物可尝试六种正交旋转；禁止倒放货物只允许绕竖直轴旋转。

重量与密度属于底层摆放的软偏好，不会作为硬约束牺牲已装体积。

### 客户清单回归

| 数据规模 | 原增强算法 | 当前增强算法 | 体积利用率 | 求解时间* |
| ---: | ---: | ---: | ---: | ---: |
| 435 件 / 5 类 | 424 / 435 | **435 / 435** | **90.35%** | **约 1.4 秒** |
| 613 件 / 9 类 | 601 / 613 | **613 / 613** | **84.83%** | **约 12 秒** |

<sub>* 开发机回归结果。实际时间受硬件、清单结构和搜索上限影响；找到全装方案后会立即停止。</sub>

## 本地运行

要求 Python 3.12 或更高版本。

```bash
git clone https://github.com/hyt0019/loader.git
cd loader
pip install -r requirements-server.txt
python -m uvicorn server.main:app --port 8000
```

浏览器打开 `http://localhost:8000`。网页是纯静态文件，无需前端构建；三维查看、高亮、分段等交互都在浏览器中完成，
服务器只负责计算与文件导入导出。货物清单和最近一次结果会自动保存在浏览器中，刷新页面不会丢失。

旧版 Streamlit 工作台仍然保留：`pip install -r requirements_web.txt && streamlit run app.py`（或运行 `run_web_app.bat` / `run_web_app.command`）。

## 使用流程

```mermaid
flowchart LR
    A[选择柜型或填写内尺寸] --> B[录入 / 导入货物清单]
    B --> C[设置倒放与底层偏好]
    C --> D[标准版或增强版计算]
    D --> E[三维复核与分段查看]
    E --> F[导出 XLSX / JSON]
```

### 清单格式

TXT 文件使用以下结构，尺寸单位为米：

```text
5.8 2.35 2.35
2
0.49 0.40 0.09 44 18 1
1.20 1.00 1.35 7 1472 0
```

第一行为集装箱长、宽、高；第二行为货物种类数；后续每行依次为长、宽、高、数量、重量、类型。类型 `0 / 1 / 2` 分别表示木箱、纸箱和托盘。网页也支持直接编辑表格或导入网站导出的 XLSX。

## 系统结构

```text
server/                   网站后端（FastAPI）：计算任务调度（独立子进程、排队、取消）与文件接口
web/                      网站前端：静态页面 + Three.js 三维查看器（web/vendor 为按需打包的 three.js）
planner_core.py           清单解析、方案导入导出、实时预估等通用逻辑（网站与 Streamlit 版共用）
packer_pro.py             整数几何内核、标准与增强求解器（也可作为桌面命令行运行）
app.py                    旧版 Streamlit 工作台
tests/                    算法、方案导入与网站接口回归测试
deploy/                   Caddy 配置与服务器部署说明
Dockerfile                网站镜像；docker-compose.yml 同时启动网站与 Caddy（自动 HTTPS）
sample_data.txt           可直接导入的示例清单
```

网站、Streamlit 版与桌面命令行调用的是 `packer_pro.py` 中同一套求解与校验逻辑。

## 部署

**自有服务器（Docker，推荐）**：仓库自带 `Dockerfile` 与 `docker-compose.yml`，由 Caddy 自动签发 HTTPS 证书。

```bash
cp .env.example .env        # 填写 DOMAIN=你的子域名
docker compose up -d --build
```

域名解析、防火墙、国内镜像源、已有 nginx 等完整步骤见 [deploy/README.md](deploy/README.md)。

**Streamlit Community Cloud（旧版）**：选择本仓库和目标分支，入口文件填写 `app.py`。

## License

基于 [MIT License](LICENSE) 发布。

<p align="center">
  <sub>Designed &amp; engineered by HYT · Container Load Planner</sub>
</p>
