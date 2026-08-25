#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  client_armbian.py — O OUVIDO E A BOCA (roda na TV Box, FORA do Docker)    ║
║                                                                              ║
║  Captura áudio do Anker PowerConf S3 e faz streaming para o container        ║
║  do server (no PC) via TCP. Pelo canal de volta recebe:                     ║
║      • beep de confirmação da wake word                                      ║
║      • o áudio WAV da resposta, gerado pelo TTS no server                   ║
║  Os dois são tocados pelo próprio Anker via aplay.                          ║
║                                                                              ║
║  MUDANÇA em relação à versão do Go2: o canal reverso deixou de ser um       ║
║  único byte e passou a ser um frame com header (opcode + tamanho), porque    ║
║  agora ele carrega payload de áudio. Ver protocol.py no server.             ║
║                                                                              ║
║  Por que fora do Docker: depende do hardware específico da TV Box            ║
║  (Anker via ALSA, índice PyAudio). Roda Python direto — simples e estável.  ║
║                                                                              ║
║  Hardware:                                                                    ║
║    Captura  : 48000Hz (nativo Anker) → decimação ÷3 → 16000Hz enviado       ║
║    Playback : aplay -D plughw:ANKER_CARD_INDEX,0 <arquivo.wav>              ║
║                                                                              ║
║  Configuração via .env na raiz deste repositório:                            ║
║    PC_SERVER_IP=<IP do PC>   TCP_PORT=9876   MIC_DEVICE_INDEX=6              ║
║    ANKER_CARD_INDEX=1   BEEP_FILE=<caminho absoluto do beep.wav>            ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import math
import os
import struct
import sys
import tempfile
import wave

import numpy as np
import pyaudio
from dotenv import load_dotenv

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OUVIDO] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ─── Variáveis de ambiente ────────────────────────────────────────────────────
# Procura o .env na mesma pasta do script (raiz deste repositório).
# Fallback: ../config/.env, para quem já tinha esse layout do projeto Go2.
_script_dir = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(_script_dir, ".env")

if not os.path.exists(_env_path):
    _env_path = os.path.normpath(os.path.join(_script_dir, "..", "config", ".env"))

load_dotenv(dotenv_path=_env_path)
print(f"[DEBUG] .env carregado de: {_env_path} (existe: {os.path.exists(_env_path)})")

PC_SERVER_IP: str = os.getenv("PC_SERVER_IP", "")
TCP_PORT: int     = int(os.getenv("TCP_PORT", "9876"))
MIC_DEVICE_INDEX  = int(os.getenv("MIC_DEVICE_INDEX", "6"))
TARGET_RATE       = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
CHANNELS          = int(os.getenv("AUDIO_CHANNELS", "1"))
CHUNK_MS          = int(os.getenv("AUDIO_CHUNK_MS", "30"))

# Índice ALSA do Anker para PLAYBACK (card 1). Confirme com `aplay -l`.
ANKER_CARD_INDEX  = int(os.getenv("ANKER_CARD_INDEX", "1"))

# Caminho do beep. Use caminho ABSOLUTO no .env da TV Box.
BEEP_FILE: str    = os.getenv("BEEP_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "media", "beep.wav"))
# Se o caminho vier relativo, resolve a partir da pasta do script
if not os.path.isabs(BEEP_FILE):
    BEEP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), BEEP_FILE)

if not PC_SERVER_IP:
    log.critical("PC_SERVER_IP não definido no .env! Abortando.")
    sys.exit(1)

# ─── Constantes de áudio ─────────────────────────────────────────────────────
CAPTURE_RATE: int  = 48000                            # taxa nativa do Anker
DOWNSAMPLE_RATIO   = CAPTURE_RATE // TARGET_RATE      # 3
CHUNK_SAMPLES_48K  = CAPTURE_RATE * CHUNK_MS // 1000   # 1440 amostras
CHUNK_SAMPLES_16K  = TARGET_RATE  * CHUNK_MS // 1000   # 480 amostras
CHUNK_BYTES_16K    = CHUNK_SAMPLES_16K * 2             # 960 bytes
RECONNECT_DELAY    = 3.0

