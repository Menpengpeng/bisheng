# BiSheng 部署更新指南

> 适用场景：从开发机源码构建镜像/产物，更新到远程 Docker 部署服务器（如 `10.100.202.223`）。
> 本文档基于实际部署踩坑整理，包含完整的构建、上传、部署、验证、故障排查和永久修复流程。

---

## 一、环境约定

| 角色 | 主机 | 说明 |
|------|------|------|
| 开发机 | 本机（Windows） | 源码编辑、构建镜像、打包前端 |
| 部署服务器 | `10.100.202.223`（示例） | Docker 部署，所有中间件+业务容器 |

**部署服务器关键约定**：

| 项 | 值 |
|----|------|
| Compose 工程目录 | `/opt/bisheng/docker` |
| Compose project name | `bisheng`（决定 docker 网络名 `bisheng_default`） |
| Docker 网络 | `bisheng_default`（所有 bisheng 容器必须在此网络） |
| MySQL 密码 | `1234`（与 `config.yaml` 加密串对应） |
| 后端容器名 | `bisheng-backend` / `bisheng-backend-worker` |
| 前端容器名 | `bisheng-frontend` |
| 中间件容器 | `bisheng-mysql` / `bisheng-redis` / `bisheng-openfga` / `bisheng-es` / `bisheng-milvus-*` |
| SSH 用户 | `gs`（执行 docker 命令需 `sudo su` 切换 root） |

**端口映射**：

| 服务 | 宿主端口 | 容器端口 |
|------|---------|---------|
| 后端 API | 7860 | 7860 |
| 前端 Nginx | 3001 | 3001 |
| MySQL | 3306 | 3306 |
| Redis | 6379 | 6379 |
| OpenFGA | 8080 | 8080 |
| Elasticsearch | 9200 | 9200 |
| MinIO | 9100 | 9000 |
| Milvus | 19530 | 19530 |

---

## 二、前置准备

### 2.1 确认 Docker 环境就绪

在部署服务器上：

```bash
# 切换 root
sudo su

# Docker daemon 正常
docker info | head -5

# 中间件容器运行中
docker ps | grep -E "mysql|redis|openfga|elasticsearch|milvus|minio"
```

### 2.2 确认当前分支代码可编译

在开发机：

```powershell
cd e:\myCode\gitHub\bisheng
git branch --show-current        # 确认分支
git status                       # 确认无未提交冲突
```

### 2.3 确认基础镜像存在

后端 Dockerfile 依赖基础镜像 `dataelement/bisheng-backend:base.v10`，开发机需先拉取：

```powershell
docker pull dataelement/bisheng-backend:base.v10
```

---

## 三、后端镜像构建与部署

### 3.1 在开发机构建镜像

```powershell
cd e:\myCode\gitHub\bisheng\src\backend

# 构建本地镜像
docker build -t dataelement/bisheng-backend:dev-local .

# 验证镜像
docker images dataelement/bisheng-backend
```

### 3.2 导出镜像为 tar

```powershell
cd e:\myCode\gitHub\bisheng
docker save -o bisheng-backend-dev-local.tar dataelement/bisheng-backend:dev-local

# 查看大小（约 5GB）
Get-Item bisheng-backend-dev-local.tar | Select-Object Name, @{N='SizeGB';E={[math]::Round($_.Length/1GB,2)}}
```

### 3.3 上传到部署服务器

```powershell
# 在开发机执行（5GB，内网传输约几分钟）
scp e:\myCode\gitHub\bisheng\bisheng-backend-dev-local.tar gs@10.100.202.223:/home/gs/tmp/
```

### 3.4 在服务器加载并打 tag

```bash
# SSH 到服务器，切换 root
ssh gs@10.100.202.223
sudo su

# 加载镜像
docker load -i /home/gs/tmp/bisheng-backend-dev-local.tar

# 打成 compose 文件引用的 tag（v2.6.0）
docker tag dataelement/bisheng-backend:dev-local dataelement/bisheng-backend:v2.6.0

# 验证
docker images dataelement/bisheng-backend
```

### 3.5 重启后端容器

⚠️ **关键**：必须用 `-p bisheng` 指定 project name，否则会创建 `docker_default` 网络，导致容器间无法 DNS 解析（详见故障案例 F1）。

```bash
cd /opt/bisheng/docker

# 删除旧容器
docker rm -f bisheng-backend bisheng-backend-worker

# 用正确的 project name 重建（--no-deps 跳过依赖中间件，避免误删）
docker compose -p bisheng up -d --no-deps --force-recreate backend backend_worker

# 验证容器状态
docker ps | grep bisheng-backend

# 等待健康检查通过（约 30-60 秒）
sleep 60
docker ps --filter "name=bisheng-backend" --format "{{.Names}}: {{.Status}}"
```

