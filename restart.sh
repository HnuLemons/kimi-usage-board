#!/bin/bash
# restart.sh — 改完 .env 后运行此脚本即可重启看板服务
# 用法: ./restart.sh

cd "$(dirname "$0")"

# 从 .env 读取端口（默认 8080）
PORT=$(grep -E '^PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d ' ')
PORT=${PORT:-8080}

# 1. 停掉旧进程
PIDS=$(lsof -ti :"$PORT" 2>/dev/null)
if [ -n "$PIDS" ]; then
  echo "⏹  停止旧进程 (PID: $(echo $PIDS | tr '\n' ' '))"
  echo "$PIDS" | xargs kill 2>/dev/null
  sleep 1
fi

# 2. 后台启动新进程，日志写入 server.log
echo "🚀 启动服务…"
nohup uv run kimi-board > server.log 2>&1 &

# 3. 等待服务就绪（最多 15 秒）
for i in $(seq 1 30); do
  if curl -s -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then
    break
  fi
  # 进程提前退出说明启动失败，直接打印日志
  if ! pgrep -f "kimi-board" > /dev/null; then
    echo "❌ 启动失败，日志如下："
    cat server.log
    exit 1
  fi
  sleep 0.5
done

if ! curl -s -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then
  echo "❌ 服务 15 秒内未就绪，请查看日志: cat server.log"
  exit 1
fi

# 4. 打印访问地址
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "127.0.0.1")
echo ""
echo "✅ 重启完成，浏览器访问："
echo "   本机:   http://127.0.0.1:$PORT"
echo "   局域网: http://$LAN_IP:$PORT"
echo ""
echo "📄 查看日志: tail -f server.log"
echo "⏹  停止服务: lsof -ti :$PORT | xargs kill"
