#!/usr/bin/env bash
# Gera o .env na raiz do repositório detectando automaticamente:
#   - índices do microfone Anker (ALSA + PyAudio), com retry
#   - IP do PC (override > cache-por-ping > cache-por-porta > scan de rede > último recurso)
# Roda NA TV BOX, antes de iniciar o client.
#
# Portado do projeto Go2 (scripts/generate_env.sh). Mudanças em relação a ele:
#   1) O .env vai para a RAIZ do repo, não para config/.env — neste projeto o
#      client_armbian.py fica na raiz e procura o .env ao lado dele.
#   2) BEEP_FILE aponta para ./media/beep.wav deste repo.
#   3) ANKER_CARD_INDEX é detectado por `aplay -l` (playback) e não só por
#      `arecord -l` (captura). Aqui o Anker também TOCA a resposta do TTS, não
#      só o beep, então o card de playback precisa estar certo. Se os dois
#      diverfirem, o de playback é o que vale para o ANKER_CARD_INDEX.
#
# Overrides opcionais (variáveis de ambiente):
#   PC_SERVER_IP=192.168.0.111 ./scripts/generate_env.sh
#   MAX_RETRIES=10 ./scripts/generate_env.sh
#   RETRY_DELAY_S=5 ./scripts/generate_env.sh
#   NETWORK_IFACE=wlan0 ./scripts/generate_env.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"
IP_CACHE_FILE="$PROJECT_ROOT/.last_known_pc_ip"

TCP_PORT="${TCP_PORT:-9876}"
MAX_RETRIES="${MAX_RETRIES:-5}"
RETRY_DELAY_S="${RETRY_DELAY_S:-3}"
NETWORK_IFACE="${NETWORK_IFACE:-}"   # opcional: force a interface certa (ex: wlan0)

# Guarda o override ANTES de qualquer coisa no script mexer em PC_SERVER_IP.
PC_SERVER_IP_OVERRIDE="${PC_SERVER_IP:-}"

# ── Helper: testa se host:porta responde, rápido -----------------------------
port_is_open() {
    local host="$1" port="$2"
    timeout 2 bash -c "echo >/dev/tcp/$host/$port" 2>/dev/null
}

# ── Helper: testa se host responde a ping (mais barato, não depende do
#    servidor já estar escutando na porta) -----------------------------------
host_is_up() {
    local host="$1"
    ping -c 1 -W 1 "$host" >/dev/null 2>&1
}

# ── Helper: último IP conhecido, de qualquer fonte disponível ----------------
last_known_ip() {
    if [ -f "$IP_CACHE_FILE" ]; then
        cat "$IP_CACHE_FILE" 2>/dev/null || true
        return
    fi
    if [ -f "$ENV_FILE" ]; then
        grep -m1 "^PC_SERVER_IP=" "$ENV_FILE" 2>/dev/null | cut -d= -f2 || true
    fi
}

# ── Helper: detecta o CIDR da rede local correta -----------------------------
# Ignora interfaces virtuais que podiam ser escolhidas por engano antes da
# interface física real (era a causa mais provável do scan nunca achar nada).
detect_cidr() {
    if [ -n "$NETWORK_IFACE" ]; then
        ip -o -4 addr show dev "$NETWORK_IFACE" scope global | awk '{print $4}' | head -n1
        return
    fi
    ip -o -4 addr show scope global \
        | awk '{print $2, $4}' \
        | grep -Ev '^(docker|veth|br-|virbr|lo)' \
        | awk '{print $2}' \
        | head -n1
}