### 3.6 验证后端服务

```bash
# 健康检查
curl http://localhost:7860/health
# 期望: {"status":"OK"}

# 查看启动日志确认无报错
docker logs bisheng-backend --tail 50
```

---

## 四、前端产物部署

### 4.1 在开发机打包前端

```powershell
cd e:\myCode\gitHub\bisheng\src\frontend\platform

# 安装依赖（首次或 lockfile 变更时）
pnpm install --frozen-lockfile

# 构建（产物输出到 build/ 目录，不是 dist/）
pnpm build

# 打包 build 产物
Compress-Archive -Path build\* -DestinationPath bisheng-platform-frontend.tar.zip -Force
```

产物路径：`src/frontend/platform/bisheng-platform-frontend.tar.zip`（约 20MB）

⚠️ **注意**：vite.config.mts 中 `outDir: "build"`，不是 `dist`。

### 4.2 上传到部署服务器

```powershell
scp e:\myCode\gitHub\bisheng\src\frontend\platform\bisheng-platform-frontend.tar.zip gs@10.100.202.223:/home/gs/tmp/
```

### 4.3 在服务器更新前端产物

```bash
sudo su

# 解压到临时目录
mkdir -p /tmp/bisheng-platform-frontend
cd /tmp/bisheng-platform-frontend
unzip -o /home/gs/tmp/bisheng-platform-frontend.tar.zip -d .

# 备份旧前端（带时间戳，可选但推荐）
docker exec bisheng-frontend mv /usr/share/nginx/html/platform /usr/share/nginx/html/platform.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null

# 拷贝新产物进容器
docker cp /tmp/bisheng-platform-frontend/. bisheng-frontend:/usr/share/nginx/html/platform/

# 重载 nginx（零停机）
docker exec bisheng-frontend nginx -s reload

# 验证
curl -I http://localhost:3001/
# 期望: HTTP/1.1 200 OK
```

---

## 五、Nginx 动态解析永久修复（重要）

### 5.1 问题背景

**现象**：每次 `docker restart bisheng-backend` 或重建后端容器后：
1. 访问 `http://10.100.202.223:3001/api/*` 返回 **502 Bad Gateway**
2. 严重情况下 `bisheng-frontend` 容器直接退出（Exited 0），需手动 `docker start`

**原因**：nginx 默认在**启动时**解析 upstream 中的主机名并缓存 IP。
- 后端容器重启后 IP 变化，nginx 仍用旧 IP → 502
- 如果 nginx 启动瞬间后端容器还没就绪 → DNS 解析失败 → nginx 启动失败 → 容器退出

### 5.2 修复方案

将 nginx 配置改为 **变量 + resolver 动态解析** 模式，让 nginx 每 10 秒重新解析 DNS。

### 5.3 修改 nginx 配置文件

**配置文件位置**：`/opt/bisheng/docker/nginx/conf.d/default.conf`（已通过 docker-compose volumes 挂载，宿主机修改即可）。

```bash
cd /opt/bisheng/docker/nginx/conf.d

# 备份
cp default.conf default.conf.bak.$(date +%Y%m%d%H%M%S)

# 1. 在 map 块前加 resolver（127.0.0.11 是 Docker 内置 DNS）
sed -i '/^map /i resolver 127.0.0.11 valid=10s ipv6=off;' default.conf

# 2. 删除 upstream 块（用变量替代）
sed -i '/upstream backend_server {/,/}/d' default.conf

# 3. 在 listen 3001; 后加 set 变量
sed -i '/listen 3001;/a\\tset $backend "backend:7860";' default.conf

# 4. 把 proxy_pass 改成变量形式
sed -i 's|proxy_pass http://backend_server;|proxy_pass http://$backend;|' default.conf

# 验证修改结果
cat default.conf
```

### 5.4 修改后的配置示例

