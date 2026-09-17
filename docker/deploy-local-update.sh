#!/usr/bin/env bash
# =============================================================
# BiSheng 本地构建产物一键部署脚本
# 用法: ./deploy-local-update.sh [选项]
#
# 适用场景：从开发机构建的镜像 tar + 前端 zip 部署到服务器 Docker 环境
# 处理的异常情况：
#   1. 上传文件不存在 / 大小为 0
#   2. docker load 失败（tar 损坏）
#   3. backend 容器启动失败（迁移失败导致 entrypoint 退出）
#   4. backend 健康检查不通过
#   5. frontend 容器没运行（docker exec 失败）
#   6. nginx 配置改了但没 reload（导致 502）
#   7. 容器名冲突 / 网络不一致（必须 -p bisheng）
#   8. 旧前端产物备份堆积占空间
#   9. 部署中途失败时清理临时文件
# =============================================================

set -euo pipefail

# ─── 配置 ────────────────────────────────────────────────────
UPLOAD_DIR="${UPLOAD_DIR:-/home/gs/tmp}"
BACKEND_TAR="${BACKEND_TAR:-$UPLOAD_DIR/bisheng-backend-dev-local.tar}"
FRONTEND_ZIP="${FRONTEND_ZIP:-$UPLOAD_DIR/bisheng-platform-frontend.zip}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/bisheng/docker}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-bisheng}"
TEMP_DIR="${TEMP_DIR:-/tmp/bisheng-deploy-$$}"

# 容器名（与 docker-compose.yml 对应）
BACKEND_CONTAINER="bisheng-backend"
WORKER_CONTAINER="bisheng-backend-worker"
FRONTEND_CONTAINER="bisheng-frontend"

# 超时配置（秒）
BACKEND_HEALTH_TIMEOUT="${BACKEND_HEALTH_TIMEOUT:-120}"
BACKEND_HEALTH_INTERVAL="${BACKEND_HEALTH_INTERVAL:-5}"

# 镜像 tag（最终打成的 tag，与 compose 文件引用一致）
FINAL_IMAGE_TAG="${FINAL_IMAGE_TAG:-v2.6.0}"
SOURCE_IMAGE_TAG="${SOURCE_IMAGE_TAG:-dev-local}"
IMAGE_NAME="dataelement/bisheng-backend"

