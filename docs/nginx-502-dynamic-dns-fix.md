# Nginx 502 修复说明：backend 容器重启导致静态 upstream IP 缓存问题

> 日期：2026-08-17
> 环境：服务器 10.100.202.223，Docker Compose 部署（项目名 `bisheng`）
> 影响组件：`bisheng-frontend`（Nginx）→ `bisheng-backend`

---

## 1. 问题现象

- 重启 `bisheng-backend` 容器后，访问 `http://10.100.202.223:3001/` 立即返回 **502 Bad Gateway**。
- 手动执行 `docker exec bisheng-frontend nginx -s reload` 后恢复，但下次重启 backend 又复现。
- backend 本身健康（`curl http://<backend容器IP>:7860/health` 正常），问题只出在 Nginx 转发链路。

## 2. 根因分析

### 2.1 旧配置（问题写法）

```nginx
# docker/nginx/conf.d/default.conf（旧版）
upstream backend_server {
    server backend:7860;   # ← 启动时解析一次，IP 永久缓存
}

location ~ ^(/workspace)?/api(/|$) {
    proxy_pass http://backend_server;   # ← 永远向缓存的旧 IP 转发
    ...
}
```

### 2.2 Nginx DNS 解析机制

| `proxy_pass` 写法 | 域名解析时机 | 容器 IP 变化后 |
|---|---|---|
| `http://backend` / 静态 `upstream` 块 | Nginx **启动 / reload 时一次性解析**，结果常驻内存 | 永远转发到旧 IP → **502** |
| `http://$变量` | **运行时**按 `resolver` 指令的 TTL 周期性解析 | TTL 到期后自动拿到新 IP → 自愈 |

### 2.3 故障链路

```
docker restart bisheng-backend
  → backend 容器重建，Docker 分配新 IP（如 172.18.0.5 → 172.18.0.8）
  → Nginx worker 仍持有启动时缓存的旧 IP 172.18.0.5
  → 转发连接被拒（旧 IP 已无进程监听）
  → 返回 502 Bad Gateway
```

`nginx -s reload` 之所以能临时恢复：reload 会重新加载配置并触发一次新的 DNS 解析，拿到当时的正确 IP——但这只是把"一次性解析"的时点往后挪了，问题本质未变。

## 3. 永久修复方案

改用 **Docker 内置 DNS（127.0.0.11）+ 变量形式的 `proxy_pass`**，让 Nginx 运行时周期性重新解析容器名。

### 3.1 新配置（已合入仓库 `docker/nginx/conf.d/default.conf`）

```nginx
# websocket map 保持不变
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}

# ① 删除整个 upstream backend_server 块

server {
    ...
    listen 3001;

    # ② Docker 内置 DNS，10s 重新解析容器名 → IP
    resolver 127.0.0.11 valid=10s ipv6=off;

    # ③ 用变量承接地址，迫使 Nginx 走运行时解析
    set $backend http://backend:7860;

    location ~ ^(/workspace)?/api(/|$) {
        rewrite ^/workspace(/.*)$ $1 break;
        proxy_pass $backend;        # ④ 原来是 http://backend_server
        ...                          # 其余 proxy 头、超时等设置不变
    }

    location ~ ^(/workspace)?/bisheng|/tmp-dir {
        rewrite ^/workspace(/.*)$ $1 break;
        proxy_pass http://minio:9000;   # minio 容器一般不重启，未改动（如需同样可改变量形式）
    }
}
```

要点说明：

- `resolver 127.0.0.11`：Docker 嵌入式 DNS，负责容器名 → 容器 IP 解析。
- `valid=10s`：解析结果缓存 10 秒，backend IP 变化后**最多 10 秒自动恢复**。
- `ipv6=off`：Docker DNS 可能返回 AAAA（IPv6）记录导致连接失败，显式关闭更稳妥。
- `set $backend ...` + `proxy_pass $backend`：变量形式是触发运行时解析的**必要条件**，直接写 `proxy_pass http://backend:7860` 仍然是启动时一次性解析。

### 3.2 修复后效果

- 重启 / 重建 backend 容器：Nginx 在 10 秒内自动解析到新 IP，**无需任何手动操作**。
- 重建 frontend 容器：配置来自宿主机目录挂载，修复自动生效，不会回退。

## 4. 实施步骤（服务器操作记录）

### 4.1 同步配置文件

从本地仓库上传修复后的配置（或按 3.1 手动修改）：

```powershell
# 本地 Windows PowerShell
scp e:\myCode\gitHub\bisheng\docker\nginx\conf.d\default.conf `
    root@10.100.202.223:/opt/bisheng/docker/nginx/conf.d/default.conf
```

由于 docker-compose.yml 采用**目录挂载**（`./nginx/conf.d:/etc/nginx/conf.d`），宿主机文件即容器内生效文件，无需改 compose、无需重建容器。

### 4.2 校验并重载

```bash
# 语法校验
docker exec bisheng-frontend nginx -t

# 重载生效
docker exec bisheng-frontend nginx -s reload

# 确认新配置已加载
docker exec bisheng-frontend nginx -T | grep -E 'resolver|set \$backend'
# 期望输出：
#   resolver 127.0.0.11 valid=10s ipv6=off;
#   set $backend http://backend:7860;
```

### 4.3 验证（核心）

```bash
# 重启 backend，模拟 IP 变化
docker restart bisheng-backend

# 等待 backend 健康检查通过 + Nginx DNS 缓存过期
sleep 15

# 应返回 200（修复前此处为 502）
curl -I http://10.100.202.223:3001/
```

## 5. 踩坑记录

1. **"已修复"假象**：8 月 12 日曾记录过一版"永久修复"（单文件挂载 `/opt/bisheng/docker/nginx-default.conf`），但实际未落地——服务器 compose 一直是目录挂载，`default.conf` 内容也仍是静态 upstream。**验证修复时必须以 `nginx -T` 输出和重启 backend 实测为准，不能只看文件修改时间或变更记录。**
2. **单文件挂载与目录挂载混用风险**：若在目录挂载（`conf.d:/etc/nginx/conf.d`）之外再追加单文件挂载（`xxx.conf:/etc/nginx/conf.d/default.conf:ro`），两者都能生效但极易混淆来源。本次修复统一走目录挂载路径，仓库内 `docker/nginx/conf.d/default.conf` 即唯一事实源。
3. **仓库与服务器配置漂移**：服务器手工改过的配置若不同步回仓库，下次用仓库重新部署会回退到旧版本。修复后已将新配置合入仓库 `docker/nginx/conf.d/default.conf`，两者保持一致。
4. **`proxy_pass http://backend:7860` 直接写域名 ≠ 动态解析**：没有变量参与时，Nginx 仍在启动时一次性解析。动态解析必须同时满足 `resolver` + `变量形式 proxy_pass` 两个条件。

## 6. 相关文件

| 文件 | 说明 |
|---|---|
| `docker/nginx/conf.d/default.conf` | 修复后的 Nginx 站点配置（仓库唯一事实源） |
| `docker/nginx/nginx.conf` | Nginx 主配置（`include /etc/nginx/conf.d/*.conf`，未改动） |
| `docker/docker-compose.yml` | frontend 服务 volume 挂载定义（未改动） |