```nginx
# 新增：Docker DNS 解析器，每 10 秒刷新
resolver 127.0.0.11 valid=10s ipv6=off;

map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}

# 已删除：upstream backend_server { ... }

server {
    gzip on;
    gzip_comp_level  2;
    gzip_min_length  1000;
    gzip_types  text/xml text/css;
    gzip_http_version 1.1;
    gzip_vary  on;
    gzip_disable "MSIE [4-6] \.";

    listen 3001;
    set $backend "backend:7860";   # 新增：用变量才能触发动态解析

    location / {
        root /usr/share/nginx/html/platform;
        index index.html index.htm;
        location = /index.html {
            add_header Cache-Control "no-store, no-cache, must-revalidate, proxy-revalidate" always;
            add_header Pragma "no-cache" always;
            add_header Expires 0 always;
        }
        try_files $uri $uri/ /index.html;
        add_header X-Frame-Options SAMEORIGIN;
    }

    location /workspace/ {
        alias /usr/share/nginx/html/client/;
        index index.html index.htm;
        location = /workspace/index.html {
            add_header Cache-Control "no-store, no-cache, must-revalidate, proxy-revalidate" always;
            add_header Pragma "no-cache" always;
            add_header Expires 0 always;
        }
        try_files $uri $uri/ /workspace/index.html;
    }

    location ~ ^(/workspace)?/api(/|$) {
        rewrite ^/workspace(/.*)$ $1 break;
        proxy_pass http://$backend;   # 修改：用变量替代 upstream 名
        proxy_read_timeout 300s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        client_max_body_size 1024m;
        add_header Access-Control-Allow-Origin $host;
        add_header X-Frame-Options SAMEORIGIN;
    }

    location ~ ^(/workspace)?/bisheng|/tmp-dir {
        rewrite ^/workspace(/.*)$ $1 break;
        proxy_pass http://minio:9000;
    }
}
```

### 5.5 验证配置并生效

```bash
# 语法检查（在容器内）
docker exec bisheng-frontend nginx -t

# 重载（无需重启容器）
docker exec bisheng-frontend nginx -s reload
```

### 5.6 验证动态解析生效

```bash
# 重启后端容器（模拟 IP 变更）
docker restart bisheng-backend

# 等待后端启动
sleep 30

# 直接访问，不应再 502（无需手动 reload nginx）
curl http://localhost:3001/api/v1/env
```

---

## 六、完整部署验证清单

部署完成后执行以下验证：

```bash
# 1. 中间件健康
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "mysql|redis|openfga|es|milvus|minio"

# 2. 后端 API
curl http://localhost:7860/health
# 期望: {"status":"OK"}

# 3. 前端页面
curl -I http://localhost:3001/
# 期望: HTTP/1.1 200 OK

# 4. 前端代理后端 API
curl -s http://localhost:3001/api/v1/env
# 期望: 返回 JSON（含版本信息）

# 5. 容器网络一致（所有 bisheng 容器都应在 bisheng_default 网络）
docker inspect bisheng-backend --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
docker inspect bisheng-backend-worker --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
docker inspect bisheng-frontend --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
# 期望: 全部输出 bisheng_default

# 6. Worker 状态
docker logs bisheng-backend-worker --tail 10
# 期望: 看到 celery ready 信息，无报错
```

**浏览器验证**：
- 打开 `http://10.100.202.223:3001/`
- 强制刷新（Ctrl+Shift+R）避免缓存
- 验证前端改动是否生效

---

## 七、清理

部署验证通过后清理临时文件：

```bash
# 在服务器
rm -rf /tmp/bisheng-platform-frontend
rm -f /home/gs/tmp/bisheng-backend-dev-local.tar
rm -f /home/gs/tmp/bisheng-platform-frontend.tar.zip
```

```powershell
# 在开发机（可选）
del e:\myCode\gitHub\bisheng\bisheng-backend-dev-local.tar
del e:\myCode\gitHub\bisheng\src\frontend\platform\bisheng-platform-frontend.tar.zip
```

---

## 八、故障案例与解决

### F1: 容器名冲突 / 网络不一致

**现象**：

```
Error response from daemon: Conflict. The container name "/bisheng-mysql" is already in use
```

或后端报：

```
pymysql.err.OperationalError: (2003, "Can't connect to MySQL server on 'mysql' (Temporary failure in name resolution)")
```

**原因**：在 `/opt/bisheng/docker` 目录用 `docker compose up`，默认 project name 是 `docker`，会创建 `docker_default` 网络。而原部署的 project name 是 `bisheng`，网络是 `bisheng_default`。新旧网络隔离，容器间无法 DNS 解析。

**排查**：

```bash
docker inspect bisheng-backend --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
docker inspect bisheng-mysql --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
# 如果一个在 docker_default，一个在 bisheng_default，就是这个问题
```

**解决**：

```bash
# 删除用错误 project 起的容器
docker rm -f bisheng-backend bisheng-backend-worker

# 用正确的 project name 重建
cd /opt/bisheng/docker
docker compose -p bisheng up -d --no-deps --force-recreate backend backend_worker
```

### F2: 重启后端后前端 502

