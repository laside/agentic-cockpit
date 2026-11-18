param([string]$Project = "mon-app")
$env:OLLAMA_FLASH_ATTENTION = "1"
$env:OLLAMA_KV_CACHE_TYPE = "q8_0"
Write-Host "Vérification du nœud NAS (192.168.0.10:11434) et de la RTX 3070 locale..." -ForegroundColor Cyan
wsl.exe -e bash -lc "~/agentic-cockpit/bin/cockpit open $Project"
