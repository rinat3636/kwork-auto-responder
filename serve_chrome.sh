#!/bin/bash
# Запуск headless Chromium (из playwright) с remote-debugging для CDP.
set -e
CHROME=$(ls -d /root/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1)
if [ -z "$CHROME" ]; then echo "chromium not found"; exit 1; fi
mkdir -p /root/kwork/chrome-profile
exec "$CHROME" \
  --headless=new \
  --no-sandbox \
  --disable-gpu \
  --disable-dev-shm-usage \
  --remote-debugging-port=29229 \
  --remote-debugging-address=127.0.0.1 \
  --user-data-dir=/root/kwork/chrome-profile \
  --window-size=1366,900 \
  about:blank
