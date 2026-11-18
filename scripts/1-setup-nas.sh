#!/usr/bin/env bash
set -euo pipefail
mkdir -p /etc/systemd/system/ollama.service.d
cat << 'EOF' > /etc/systemd/system/ollama.service.d/cockpit-override.conf
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_KEEP_ALIVE=-1"
EOF
systemctl daemon-reload && systemctl restart ollama
sleep 2
curl -s http://127.0.0.1:11434/api/generate -d '{"model":"Gemma4-ctx:32k","prompt":"Init KV Cache","stream":false,"keep_alive":-1}' >/dev/null || true
echo "OK : nas-tour configuré (KV Cache q8_0 + Gemma4-ctx:32k épinglé en VRAM)."
