# 自有服务器部署（Docker）

部署结构：`Caddy（80/443，自动 HTTPS）` → `loader 容器（FastAPI，8000）`。

- 网页是纯静态文件，访客打开即用；三维查看、高亮、分段等交互都在浏览器中完成。
- 只有「开始计算」和文件导入导出会请求服务器。每次计算在独立子进程中运行，
  同时运行数默认等于 CPU 核数，其余排队；访客关闭页面后，未完成的计算会自动停止。
- 镜像只需在部署 / 更新时构建一次，访客访问不会触发构建。

以下用 `loader.example.com` 代表你的子域名，请替换成实际值。

## 1. 解析子域名

在域名服务商的 DNS 控制台添加一条记录：

| 类型 | 主机记录 | 记录值 |
| --- | --- | --- |
| A | `loader`（子域名前缀） | 服务器公网 IP |

在本机执行 `ping loader.example.com`，确认解析到服务器 IP 后再进行第 3 步（Caddy 申请证书时需要解析已生效）。

## 2. 准备服务器

- 安装 Docker（Ubuntu / Debian）：`curl -fsSL https://get.docker.com | sh`
- 在云服务器的**安全组 / 防火墙**中放行 TCP 80、443（UDP 443 可选，用于 HTTP/3）。
- 80 / 443 端口不能被其他程序占用；如果服务器上已经运行了 nginx，请看下文「已有 nginx 时」。

> **中国大陆服务器注意：**
> - 域名需要在该云厂商完成 **ICP 备案**，否则 80/443 端口的网站访问会被拦截；香港及海外地域不需要备案。
> - 拉取 `python` / `caddy` 镜像超时：在 `/etc/docker/daemon.json` 配置云厂商提供的镜像加速地址后 `systemctl restart docker`。
> - 安装 Python 依赖较慢：在 `.env` 中设置 `PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`。

## 3. 首次部署

```bash
git clone https://github.com/hyt0019/loader.git
cd loader
cp .env.example .env
nano .env                 # 把 DOMAIN 改成你的子域名
docker compose up -d --build
```

一两分钟后访问 `https://loader.example.com` 即可。证书由 Caddy 自动申请和续期。

常用命令：

```bash
docker compose ps                  # 查看状态（loader 应显示 healthy）
docker compose logs -f loader      # 应用日志
docker compose logs -f caddy       # 证书 / 反代日志
docker stats                       # 实时 CPU / 内存占用
docker compose restart loader      # 重启应用（正在进行的计算会中断）
docker compose down                # 停止并移除容器（证书数据保留在卷中）
```

## 4. 更新版本

```bash
cd loader
git pull
docker compose up -d --build
```

## 可调参数（写在 `.env` 中）

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `LOADER_MAX_RUNNING` | 2 | 同时运行的计算数，建议等于 CPU 核数，其余排队 |
| `LOADER_MAX_BUDGET` | 600 | 增强版搜索时间上限（秒），网页滑块不会超过它 |

`server/main.py` 还支持 `LOADER_MAX_QUEUED`（排队上限，默认 20）、`LOADER_MAX_PER_CLIENT`
（同一访客同时进行的计算数，默认 2）、`LOADER_MAX_PIECES`（单次计算件数上限，默认 20000）、
`LOADER_JOB_EXTRA_SECONDS`（硬性时限余量，默认 600 秒）。需要时在 `docker-compose.yml` 的
`environment` 中添加。

## 已有 Caddy 网关容器时（与其他系统共用服务器）

服务器上已经有其他 Docker 项目用 Caddy 占着 80/443 时，不要再启动本项目的 Caddy，
而是让装箱网站加入那个网关的 Docker 网络，再在网关的站点配置里为子域名加一个站点。

1. 查网关容器所在的网络名（把容器名换成你的网关容器）：

   ```bash
   docker inspect <网关容器名> --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
   ```

2. 启动装箱网站（只启动网站本身，默认同时只算一个任务、内存上限 400 MB、CPU 上限 1 核）：

   ```bash
   git clone https://github.com/hyt0019/loader.git hyt-loader && cd hyt-loader
   echo "GATEWAY_NETWORK=<上一步查到的网络名>" > .env
   docker compose -f docker-compose.shared.yml up -d --build
   ```

   compose 项目名固定为 `hyt-loader`（见 `docker-compose.shared.yml` 的 `name`），与网关项目互不相干：
   网关项目执行 `docker compose up --remove-orphans` 或重建时都不会删掉装箱网站。

3. 在网关的站点配置中加一个站点块，然后重启网关：

   ```caddy
   loader.example.com {
   	encode zstd gzip
   	reverse_proxy hyt-loader:8000
   }
   ```

   推荐的做法是网关的 Caddyfile 末尾写 `import /etc/caddy/sites/*.caddy`，并把服务器本地的
   `deploy/sites` 目录只读挂载到 `/etc/caddy/sites`；站点块写在该目录的 `.caddy` 文件里（不进网关的 Git），
   改完后在网关项目目录执行 `docker compose restart <网关服务名>`。

   本项目当前所在的服务器即按此方式配置：网关为 ERP 项目（`/opt/caigou-baoguan-kaipiao`，
   compose 项目 `purchase-customs-app`，服务 `web`），装箱网站的站点块在
   `/opt/caigou-baoguan-kaipiao/deploy/sites/hoteches.caddy` 中。改转发规则只改这个文件，然后：

   ```bash
   cd /opt/caigou-baoguan-kaipiao && sudo docker compose restart web
   ```

   不要改网关的 `deploy/Caddyfile`；也不要用 `docker cp` 往网关容器里复制配置（`sites` 目录为只读挂载）。

4. 把子域名的 A 记录解析到服务器 IP，Caddy 会自动申请 HTTPS 证书。

资源上限可在 `.env` 中用 `LOADER_MEM_LIMIT`、`LOADER_CPUS`、`LOADER_MAX_RUNNING` 调整。

### 共用网关时更新版本

只要容器名 `hyt-loader`、端口 `8000` 和所在网络不变，更新装箱网站不需要改网关配置、不需要重启或重建网关：

```bash
cd ~/hyt-loader
git pull
sudo docker compose -f docker-compose.shared.yml up -d --build
```

如果网关项目的 Docker 网络被删除后重新创建，装箱网站会与新网络断开，访问出现 502；
在装箱网站目录执行 `sudo docker compose -f docker-compose.shared.yml up -d --force-recreate` 重新接入即可。

## 已有 nginx 时

如果服务器上已经有 nginx 在使用 80/443，就不要启动 Caddy，只启动应用容器：

```bash
docker compose up -d --build loader
```

应用只监听本机 `127.0.0.1:8000`，再在 nginx 中为子域名添加站点：

```nginx
server {
    listen 80;
    server_name loader.example.com;

    client_max_body_size 20m;   # 允许上传清单 / 方案文件
    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

HTTPS 证书可用 `certbot --nginx -d loader.example.com` 申请。

## 本机试运行（不需要域名）

```bash
docker build -t hyt-loader .
docker run --rm -p 8000:8000 hyt-loader
```

浏览器打开 `http://localhost:8000`。不用 Docker 时：

```bash
pip install -r requirements-server.txt
python -m uvicorn server.main:app --port 8000
```