# ── IP do PC ------------------------------------------------------------------
find_pc_ip() {
    # 1) Override explícito do usuário — sempre tem prioridade máxima
    if [ -n "$PC_SERVER_IP_OVERRIDE" ]; then
        echo "  (usando PC_SERVER_IP definido manualmente: $PC_SERVER_IP_OVERRIDE)" >&2
        echo "$PC_SERVER_IP_OVERRIDE"
        return 0
    fi

    local cached
    cached="$(last_known_ip)"

    # 2) Cache confirmado pela PORTA (servidor já rodando) — o ideal
    if [ -n "$cached" ] && port_is_open "$cached" "$TCP_PORT"; then
        echo "  (usando último IP conhecido, porta confirmada: $cached)" >&2
        echo "$cached"
        return 0
    fi

    # 2b) Cache confirmado por PING (host existe, mas servidor pode não ter
    #     subido ainda — resolve o problema de ordem de boot TV-BOX x PC).
    #     Neste projeto isso é ainda mais provável que no Go2: o container
    #     carrega Whisper e Piper antes de abrir o socket, então há uma janela
    #     de dezenas de segundos em que o PC responde a ping mas a 9876 não.
    if [ -n "$cached" ] && host_is_up "$cached"; then
        echo "  AVISO: host $cached responde a ping mas a porta $TCP_PORT ainda não está aberta." >&2
        echo "  Provavelmente o container ainda está carregando os modelos. Usando $cached mesmo assim." >&2
        echo "$cached"
        return 0
    fi

    # 3) Scan completo da sub-rede
    echo "  Cache indisponível/não respondeu, escaneando a rede (porta $TCP_PORT)..." >&2

    local cidr
    cidr="$(detect_cidr)"
    if [ -n "$cidr" ]; then
        echo "  (escaneando sub-rede: $cidr)" >&2
        if command -v nmap >/dev/null 2>&1; then
            local found
            found="$(nmap -p "$TCP_PORT" --open -n "$cidr" -oG - 2>/dev/null \
                | awk -v p="$TCP_PORT" '/Ports: .*'"$TCP_PORT"'\/open/{print $2; exit}')"
            if [ -n "$found" ]; then
                echo "$found"
                return 0
            fi
        else
            local base
            base="$(echo "$cidr" | sed -E 's|([0-9]+\.[0-9]+\.[0-9]+)\.[0-9]+/.*|\1|')"
            local tmp_result
            tmp_result="$(mktemp)"
            for i in $(seq 1 254); do
                (
                    port_is_open "$base.$i" "$TCP_PORT" && echo "$base.$i" >> "$tmp_result"
                ) &
                if (( i % 40 == 0 )); then wait; fi
            done
            wait
            local found
            found="$(head -n1 "$tmp_result" 2>/dev/null || true)"
            rm -f "$tmp_result"
            if [ -n "$found" ]; then
                echo "$found"
                return 0
            fi
        fi
    else
        echo "  (não foi possível determinar a sub-rede local para o scan — verifique NETWORK_IFACE)" >&2
    fi

    # 4) Último recurso: usa o cache mesmo sem confirmar que responde agora
    if [ -n "$cached" ]; then
        echo "  AVISO: scan não encontrou nada. Usando último IP conhecido sem confirmação: $cached" >&2
        echo "$cached"
        return 0
    fi

    return 1
}

echo "==> Detectando IP do servidor (PC)..."
PC_SERVER_IP=""
for attempt in $(seq 1 "$MAX_RETRIES"); do
    if PC_SERVER_IP="$(find_pc_ip)"; then
        break
    fi
    echo "  Tentativa $attempt/$MAX_RETRIES falhou. Aguardando rede estabilizar..." >&2
    sleep "$RETRY_DELAY_S"
done

if [ -z "$PC_SERVER_IP" ]; then
    echo "ERRO: nenhum servidor encontrado (nem via scan, nem via cache) após $MAX_RETRIES tentativas." >&2
    echo "Defina manualmente: PC_SERVER_IP=<ip> ./run.sh" >&2
    echo "Ou force a interface certa: NETWORK_IFACE=wlan0 ./run.sh" >&2
    exit 1
fi
echo "  PC_SERVER_IP: $PC_SERVER_IP"
echo "$PC_SERVER_IP" > "$IP_CACHE_FILE"