**现象**：`docker restart bisheng-backend` 后，访问 `http://localhost:3001/api/*` 返回 502。

**原因**：nginx 启动时缓存了 backend 旧 IP，重启后端后 IP 变化，nginx 仍用旧 IP。

**临时解决**：

```bash
docker exec bisheng-frontend nginx -s reload
```

**永久解决**：按本指南第五节修改 nginx 配置，启用动态 DNS 解析。

### F3: 前端容器启动失败退出

**现象**：

```
Error response from daemon: container xxx is not running
```

或 nginx 日志：

```
[emerg] host not found in upstream "backend:7860" in /etc/nginx/conf.d/default.conf:10
nginx: configuration file /etc/nginx/nginx.conf test failed
```

**原因**：nginx 静态 upstream 在启动时立即解析 DNS。如果启动瞬间后端容器还没就绪，DNS 解析失败 → nginx 启动失败 → 容器退出。

**解决**：

```bash
# 1. 确认后端健康
curl http://localhost:7860/health
# 必须返回 {"status":"OK"}

# 2. 确认后端运行后再启动前端
cd /opt/bisheng/docker
docker compose -p bisheng up -d --no-deps frontend

# 3. 永久解决：按第五节改 nginx 配置为动态解析
```

### F4: `docker run --rm` 测试 nginx 配置报 host not found

**现象**：

```bash
docker run --rm -v /opt/bisheng/docker/nginx/conf.d:/etc/nginx/conf.d:ro \
  dataelement/bisheng-frontend:v2.6.0 nginx -t
# 报错：host not found in upstream "backend:7860"
```

**原因**：临时容器没连到 `bisheng_default` 网络，自然解析不到 `backend` 主机名。**这不是配置错误**。

**正确做法**：在运行中的容器内测试：

```bash
docker exec bisheng-frontend nginx -t
```

### F5: 前端打包路径错误

**现象**：

```
Compress-Archive : 路径"dist\*"不存在
```

**原因**：vite.config.mts 配置 `outDir: "build"`，产物在 `build/` 目录不是 `dist/`。

**解决**：

```powershell
Compress-Archive -Path build\* -DestinationPath bisheng-platform-frontend.tar.zip -Force
```

### F6: SSH 用户权限不足

**现象**：`gs` 用户执行 `docker` 命令报权限错误。

**解决**：

```bash
sudo su
# 或把 gs 加入 docker 组（一次性操作）
usermod -aG docker gs
# 加组后需重新登录生效
```

---

## 九、常用命令速查

### 9.1 容器管理

```bash
# 查看所有 bisheng 容器
docker ps -a | grep bisheng

# 查看某个容器日志（实时跟踪）
docker logs -f bisheng-backend

# 重启单个服务
docker compose -p bisheng restart backend

# 重建单个服务（保留中间件）
docker rm -f bisheng-backend && docker compose -p bisheng up -d --no-deps backend

# 进入容器
docker exec -it bisheng-backend bash

# 查看容器所在网络
docker inspect <容器名> --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```

### 9.2 镜像管理

```bash
# 查看本地镜像
docker images | grep bisheng

# 加载 tar 镜像
docker load -i /tmp/bisheng-backend-dev-local.tar

# 重新打 tag
docker tag dataelement/bisheng-backend:dev-local dataelement/bisheng-backend:v2.6.0

# 删除镜像
docker rmi dataelement/bisheng-backend:dev-local
```

### 9.3 Nginx 操作

```bash
# 语法检查
docker exec bisheng-frontend nginx -t

# 重载配置（零停机）
docker exec bisheng-frontend nginx -s reload

# 查看配置文件
docker exec bisheng-frontend cat /etc/nginx/conf.d/default.conf

# 查看 nginx 错误日志
docker exec bisheng-frontend tail -50 /var/log/nginx/error.log
```

### 9.4 网络排查

```bash
# 从前端容器测试后端连通性
docker exec bisheng-frontend curl -s http://backend:7860/health
docker exec bisheng-frontend curl -s http://bisheng-backend:7860/health

# 从前端容器解析主机名
docker exec bisheng-frontend getent hosts backend
docker exec bisheng-frontend getent hosts bisheng-backend
```

---

## 十、附录

### 10.1 关键路径速查

