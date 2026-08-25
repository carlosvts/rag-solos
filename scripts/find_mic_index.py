#!/usr/bin/env python3
"""Localiza o índice PyAudio do dispositivo de captura Anker.

Testa abrindo um stream real (1 e depois 2 canais) em vez de confiar
apenas em maxInputChannels, que pode reportar 0 de forma espúria logo
após o boot (timing de inicialização do próprio chip USB do Anker).

Imprime APENAS o índice (int) em stdout. Erros vão para stderr.
"""
import sys

import pyaudio

TARGET_KEYWORDS = ["anker"]
CHANNELS_TO_TRY = (1, 2)
SAMPLE_RATE = 16000


def _try_open(pa, index, channels):
    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=index,
            frames_per_buffer=1024,
        )
        stream.read(1024, exception_on_overflow=False)
        stream.close()
        return True
    except Exception:
        return False


def find_index():
    pa = pyaudio.PyAudio()
    try:
        candidates = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            name = info.get("name", "").lower()
            if any(k in name for k in TARGET_KEYWORDS):
                candidates.append(i)

        for idx in candidates:
            for channels in CHANNELS_TO_TRY:
                if _try_open(pa, idx, channels):
                    print(f"Device {idx} OK com {channels} canal(is).", file=sys.stderr)
                    return idx
        return None
    finally:
        pa.terminate()


if __name__ == "__main__":
    idx = find_index()
    if idx is None:
        print("Nenhum dispositivo Anker abriu com sucesso (1 ou 2 canais).", file=sys.stderr)
        sys.exit(1)
    print(idx)
