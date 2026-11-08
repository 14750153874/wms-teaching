#!/usr/bin/env bash
# ============================================================================
# WMS 服务器防火墙配置（Ubuntu 22.04 LTS + ufw）
# 对应《项目方案与技术文档 V2.0》6.2 / 7.4：
#   "仅放行 80、443 与自定义 SSH 端口（如 22022）；云安全组与系统 ufw 双层
#    配置，默认拒绝入站"；"严禁把 3306/6379/8080 映射到公网"。
#
# 用法（需要 root 权限）：
#     sudo bash firewall-ufw.sh --dry-run   # 只打印将要执行的命令，不生效
#     sudo bash firewall-ufw.sh             # 实际应用规则
#
# 可覆盖变量：
#     SSH_PORT=22022 HTTP_PORT=80 HTTPS_PORT=443 APP_PORT=8000 sudo -E bash firewall-ufw.sh
#
# 说明：
#   - 云服务器还必须同步收紧控制台安全组（双层防护）；
#   - 应用端口（默认 8000）不对公网开放，由同机 Nginx 反代 127.0.0.1:8000；
#   - 应用进程启动方式见 DEPLOY.md「后端启动命令」。
# ============================================================================
set -euo pipefail

SSH_PORT="${SSH_PORT:-22022}"
HTTP_PORT="${HTTP_PORT:-80}"
HTTPS_PORT="${HTTPS_PORT:-443}"
APP_PORT="${APP_PORT:-8000}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

run() {
  echo "+ $*"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    "$@"
  fi
}

if ! command -v ufw >/dev/null 2>&1; then
  echo "[ERROR] 未安装 ufw。请先执行：apt update && apt install -y ufw" >&2
  exit 1
fi

echo "== WMS 防火墙配置（ufw）$([[ "$DRY_RUN" -eq 1 ]] && echo '· dry-run 模式') =="

# 默认策略：拒绝所有入站、允许所有出站
run ufw default deny incoming
run ufw default allow outgoing

# 仅放行 SSH / HTTP / HTTPS
run ufw allow "${SSH_PORT}/tcp" comment 'SSH (custom port)'
run ufw allow "${HTTP_PORT}/tcp" comment 'HTTP (ACME + redirect)'
run ufw allow "${HTTPS_PORT}/tcp" comment 'HTTPS'

# 应用端口只允许本机访问（Nginx 反代）；不要对公网放行
run ufw allow from 127.0.0.1 to any port "${APP_PORT}" proto tcp comment 'WMS API (localhost only)'

# 启用（--force 跳过交互确认）
run ufw --force enable

echo
if [[ "$DRY_RUN" -eq 0 ]]; then
  echo "== 当前规则（ufw status verbose） =="
  ufw status verbose
  echo
  echo "== 建议验证 =="
  echo "  1) ss -lntp            # 宿主机只应看到 ${HTTP_PORT}、${HTTPS_PORT} 与 ${SSH_PORT}（及本机回环）"
  echo "  2) 外网端口扫描        # 仅 3 个端口对外可见（文档 8.1 验收项）"
  echo "  3) 云控制台安全组      # 与以上规则保持一致（双层防护）"
else
  echo "（dry-run 模式：以上命令未实际执行）"
fi
