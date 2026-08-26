#!/usr/bin/env bash
# Vitalis 公网服务器启动脚本
# 用法: ./run.sh            （前台运行）
#       ./run.sh --daemon   （后台常驻，日志 /tmp/vitalis.log）
set -e
cd "$(dirname "$0")"

# 公网部署必须显式提供 HTTPS 地址；缺省仅输出本机入口。
PUBLIC_URL="${VITALIS_PUBLIC_URL:-http://127.0.0.1:${PORT:-8000}}"
export ZEPP_REDIRECT_URI="${ZEPP_REDIRECT_URI:-$PUBLIC_URL/api/v1/connect/zepp/callback}"

echo "[Vitalis] 回调地址: $ZEPP_REDIRECT_URI"
echo "[Vitalis] 扫码页:   $PUBLIC_URL/api/v1/connect/zepp/scan"

if [ "$1" = "--daemon" ]; then
  nohup .venv/bin/python -m vitalis.main > /tmp/vitalis.log 2>&1 &
  echo "[Vitalis] 已后台启动 (pid $!) → 日志 /tmp/vitalis.log"
else
  exec .venv/bin/python -m vitalis.main
fi
