#!/usr/bin/env python3
"""
tvbox_uploader.py

Envia arquivos CSV da TV Box para a API do PortalTCC (POST /api/upload) e
apaga o arquivo local SOMENTE após o servidor confirmar que recebeu e
processou o upload com sucesso. Assim a TV Box nunca perde um CSV por
apagar cedo demais, mas também não acumula arquivo em disco.

Uso:
    # Enviar um único arquivo
    python3 tvbox_uploader.py --file /caminho/dados.csv

    # Processar todos os CSVs de uma pasta, uma vez
    python3 tvbox_uploader.py --dir /caminho/pasta

    # Vigiar uma pasta continuamente e enviar (e apagar) cada novo CSV
    python3 tvbox_uploader.py --watch /caminho/pasta --interval 30

Requisitos:
    pip install requests
"""

import argparse
import logging
import time
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tvbox_uploader")

DEFAULT_URL = "http://localhost:8000/api/upload"
DEFAULT_DIR = Path("data")  # pasta estática de teste; troque por --watch quando for dinâmico
TIMEOUT_SECONDS = 60
TAMANHO_MAXIMO_MB = 10


def validar_arquivo(caminho: Path) -> bool:
    """Confere localmente as mesmas regras que o PortalTCC exige,
    para não gastar banda/tempo enviando algo que vai ser recusado."""
    if caminho.suffix.lower() != ".csv":
        log.warning("Ignorando %s: só arquivos .csv são aceitos.", caminho.name)
        return False

    tamanho_mb = caminho.stat().st_size / (1024 * 1024)
    if tamanho_mb > TAMANHO_MAXIMO_MB:
        log.warning(
            "Ignorando %s: %.1f MB excede o limite de %d MB do PortalTCC.",
            caminho.name, tamanho_mb, TAMANHO_MAXIMO_MB,
        )
        return False

    return True


def enviar_csv(caminho: Path, api_url: str) -> bool:
    """Envia um único CSV via POST multipart. Retorna True se o servidor
    confirmou sucesso (HTTP 200)."""
    if not caminho.is_file():
        log.warning("Arquivo não encontrado, pulando: %s", caminho)
        return False

    if not validar_arquivo(caminho):
        return False

    log.info(
        "Enviando %s (%.1f KB) para %s",
        caminho.name, caminho.stat().st_size / 1024, api_url,
    )

    try:
        with open(caminho, "rb") as f:
            # Nome do campo confirmado via erro 422 do servidor: "csvFile".
            arquivos = {"csvFile": (caminho.name, f, "text/csv")}
            resp = requests.post(api_url, files=arquivos, timeout=TIMEOUT_SECONDS)
    except requests.exceptions.RequestException as exc:
        log.error("Falha de rede ao enviar %s: %s", caminho.name, exc)
        return False

    if resp.status_code != 200:
        log.error(
            "Servidor recusou %s (HTTP %s): %s",
            caminho.name, resp.status_code, resp.text[:300],
        )
        return False

    log.info("Upload de %s confirmado pelo servidor.", caminho.name)
    return True


def enviar_e_apagar(caminho: Path, api_url: str) -> bool:
    """Envia o CSV e só apaga o arquivo local se o upload foi confirmado."""
    sucesso = enviar_csv(caminho, api_url)
    if sucesso:
        try:
            caminho.unlink()
            log.info("Arquivo local apagado (espaço liberado): %s", caminho)
        except OSError as exc:
            log.error(
                "Upload OK mas falhou ao apagar %s (%s). "
                "Apague manualmente para não desperdiçar espaço.",
                caminho, exc,
            )
    else:
        log.warning("Arquivo mantido em disco para nova tentativa: %s", caminho)
    return sucesso


def processar_pasta(pasta: Path, api_url: str) -> None:
    csvs = sorted(pasta.glob("*.csv"))
    if not csvs:
        log.info("Nenhum CSV encontrado em %s", pasta)
        return
    for csv_file in csvs:
        enviar_e_apagar(csv_file, api_url)


def modo_vigilancia(pasta: Path, api_url: str, intervalo: int) -> None:
    log.info("Vigiando %s a cada %ds. Ctrl+C para parar.", pasta, intervalo)
    try:
        while True:
            processar_pasta(pasta, api_url)
            time.sleep(intervalo)
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Envia CSVs da TV Box para o PortalTCC e apaga após confirmação de sucesso."
    )
    parser.add_argument("--file", type=Path, help="Caminho de um único CSV a enviar")
    parser.add_argument(
        "--dir", type=Path, default=DEFAULT_DIR,
        help=f"Pasta com CSVs a enviar, processa uma vez e sai (padrão: {DEFAULT_DIR})",
    )
    parser.add_argument("--watch", type=Path, help="Pasta a vigiar continuamente (ignora --dir)")
    parser.add_argument("--interval", type=int, default=30, help="Intervalo em segundos do modo --watch (padrão: 30)")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"URL da API de upload (padrão: {DEFAULT_URL})")
    args = parser.parse_args()

    if args.file:
        enviar_e_apagar(args.file, args.url)
    elif args.watch:
        args.watch.mkdir(parents=True, exist_ok=True)
        modo_vigilancia(args.watch, args.url, args.interval)
    else:
        # Padrão: pasta estática "data/" na raiz do projeto.
        # Quando precisar de algo dinâmico, troque para --watch data --interval 30.
        args.dir.mkdir(parents=True, exist_ok=True)
        processar_pasta(args.dir, args.url)


if __name__ == "__main__":
    main()