| 项 | 路径 |
|----|------|
| 开发机后端源码 | `e:\myCode\gitHub\bisheng\src\backend\` |
| 开发机后端 Dockerfile | `e:\myCode\gitHub\bisheng\src\backend\Dockerfile` |
| 开发机前端源码 | `e:\myCode\gitHub\bisheng\src\frontend\platform\` |
| 开发机前端构建产物 | `e:\myCode\gitHub\bisheng\src\frontend\platform\build\` |
| 服务器 compose 文件 | `/opt/bisheng/docker/docker-compose.yml` |
| 服务器 backend config | `/opt/bisheng/docker/bisheng/config/config.yaml` |
| 服务器 nginx 主配置 | `/opt/bisheng/docker/nginx/nginx.conf` |
| 服务器 nginx 站点配置 | `/opt/bisheng/docker/nginx/conf.d/default.conf` |
| 服务器数据卷 | `/opt/bisheng/docker/data/` |
| 服务器上传临时目录 | `/home/gs/tmp/` |

### 10.2 镜像构建注意事项

- 后端镜像依赖基础镜像 `dataelement/bisheng-backend:base.v10`，开发机需先 `docker pull`
- 构建上下文是 `src/backend/`，Dockerfile 在该目录下
- 镜像约 5GB，`docker save` 导出 + scp 传输较慢，建议内网环境操作
- 前端产物约 20MB，传输快
- 前端构建产物在 `build/` 目录（vite.config.mts 配置 `outDir: "build"`），**不是 `dist/`**

### 10.3 docker-compose.yml 关键配置

```yaml
# frontend 服务已挂载 nginx 配置（宿主机修改即可生效）
frontend:
  container_name: bisheng-frontend
  image: dataelement/bisheng-frontend:v2.6.0
  ports:
    - "3001:3001"
  volumes:
    - ${DOCKER_VOLUME_DIRECTORY:-.}/nginx/nginx.conf:/etc/nginx/nginx.conf
    - ${DOCKER_VOLUME_DIRECTORY:-.}/nginx/conf.d:/etc/nginx/conf.d
  depends_on:
    - backend
```

```yaml
# backend 服务依赖 mysql/redis/openfga
backend:
  container_name: bisheng-backend
  image: dataelement/bisheng-backend:v2.6.0
  ports:
    - "7860:7860"
  volumes:
    - ${DOCKER_VOLUME_DIRECTORY:-.}/bisheng/config/config.yaml:/app/bisheng/config.yaml
    - ${DOCKER_VOLUME_DIRECTORY:-.}/bisheng/entrypoint.sh:/app/entrypoint.sh
    - ${DOCKER_VOLUME_DIRECTORY:-.}/data/bisheng:/app/data
  command: sh entrypoint.sh api
  depends_on:
    mysql:
      condition: service_healthy
    redis:
      condition: service_healthy
    openfga:
      condition: service_started
```

### 10.4 一次性完整部署脚本（服务器侧）

假设镜像和前端产物已上传到 `/home/gs/tmp/`：

```bash
#!/bin/bash
set -e

# 切换 root
sudo su

echo "===== 1. 加载后端镜像 ====="
docker load -i /home/gs/tmp/bisheng-backend-dev-local.tar
docker tag dataelement/bisheng-backend:dev-local dataelement/bisheng-backend:v2.6.0

echo "===== 2. 重建后端容器 ====="
cd /opt/bisheng/docker
docker rm -f bisheng-backend bisheng-backend-worker
docker compose -p bisheng up -d --no-deps --force-recreate backend backend_worker

echo "===== 3. 等待后端启动 ====="
sleep 60
curl http://localhost:7860/health

echo "===== 4. 更新前端产物 ====="
mkdir -p /tmp/bisheng-platform-frontend
cd /tmp/bisheng-platform-frontend
unzip -o /home/gs/tmp/bisheng-platform-frontend.tar.zip -d .
docker exec bisheng-frontend mv /usr/share/nginx/html/platform /usr/share/nginx/html/platform.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null || true
docker cp /tmp/bisheng-platform-frontend/. bisheng-frontend:/usr/share/nginx/html/platform/
docker exec bisheng-frontend nginx -s reload

echo "===== 5. 验证 ====="
curl http://localhost:7860/health
curl -I http://localhost:3001/
curl -s http://localhost:3001/api/v1/env

echo "===== 6. 清理 ====="
rm -rf /tmp/bisheng-platform-frontend
rm -f /home/gs/tmp/bisheng-backend-dev-local.tar
rm -f /home/gs/tmp/bisheng-platform-frontend.tar.zip

echo "===== 部署完成 ====="
```

---

## 十一、变更记录

| 日期 | 变更内容 |
|------|---------|
| 2026-07-24 | 初版，整理后端镜像构建、前端产物部署、nginx 动态解析修复 |
| 2026-08-17 | 补充实际部署踩坑案例（F1-F6），加入完整部署脚本和验证清单 |
