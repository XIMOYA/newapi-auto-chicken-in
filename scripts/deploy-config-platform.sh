#!/usr/bin/env bash
# scripts/deploy-config-platform.sh
# 一键部署「NewAPI 签到配置管理平台」到 VPS（Ubuntu 24.04）
#
# 作用：
#   1. 本地构建 Linux/amd64 单二进制（前端已嵌入）
#   2. 上传二进制到 VPS
#   3. VPS 上自动完成：systemd 服务 + Nginx 反向代理 + HTTPS（certbot）
#
# 用法（在你自己的电脑上执行，而不是 VPS）：
#   bash scripts/deploy-config-platform.sh
#
# 可配置环境变量（全部有默认值，按需覆盖）：
#   DOMAIN=你的域名          # 必须已解析到 VPS，用于 HTTPS 证书
#   VPS_USER=root            # SSH 用户
#   VPS_HOST=1.2.3.4         # VPS 公网 IP
#   VPS_PORT=22              # SSH 端口
#   CERTBOT_EMAIL=you@x.com  # certbot 注册邮箱（HTTPS 证书通知用）
#
# 示例：
#   DOMAIN=config.ximoya.top VPS_HOST=103.x.x.x CERTBOT_EMAIL=a@b.com bash scripts/deploy-config-platform.sh
#
# 前置要求：
#   - 本地已装 node/npm/go（构建用）
#   - 本机可免密 ssh 到 VPS（或会提示输入密码）
#   - VPS 为 Ubuntu 24.04（脚本会自动安装 nginx/certbot，需要 root 权限）
set -euo pipefail

# ===== 可配置项 =====
DOMAIN="${DOMAIN:-}"
VPS_USER="${VPS_USER:-root}"
VPS_HOST="${VPS_HOST:-}"
VPS_PORT="${VPS_PORT:-22}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
APP_NAME="newapi-config-server"
APP_DIR="/opt/${APP_NAME}"
SERVICE_PORT=8080

if [[ -z "$VPS_HOST" ]]; then
  echo "❌ 请设置 VPS_HOST（VPS 公网 IP）："
  echo "   VPS_HOST=1.2.3.4 bash scripts/deploy-config-platform.sh"
  exit 1
fi
if [[ -z "$DOMAIN" ]]; then
  echo "⚠️  未设置 DOMAIN，将跳过 HTTPS（仅 HTTP 部署，不推荐用于生产）。"
  echo "   配置方法：DOMAIN=config.xxx.com bash scripts/deploy-config-platform.sh"
fi

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_TARGET="${VPS_USER}@${VPS_HOST}"
SSH_ARGS=(-p "$VPS_PORT" -o StrictHostKeyChecking=accept-new)

echo "=============================================================="
echo " NewAPI 配置管理平台 · 一键部署"
echo " 目标: ${SSH_TARGET}  域名: ${DOMAIN:-（无，仅 HTTP）}"
echo "=============================================================="

echo
echo "==> [1/5] 本地构建 Linux/amd64 单二进制"
bash "$ROOT/scripts/build-config-platform.sh"
BIN="$ROOT/server/$APP_NAME"
[[ -f "$BIN" ]] || { echo "❌ 构建产物不存在: $BIN"; exit 1; }

echo
echo "==> [2/5] 生成随机 JWT 密钥（写入远端环境文件，勿外泄）"
JWT_SECRET="$(openssl rand -hex 32)"
if [[ -z "$JWT_SECRET" ]]; then
  JWT_SECRET="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi

echo
echo "==> [3/5] 上传二进制到 VPS"
ssh "${SSH_ARGS[@]}" "$SSH_TARGET" "mkdir -p '$APP_DIR' && mkdir -p '$APP_DIR/data'"
scp -P "$VPS_PORT" "$BIN" "${SSH_TARGET}:${APP_DIR}/"

echo
echo "==> [4/5] VPS 上安装 systemd 服务 + Nginx + HTTPS"
ssh "${SSH_ARGS[@]}" "$SSH_TARGET" \
  "APP_NAME='$APP_NAME' APP_DIR='$APP_DIR' SERVICE_PORT='$SERVICE_PORT' DOMAIN='$DOMAIN' JWT_SECRET='$JWT_SECRET' CERTBOT_EMAIL='$CERTBOT_EMAIL' VPS_USER='$VPS_USER' bash -s" <<'REMOTE'
set -euo pipefail

echo "   - 安装依赖（nginx / certbot / python3-certbot-nginx）"
export DEBIAN_FRONTEND=noninteractive
if ! command -v nginx >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq nginx >/dev/null
fi
if [[ -n "$DOMAIN" ]] && ! command -v certbot >/dev/null 2>&1; then
  apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
fi

echo "   - 写入环境文件 /etc/${APP_NAME}.env"
cat > "/etc/${APP_NAME}.env" <<EOF
NCF_DB_PATH=${APP_DIR}/data/config.db
NCF_JWT_SECRET=${JWT_SECRET}
NCF_HTTP_ADDR=127.0.0.1:${SERVICE_PORT}
NCF_ADMIN_USER=admin
NCF_ADMIN_PASS=admin123456
EOF
chmod 600 "/etc/${APP_NAME}.env"

echo "   - 写入 systemd 服务"
cat > "/etc/systemd/system/${APP_NAME}.service" <<EOF
[Unit]
Description=NewAPI 签到配置管理平台
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
EnvironmentFile=/etc/${APP_NAME}.env
ExecStart=${APP_DIR}/${APP_NAME}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "${APP_NAME}" >/dev/null 2>&1 || true
systemctl restart "${APP_NAME}"
sleep 1
if ! systemctl is-active --quiet "${APP_NAME}"; then
  echo "❌ systemd 服务启动失败，日志："
  journalctl -u "${APP_NAME}" -n 20 --no-pager || true
  exit 1
fi
echo "   - systemd 服务运行中 ✓"

if [[ -n "$DOMAIN" ]]; then
  echo "   - 写入 Nginx 配置（HTTP，certbot 随后升级为 HTTPS）"
  cat > "/etc/nginx/sites-available/${APP_NAME}" <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:${SERVICE_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  ln -sf "/etc/nginx/sites-available/${APP_NAME}" "/etc/nginx/sites-enabled/${APP_NAME}"
  nginx -t >/dev/null && systemctl reload nginx

  echo "   - 申请 HTTPS 证书（certbot）"
  if [[ -n "$CERTBOT_EMAIL" ]]; then
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect || {
      echo "⚠️  certbot 自动申请失败（可能是 DNS 未生效或 80 端口未开放），可稍后手动执行："
      echo "   certbot --nginx -d $DOMAIN"
    }
  else
    echo "⚠️  未提供 CERTBOT_EMAIL，跳过自动申请。可稍后手动执行："
    echo "   certbot --nginx -d $DOMAIN"
  fi
else
  echo "   - 未设置 DOMAIN，跳过 Nginx/HTTPS（服务已监听 127.0.0.1:${SERVICE_PORT}）"
fi
REMOTE

echo
echo "==> [5/5] 部署完成！"
if [[ -n "$DOMAIN" ]]; then
  echo "  访问: https://${DOMAIN}"
else
  echo "  访问: http://${VPS_HOST}:${SERVICE_PORT}  （需先放行安全组/防火墙端口）"
fi
echo "  初始账号: admin / admin123456"
echo "  ⚠️ 登录后请立即修改默认密码！JWT 密钥已写入 /etc/${APP_NAME}.env"