# ── Microfone Anker (ALSA + PyAudio), com retry -------------------------------
echo "==> Detectando dispositivo Anker..."

ANKER_CARD_INDEX=""
MIC_DEVICE_INDEX=""
for attempt in $(seq 1 "$MAX_RETRIES"); do
    # Card de PLAYBACK: é por ele que sai o beep E a resposta do TTS.
    ANKER_CARD_INDEX="$(aplay -l 2>/dev/null | grep -i "anker" | head -n1 | sed -n 's/^card \([0-9]*\):.*/\1/p' || true)"

    # Fallback: se o Anker não aparecer em aplay -l, tenta arecord -l. Em geral
    # é o mesmo card, mas playback é o que importa aqui.
    if [ -z "$ANKER_CARD_INDEX" ]; then
        ANKER_CARD_INDEX="$(arecord -l 2>/dev/null | grep -i "anker" | head -n1 | sed -n 's/^card \([0-9]*\):.*/\1/p' || true)"
        [ -n "$ANKER_CARD_INDEX" ] && \
            echo "  AVISO: Anker não listado em 'aplay -l'; usando o card de captura ($ANKER_CARD_INDEX)." >&2
    fi

    if [ -n "$ANKER_CARD_INDEX" ] && MIC_DEVICE_INDEX="$(python3 "$SCRIPT_DIR/find_mic_index.py" 2>/dev/null)"; then
        break
    fi
    echo "  Tentativa $attempt/$MAX_RETRIES falhou. Aguardando dispositivo de áudio estabilizar..." >&2
    ANKER_CARD_INDEX=""
    MIC_DEVICE_INDEX=""
    sleep "$RETRY_DELAY_S"
done

if [ -z "$ANKER_CARD_INDEX" ] || [ -z "$MIC_DEVICE_INDEX" ]; then
    echo "ERRO: microfone Anker não detectado/funcional após $MAX_RETRIES tentativas." >&2
    echo "Rode manualmente para depurar: aplay -l   /   arecord -l   /   python3 scripts/find_mic_index.py" >&2
    exit 1
fi
echo "  Card ALSA playback (Anker): $ANKER_CARD_INDEX"
echo "  Índice PyAudio (Anker): $MIC_DEVICE_INDEX"

# ── Valores fixos (sobrescrevíveis via variável de ambiente) ------------------
AUDIO_SAMPLE_RATE="${AUDIO_SAMPLE_RATE:-16000}"
AUDIO_CHANNELS="${AUDIO_CHANNELS:-1}"
AUDIO_CHUNK_MS="${AUDIO_CHUNK_MS:-30}"
BEEP_FILE="${BEEP_FILE:-$PROJECT_ROOT/media/beep.wav}"

mkdir -p "$(dirname "$BEEP_FILE")"

cat > "$ENV_FILE" <<EOF
#=============================================================
# .env DA TV BOX — gerado automaticamente por scripts/generate_env.sh
# NÃO edite os índices/IP manualmente — eles mudam a cada boot/reconexão.
# Gerado em: $(date '+%Y-%m-%d %H:%M:%S')
#=============================================================
PC_SERVER_IP=$PC_SERVER_IP
TCP_PORT=$TCP_PORT

# Áudio (deve casar com o .env do server)
AUDIO_SAMPLE_RATE=$AUDIO_SAMPLE_RATE
AUDIO_CHANNELS=$AUDIO_CHANNELS
AUDIO_CHUNK_MS=$AUDIO_CHUNK_MS

# Microfone Anker — detectado automaticamente a cada execução
MIC_DEVICE_INDEX=$MIC_DEVICE_INDEX
ANKER_CARD_INDEX=$ANKER_CARD_INDEX

# Caminho ABSOLUTO do beep na TV Box
BEEP_FILE=$BEEP_FILE
EOF

echo "==> .env gerado em: $ENV_FILE"
