# 📦 智能集装箱装箱系统

精确装箱 · 混合启发式逼近最优 · 交互式 3D 可视化 · 一键导出方案

一个三维装箱（3D bin packing）计算工具：给定集装箱尺寸和一批货物（长/宽/高/数量/重量/类型），
自动计算摆放方案，支持重量叠放约束与 60% 底面支撑约束，并提供网页版交互界面与桌面命令行两种使用方式。

---

## ✨ 功能特点

- **两种计算模式**
  - **标准版**：确定性启发式（极点 + Deepest-Bottom-Left），结果稳定、可复现、速度快。
  - **增强版**：组合通用列/层生成、二维 MaxRects 排样和遗传/随机重启搜索；按“件数 → 体积利用率 → 底层偏好”排序，**保证不劣于标准版**，达到 100% 装载即停止。网页默认最多搜索 600 秒。
- **整数毫米几何内核**：碰撞/支撑/边界判定全部整数运算，无浮点误差；网格空间索引 + 增量极点，速度快。
- **交互式 3D 可视化**（网页版，Plotly）：可旋转/缩放/悬停；按类别切换高亮；逐层查看装载过程。
- **实时预估与预警**：填数据即时显示体积占比、总重，并预警超体积/超尺寸/重货。
- **一键导出**：Excel 装箱清单、JSON 方案；两种格式都可重新导入回看。

## 🗂️ 项目结构

```
app.py                 网页版（Streamlit + Plotly），推荐入口
packer_pro.py          装箱计算内核（标准版 + 增强版），网页版依赖它；也可直接运行命令行版
.streamlit/config.toml 网页版主题与外观配置
requirements.txt       运行依赖（Streamlit Cloud 部署用）
requirements_web.txt   网页版依赖（本地一键脚本用）
run_web_app.bat        Windows 一键启动网页版
run_web_app.command    macOS 一键启动网页版
build_exe.bat          把命令行版打包成独立 exe（Windows，可选）
sample_data.txt        示例数据（可直接导入体验）
网页版说明.txt          网页版使用与部署说明
客户使用说明.txt        交付给最终用户的操作指南
打包与分发说明.txt      桌面版打包说明
```

## 🚀 快速开始（网页版）

需要 Python 3.9+。

```bash
pip install -r requirements_web.txt
streamlit run app.py
```

浏览器会自动打开 `http://localhost:8501`。
Windows 也可直接双击 `run_web_app.bat`，macOS 双击 `run_web_app.command`（首次会自动建环境装依赖）。

## ☁️ 部署到 Streamlit Community Cloud（免费，客户零安装）

1. 把本仓库推到 GitHub。
2. 打开 https://share.streamlit.io ，用 GitHub 登录，选择本仓库，主文件填 `app.py`，Deploy。
3. 得到一个公开网址，任何人用浏览器即可访问（Windows/Mac/手机皆可）。

## 📐 数据格式

- 第一行：集装箱 长 宽 高（米，空格分隔）
- 第二行：货物种类数
- 其后每行：长 宽 高 数量 重量 类型（`0`=木箱，`1`=纸箱，`2`=托盘）

示例：

```
2.35 5.8 2.35
2
0.49 0.4 0.09 44 18 1
1.2 1 1.35 7 1472 0
```

网页版可直接用表格录入，无需手写此格式。

## 🖥️ 打包为桌面程序（可选）

在 Windows 上双击 `build_exe.bat`（打包 `packer_pro.py` 的命令行版），
会在干净虚拟环境中生成体积精简的“文件夹版”程序，详见 `打包与分发说明.txt`。

> 网页版是推荐的交付方式：客户只需浏览器，无需安装任何环境，也不会接触到源码。

## 📄 许可证

本项目基于 MIT License 开源，详见 [LICENSE](LICENSE)。

---

Designed by **HE**