# ─── Protocolo do canal reverso (espelha server/protocol.py) ─────────────────
OP_BEEP           = 0x01     # servidor → cliente: toca o beep local
OP_AUDIO          = 0x02     # servidor → cliente: payload é um WAV completo
HEADER_FORMAT     = ">BI"    # opcode (uint8) + tamanho (uint32 big-endian)
HEADER_SIZE       = struct.calcsize(HEADER_FORMAT)     # 5 bytes
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024


# ═══════════════════════════════════════════════════════════════════════════════
#  SEÇÃO 1: Playback pelo Anker
# ═══════════════════════════════════════════════════════════════════════════════

def generate_beep(path: str, freq: int = 880, duration: float = 0.18,
                  volume: float = 0.65, rate: int = 44100) -> None:
    """Gera beep WAV programaticamente (onda senoidal com fade de 10ms)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    n = int(rate * duration)
    fade = int(rate * 0.01)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        for i in range(n):
            env = min(i / fade, 1.0) * min((n - i) / fade, 1.0)
            val = int(volume * 32767 * env * math.sin(2 * math.pi * freq * i / rate))
            wf.writeframes(struct.pack("<h", val))
    log.info("✓ beep.wav gerado: %s", path)


async def play_wav_anker(path: str, timeout: float = 120.0) -> None:
    """Toca um WAV pelo Anker via aplay (ALSA). Falha silenciosamente."""
    alsa_device = f"plughw:{ANKER_CARD_INDEX},0"
    try:
        proc = await asyncio.create_subprocess_exec(
            "aplay", "-D", alsa_device, "--quiet", path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except FileNotFoundError:
        log.warning("Playback: aplay não encontrado.")
    except asyncio.TimeoutError:
        log.warning("Playback: timeout.")
    except Exception as e:
        log.warning("Playback: %s", e)


async def play_beep_anker() -> None:
    log.info("🔔 Beep (wake word reconhecida).")
    await play_wav_anker(BEEP_FILE, timeout=3.0)


async def play_response_audio(wav_bytes: bytes) -> None:
    """Grava o WAV recebido em arquivo temporário e toca pelo Anker."""
    log.info("🔊 Tocando resposta (%d KB)...", len(wav_bytes) // 1024)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(wav_bytes)
            tmp_path = tmp.name
        await play_wav_anker(tmp_path)
        log.info("✓ Resposta reproduzida.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
#  SEÇÃO 2: Áudio
# ═══════════════════════════════════════════════════════════════════════════════

def downsample(data_bytes: bytes) -> bytes:
    """48kHz → 16kHz por decimação (pega 1 de cada 3 amostras)."""
    samples = np.frombuffer(data_bytes, dtype=np.int16)
    return samples[::DOWNSAMPLE_RATIO].tobytes()


def _list_audio_devices() -> None:
    pa = pyaudio.PyAudio()
    log.info("Dispositivos de entrada disponíveis:")
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            log.info("  [%d] %s", i, info["name"])
    pa.terminate()


def _open_audio_stream(pa: pyaudio.PyAudio) -> pyaudio.Stream:
    """Abre stream de captura em 48kHz (taxa nativa do Anker)."""
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=CAPTURE_RATE,
        input=True,
        input_device_index=MIC_DEVICE_INDEX,
        frames_per_buffer=CHUNK_SAMPLES_48K,
    )
    log.info("Microfone aberto: idx=%d | captura=%dHz → envia=%dHz | chunk=%dms",
             MIC_DEVICE_INDEX, CAPTURE_RATE, TARGET_RATE, CHUNK_MS)
    return stream


async def _read_audio_chunk(loop, stream) -> bytes:
    """Lê 30ms @ 48kHz de forma não-bloqueante."""
    return await loop.run_in_executor(None, stream.read, CHUNK_SAMPLES_48K, False)


# ═══════════════════════════════════════════════════════════════════════════════
#  SEÇÃO 3: Listener do canal reverso (beep + áudio da resposta)
# ═══════════════════════════════════════════════════════════════════════════════

async def _listen_server_commands(reader: asyncio.StreamReader) -> None:
    """
    Lê frames do servidor: header de 5 bytes + payload.
    O playback roda em task separada para não travar a leitura do socket.
    """
    log.info("👂 Listener de comandos do servidor ativo.")
    try:
        while True:
            header = await reader.readexactly(HEADER_SIZE)
            opcode, length = struct.unpack(HEADER_FORMAT, header)

            if length > MAX_PAYLOAD_BYTES:
                log.error("Payload absurdo (%d bytes) — derrubando a conexão.", length)
                raise ConnectionResetError("frame inválido")

            payload = await reader.readexactly(length) if length else b""

            if opcode == OP_BEEP:
                asyncio.create_task(play_beep_anker())
            elif opcode == OP_AUDIO:
                asyncio.create_task(play_response_audio(payload))
            else:
                log.warning("Opcode desconhecido: 0x%02x (ignorado)", opcode)

    except asyncio.IncompleteReadError:
        log.debug("Servidor fechou o canal reverso.")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.debug("Listener encerrado: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
#  SEÇÃO 4: Loop principal
# ═══════════════════════════════════════════════════════════════════════════════

async def stream_audio(loop) -> None:
    """Captura → envia ao servidor → escuta o retorno. Reconecta se cair."""
    while True:    # loop externo: reinicia se microfone falhar
        pa = pyaudio.PyAudio()
        stream = None
        try:
            stream = _open_audio_stream(pa)
        except OSError as e:
            log.error("Falha ao abrir microfone: %s", e)
            _list_audio_devices()
            pa.terminate()
            await asyncio.sleep(RECONNECT_DELAY)
            continue

        log.info("Conectando ao servidor %s:%d...", PC_SERVER_IP, TCP_PORT)

        while True:    # loop interno: reconecta se a rede cair
            writer = None
            listen_task = None
            try:
                reader, writer = await asyncio.open_connection(PC_SERVER_IP, TCP_PORT)
                log.info("✓ Conectado ao PC em %s:%d", PC_SERVER_IP, TCP_PORT)
                log.info("Streaming: 48kHz → ÷3 → 16kHz | Playback: Anker card %d",
                         ANKER_CARD_INDEX)

                listen_task = asyncio.create_task(_listen_server_commands(reader))

                chunks = 0
                while True:
                    raw_48k = await _read_audio_chunk(loop, stream)
                    raw_16k = downsample(raw_48k)
                    writer.write(raw_16k)
                    chunks += 1
                    if chunks % 100 == 0:
                        await writer.drain()

                    # Se o listener morreu, a conexão não presta mais.
                    if listen_task.done():
                        raise ConnectionResetError("canal reverso encerrado")

            except (ConnectionRefusedError, TimeoutError):
                log.warning("Servidor offline. Tentando em %.0fs...", RECONNECT_DELAY)
            except (ConnectionResetError, BrokenPipeError, EOFError):
                log.warning("Conexão perdida. Reconectando em %.0fs...", RECONNECT_DELAY)
            except OSError as e:
                log.error("Erro de I/O: %s", e)
            finally:
                if listen_task and not listen_task.done():
                    listen_task.cancel()
                    try:
                        await listen_task
                    except asyncio.CancelledError:
                        pass
                if writer and not writer.is_closing():
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass

            await asyncio.sleep(RECONNECT_DELAY)

        if stream:
            stream.stop_stream()
            stream.close()
        pa.terminate()


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    if not os.path.exists(BEEP_FILE):
        log.info("Gerando beep.wav...")
        generate_beep(BEEP_FILE)

    log.info("=" * 60)
    log.info("  OUVIDO — Cliente de Áudio TVBot + Portal Cafeicultura")
    log.info("  Servidor      : %s:%d", PC_SERVER_IP, TCP_PORT)
    log.info("  Microfone idx : %d (PyAudio)", MIC_DEVICE_INDEX)
    log.info("  Anker card    : %d (ALSA playback)", ANKER_CARD_INDEX)
    log.info("  Captura       : %dHz → %dHz (÷%d)",
             CAPTURE_RATE, TARGET_RATE, DOWNSAMPLE_RATIO)
    log.info("  Beep          : %s", BEEP_FILE)
    log.info("=" * 60)

    loop = asyncio.get_running_loop()
    try:
        await stream_audio(loop)
    except KeyboardInterrupt:
        log.info("Interrompido pelo usuário.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