# ─── 颜色输出 ────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()   { echo -e "${GREEN}[INFO]${RESET} $*"; }
warn()   { echo -e "${YELLOW}[WARN]${RESET} $*"; }
error()  { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
header() { echo -e "\n${CYAN}${BOLD}═══ $* ═══${RESET}"; }
step()   { echo -e "${CYAN}[$1]${RESET} $2"; }

# ─── 错误处理 ─────────────────────────────────────────────────
cleanup() {
    local exit_code=$?
    if [[ -d "$TEMP_DIR" ]]; then
        info "清理临时目录 $TEMP_DIR"
        rm -rf "$TEMP_DIR"
    fi
    if [[ $exit_code -ne 0 ]]; then
        error "部署失败（exit code: $exit_code）。请检查上方日志。"
        error "排查建议："
        error "  1. backend 没起来：docker logs $BACKEND_CONTAINER（看是否迁移失败）"
        error "  2. 502：docker exec $FRONTEND_CONTAINER nginx -s reload（动态解析已配置）"
        error "  3. 网络问题：docker inspect $BACKEND_CONTAINER --format '{{range \$k,\$v := .NetworkSettings.Networks}}{{\$k}} {{end}}' 应为 bisheng_default"
    fi
    exit $exit_code
}
trap cleanup EXIT INT TERM

# ─── 前置检查 ─────────────────────────────────────────────────
check_prerequisites() {
    header "前置检查"

    # 必须是 root
    if [[ $EUID -ne 0 ]]; then
        error "必须以 root 身份运行（需要操作 docker）"
        error "请执行：sudo su"
        exit 1
    fi
    info "root 身份确认"

    # Docker daemon
    if ! docker info >/dev/null 2>&1; then
        error "Docker daemon 未运行，请先启动 docker"
        exit 1
    fi
    info "Docker daemon 正常"

    # Compose 目录
    if [[ ! -f "$COMPOSE_DIR/docker-compose.yml" ]]; then
        error "docker-compose.yml 不存在: $COMPOSE_DIR/docker-compose.yml"
        error "请确认 COMPOSE_DIR 配置正确"
        exit 1
    fi
    info "Compose 目录: $COMPOSE_DIR"

    # 上传文件存在性 + 大小
    local missing=()
    [[ ! -s "$BACKEND_TAR" ]] && missing+=("$BACKEND_TAR")
    [[ ! -s "$FRONTEND_ZIP" ]] && missing+=("$FRONTEND_ZIP")

    if [[ ${#missing[@]} -gt 0 ]]; then
        error "以下文件不存在或大小为 0："
        printf '  - %s\n' "${missing[@]}"
        error "请先从开发机上传："
        error "  scp bisheng-backend-dev-local.tar gs@<server>:$UPLOAD_DIR/"
        error "  scp bisheng-platform-frontend.zip gs@<server>:$UPLOAD_DIR/"
        exit 1
    fi

    local tar_size zip_size
    tar_size=$(du -h "$BACKEND_TAR" | cut -f1)
    zip_size=$(du -h "$FRONTEND_ZIP" | cut -f1)
    info "后端镜像 tar: $BACKEND_TAR ($tar_size)"
    info "前端产物 zip: $FRONTEND_ZIP ($zip_size)"
}

# ─── 网络一致性检查 ───────────────────────────────────────────
check_network_consistency() {
    header "网络一致性检查"

    local expected_network="${COMPOSE_PROJECT}_default"
    local containers=($BACKEND_CONTAINER $WORKER_CONTAINER $FRONTEND_CONTAINER)
    local net_ok=true

    for c in "${containers[@]}"; do
        local net
        net=$(docker inspect "$c" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null || echo "<not exists>")
        if [[ "$net" == *"$expected_network"* ]]; then
            info "$c 在 $expected_network 网络"
        elif [[ "$net" == "<not exists>" ]]; then
            warn "$c 容器不存在，将在后续步骤创建"
        else
            warn "$c 在错误网络: '$net'（期望 $expected_network）"
            warn "  将通过 docker rm -f + 重建来修复"
            net_ok=false
        fi
    done

    if [[ "$net_ok" == "false" ]]; then
        warn "发现网络不一致，将通过强制重建修复"
    fi
}

# ─── 加载后端镜像 ────────────────────────────────────────────
load_backend_image() {
    header "加载后端镜像"

    step "1/6" "docker load 镜像 tar..."
    if ! docker load -i "$BACKEND_TAR"; then
        error "docker load 失败，tar 文件可能损坏"
        exit 1
    fi
    info "镜像加载完成"

    step "2/6" "打成 compose 引用的 tag: $IMAGE_NAME:$FINAL_IMAGE_TAG"
    docker tag "$IMAGE_NAME:$SOURCE_IMAGE_TAG" "$IMAGE_NAME:$FINAL_IMAGE_TAG"
    info "镜像 tag 完成"

    docker images "$IMAGE_NAME" --format "{{.Repository}}:{{.Tag}}  {{.Size}}"
}

# ─── 重建后端容器 ────────────────────────────────────────────
recreate_backend() {
    header "重建后端容器"

    step "3/6" "删除旧 backend 容器..."
    docker rm -f "$BACKEND_CONTAINER" "$WORKER_CONTAINER" 2>/dev/null || true
    info "旧容器已删除"

    step "4/6" "重建 backend + worker（-p $COMPOSE_PROJECT 必须带）..."
    cd "$COMPOSE_DIR"
    if ! docker compose -p "$COMPOSE_PROJECT" up -d --no-deps --force-recreate backend backend_worker; then
        error "docker compose up 失败"
        error "常见原因："
        error "  1. 忘了 -p $COMPOSE_PROJECT，容器落到 docker_default 网络，连不上 MySQL"
        error "  2. config.yaml 路径或权限错误"
        exit 1
    fi
    info "容器重建命令已执行"
}

# ─── 等待 backend 健康 ───────────────────────────────────────
wait_backend_healthy() {
    header "等待 backend 健康"

    step "5/6" "等待 backend 启动 + 自动跑 alembic 迁移..."
    info "超时: ${BACKEND_HEALTH_TIMEOUT}s, 检查间隔: ${BACKEND_HEALTH_INTERVAL}s"
    info "（api 模式会先跑 alembic upgrade head，迁移失败 entrypoint 会直接退出）"

    local elapsed=0
    while [[ $elapsed -lt $BACKEND_HEALTH_TIMEOUT ]]; do
        # 容器是否还在运行（迁移失败会退出）
        local status
        status=$(docker inspect "$BACKEND_CONTAINER" --format '{{.State.Status}}' 2>/dev/null || echo "missing")
        if [[ "$status" == "missing" ]]; then
            error "backend 容器不存在"
            exit 1
        fi
        if [[ "$status" != "running" ]]; then
            error "backend 容器状态异常: $status"
            error "可能迁移失败导致 entrypoint 退出，查看日志："
            docker logs "$BACKEND_CONTAINER" --tail 50 2>&1 || true
            exit 1
        fi

        # 健康检查
        if docker exec "$BACKEND_CONTAINER" curl -sf http://localhost:7860/health >/dev/null 2>&1; then
            info "backend 健康（耗时 ${elapsed}s）"
            docker ps --filter "name=bisheng-backend" --format "table {{.Names}}\t{{.Status}}"
            return 0
        fi

        sleep "$BACKEND_HEALTH_INTERVAL"
        elapsed=$((elapsed + BACKEND_HEALTH_INTERVAL))
        printf "\r  已等待 %ss / %ss..." "$elapsed" "$BACKEND_HEALTH_TIMEOUT"
    done

    echo ""
    error "backend 健康检查超时（${BACKEND_HEALTH_TIMEOUT}s）"
    error "查看启动日志排查："
    docker logs "$BACKEND_CONTAINER" --tail 80 2>&1 || true
    exit 1
}

# ─── 更新前端产物 ────────────────────────────────────────────
update_frontend() {
    header "更新前端产物"

    step "6/6" "解压前端 zip..."
    mkdir -p "$TEMP_DIR"
    if ! unzip -o -q "$FRONTEND_ZIP" -d "$TEMP_DIR"; then
        error "unzip 失败，zip 文件可能损坏"
        exit 1
    fi
    if [[ ! -f "$TEMP_DIR/index.html" ]]; then
        error "解压后未找到 index.html，构建产物可能不完整"
        exit 1
    fi
    info "前端产物解压完成"

    # 确保 frontend 容器运行（异常情况：容器可能已停止）
    local fe_status
    fe_status=$(docker inspect "$FRONTEND_CONTAINER" --format '{{.State.Status}}' 2>/dev/null || echo "missing")
    if [[ "$fe_status" != "running" ]]; then
        warn "frontend 容器未运行（状态: $fe_status），先启动..."
        if ! docker start "$FRONTEND_CONTAINER" 2>/dev/null; then
            warn "docker start 失败，容器可能不存在，尝试通过 compose 创建..."
            cd "$COMPOSE_DIR"
            docker compose -p "$COMPOSE_PROJECT" up -d --no-deps "$FRONTEND_CONTAINER" 2>/dev/null \
                || docker compose -p "$COMPOSE_PROJECT" up -d --no-deps frontend
            sleep 3
        fi
        fe_status=$(docker inspect "$FRONTEND_CONTAINER" --format '{{.State.Status}}' 2>/dev/null || echo "missing")
        if [[ "$fe_status" != "running" ]]; then
            error "frontend 容器仍无法启动（状态: $fe_status）"
            error "查看日志: docker logs $FRONTEND_CONTAINER"
            exit 1
        fi
        info "frontend 容器已启动"
    else
        info "frontend 容器运行中"
    fi

    # 备份旧产物（清理历史 .bak 避免堆积）+ 拷贝新产物
    info "备份旧产物 + 清理历史 .bak..."
    docker exec "$FRONTEND_CONTAINER" sh -c '
        cd /usr/share/nginx/html/
        rm -rf platform.bak.* 2>/dev/null || true
        if [[ -d platform ]]; then
            mv platform "platform.bak.$(date +%Y%m%d%H%M%S)"
        fi
        mkdir -p platform
    '

    info "拷贝新产物到容器..."
    if ! docker cp "$TEMP_DIR/." "$FRONTEND_CONTAINER:/usr/share/nginx/html/platform/"; then
        error "docker cp 失败"
        exit 1
    fi
    info "前端产物已部署"

    # nginx 语法检查 + reload（关键：改了配置或更新产物后必须 reload 进程）
    info "nginx 语法检查..."
    if ! docker exec "$FRONTEND_CONTAINER" nginx -t; then
        error "nginx -t 失败，配置文件有语法错误"
        error "查看配置: docker exec $FRONTEND_CONTAINER cat /etc/nginx/conf.d/default.conf"
        exit 1
    fi

    info "reload nginx（让新配置生效，避免 502）..."
    if ! docker exec "$FRONTEND_CONTAINER" nginx -s reload; then
        error "nginx reload 失败"
        exit 1
    fi
    sleep 2
    info "nginx 已 reload"
}

# ─── 最终验证 ────────────────────────────────────────────────
final_verification() {
    header "最终验证"

    info "1. 后端 health..."
    if ! docker exec "$BACKEND_CONTAINER" curl -sf http://localhost:7860/health; then
        error "后端 health 检查失败"
        exit 1
    fi
    echo ""

    info "2. 前端页面..."
    local fe_code
    fe_code=$(curl -s -o /dev/null -w "%{http_code}" -I http://localhost:3001/ 2>/dev/null || echo "000")
    if [[ "$fe_code" != "200" ]]; then
        error "前端页面返回 $fe_code（期望 200）"
        error "排查："
        error "  - 502: nginx 配置改了但没 reload？执行 docker exec $FRONTEND_CONTAINER nginx -s reload"
        error "  - 502: backend IP 变了？动态解析应自动恢复，最多 10s"
        exit 1
    fi
    info "前端页面 200 OK"

    info "3. 前端代理后端 API..."
    local api_code
    api_code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3001/api/v1/env 2>/dev/null || echo "000")
    # /api/v1/env 对未登录返回 401 也算正常（说明 nginx 转发到了后端）
    if [[ "$api_code" =~ ^(200|401)$ ]]; then
        info "前端→后端 API 代理正常 (HTTP $api_code)"
    else
        error "前端→后端 API 代理异常: HTTP $api_code"
        exit 1
    fi

    info "4. 网络一致性..."
    local expected_network="${COMPOSE_PROJECT}_default"
    for c in "$BACKEND_CONTAINER" "$WORKER_CONTAINER" "$FRONTEND_CONTAINER"; do
        local net
        net=$(docker inspect "$c" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null || echo "<not exists>")
        if [[ "$net" != *"$expected_network"* ]]; then
            warn "$c 不在 $expected_network 网络（当前: $net）"
        fi
    done

    echo ""
    header "部署完成"
    info "下一步验证：浏览器打开 http://<server_ip>:3001/ 强制刷新（Ctrl+Shift+R）"
}

# ─── 主入口 ──────────────────────────────────────────────────
main() {
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --backend-tar)     BACKEND_TAR="$2"; shift 2 ;;
            --frontend-zip)    FRONTEND_ZIP="$2"; shift 2 ;;
            --upload-dir)      UPLOAD_DIR="$2"; shift 2 ;;
            --compose-dir)    COMPOSE_DIR="$2"; shift 2 ;;
            --no-frontend)     SKIP_FRONTEND=1; shift ;;
            --no-backend)      SKIP_BACKEND=1; shift ;;
            --health-timeout)  BACKEND_HEALTH_TIMEOUT="$2"; shift 2 ;;
            -h|--help)
                cat <<EOF
