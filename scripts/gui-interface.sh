#!/usr/bin/env bash
#
# C++ Library Manager —— 启动本地收集服务
#
# 用法:
#   ./scripts/gui-interface.sh             # 默认 http://127.0.0.1:8080
#   GUI_PORT=9000 ./scripts/gui-interface.sh
#   ./scripts/gui-interface.sh --no-open   # 不自动打开浏览器
#
# 真正的服务在 server.py，这里只负责环境检查 + 启动。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

GUI_PORT="${GUI_PORT:-8080}"
GUI_HOST="${GUI_HOST:-127.0.0.1}"

OPEN_BROWSER=1
for arg in "$@"; do
    case "$arg" in
        --no-open) OPEN_BROWSER=0 ;;
        --help|-h)
            echo "用法: $0 [--no-open]"
            echo "环境变量: GUI_PORT(默认 8080)  GUI_HOST(默认 127.0.0.1)"
            exit 0
            ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

# ---- 检查 git ----
if ! command -v git >/dev/null 2>&1; then
    echo "错误: 找不到 git，请先安装 https://git-scm.com/downloads" >&2
    exit 1
fi

# ---- 检查 python ----
PYTHON=""
for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "错误: 需要 Python 3.8 或更高版本" >&2
    echo "下载: https://www.python.org/downloads/" >&2
    exit 1
fi

cd "$PROJECT_ROOT"

# ---- 打开浏览器（延迟一下等服务起来）----
if [ "$OPEN_BROWSER" -eq 1 ]; then
    URL="http://${GUI_HOST}:${GUI_PORT}/"
    (
        sleep 1.2
        if command -v xdg-open >/dev/null 2>&1; then
            xdg-open "$URL" >/dev/null 2>&1 || true
        elif command -v open >/dev/null 2>&1; then
            open "$URL" >/dev/null 2>&1 || true
        elif command -v start >/dev/null 2>&1; then
            start "" "$URL" >/dev/null 2>&1 || true
        fi
    ) &
fi

exec "$PYTHON" "$PROJECT_ROOT/server.py" --port "$GUI_PORT" --host "$GUI_HOST"
