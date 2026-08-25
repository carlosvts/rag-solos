#!/usr/bin/env bash
# Ponto de entrada único do cliente de áudio na TV Box.
# 1) Regenera o .env na raiz do repo (IP do PC + índices do Anker, com retry)
# 2) Roda o client
#
# Overrides opcionais (variáveis de ambiente):
#   PC_SERVER_IP=192.168.0.111 ./run.sh     -> pula toda a detecção de IP
#   MAX_RETRIES=10 ./run.sh                 -> mais tentativas antes de desistir
#   RETRY_DELAY_S=5 ./run.sh                -> espera maior entre tentativas
#   NETWORK_IFACE=wlan0 ./run.sh            -> força a interface de rede
#   SKIP_ENV=1 ./run.sh                     -> usa o .env existente, sem regerar

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${SKIP_ENV:-0}" = "1" ]; then
    echo "==> SKIP_ENV=1 — usando o .env existente."
else
    echo "==> Regenerando .env..."
    bash "$PROJECT_ROOT/scripts/generate_env.sh"
fi

echo "==> Iniciando client_armbian.py..."
exec python3 "$PROJECT_ROOT/client_armbian.py" "$@"