BiSheng 本地构建产物一键部署脚本

用法: $0 [选项]

选项:
  --backend-tar <path>     后端镜像 tar 路径（默认: $UPLOAD_DIR/bisheng-backend-dev-local.tar）
  --frontend-zip <path>    前端产物 zip 路径（默认: $UPLOAD_DIR/bisheng-platform-frontend.zip）
  --upload-dir <path>      上传目录（默认: $UPLOAD_DIR）
  --compose-dir <path>     compose 目录（默认: $COMPOSE_DIR）
  --no-frontend            跳过前端更新（只更新后端）
  --no-backend             跳过后端更新（只更新前端）
  --health-timeout <sec>   backend 健康检查超时（默认: $BACKEND_HEALTH_TIMEOUT）
  -h, --help               显示帮助

环境变量（可覆盖默认值）:
  UPLOAD_DIR, BACKEND_TAR, FRONTEND_ZIP, COMPOSE_DIR, COMPOSE_PROJECT
  BACKEND_HEALTH_TIMEOUT, FINAL_IMAGE_TAG, SOURCE_IMAGE_TAG

示例:
  $0                                          # 用默认路径部署
  $0 --no-frontend                            # 只更新后端
  $0 --backend-tar /tmp/custom.tar --health-timeout 180
EOF
                exit 0
                ;;
            *)
                error "未知参数: $1（使用 -h 查看帮助）"
                exit 1
                ;;
        esac
    done

    check_prerequisites
    check_network_consistency

    if [[ -z "${SKIP_BACKEND:-}" ]]; then
        load_backend_image
        recreate_backend
        wait_backend_healthy
    else
        info "跳过后端更新（--no-backend）"
    fi

    if [[ -z "${SKIP_FRONTEND:-}" ]]; then
        update_frontend
    else
        info "跳过前端更新（--no-frontend）"
    fi

    final_verification
}

main "$@"
