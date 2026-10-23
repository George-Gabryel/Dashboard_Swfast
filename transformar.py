"""Ponto de entrada da transformação (Etapa 2).

Lê os JSONs de data/raw/ e grava as tabelas tipadas em data/staging/.
Não chama a API e não precisa da API-Key.

Uso:
    python transformar.py                     # o JSON mais recente de cada fonte
    python transformar.py --todos             # todos os JSONs de data/raw/
    python transformar.py --arquivo data/raw/vendas/dt=2026-10-23/vendas_20261017_20261023.json

Códigos de saída:
    0    todos os arquivos transformados, sem alertas
    1    algum arquivo falhou, ou houve alerta de dados (ver o log)
    2    erro de uso ou nenhum arquivo para transformar
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from swfast.config import diretorio_dados_do_ambiente
from swfast.transformacao import (
    EtapaTransformacao,
    ErroDeTransformacao,
    arquivos_mais_recentes,
    todos_os_arquivos,
)

SAIDA_OK = 0
SAIDA_FALHA = 1
SAIDA_USO = 2


def configurar_log(diretorio: Path) -> None:
    diretorio.mkdir(parents=True, exist_ok=True)
    arquivo = diretorio / f"transformacao_{datetime.now():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(arquivo, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def montar_argumentos(argv: list[str] | None = None) -> argparse.Namespace:
    analisador = argparse.ArgumentParser(description="Transformação dos JSONs da Swfast.")
    grupo = analisador.add_mutually_exclusive_group()
    grupo.add_argument("--arquivo", action="append", type=Path, help="JSON específico (pode repetir).")
    grupo.add_argument("--todos", action="store_true", help="Reprocessa todos os JSONs de data/raw/.")
    return analisador.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argumentos = montar_argumentos(argv)

    diretorio_dados = diretorio_dados_do_ambiente()
    configurar_log(diretorio_dados / "_logs")
    logger = logging.getLogger("transformar")

    diretorio_raw = diretorio_dados / "raw"
    if argumentos.arquivo:
        arquivos = argumentos.arquivo
    elif argumentos.todos:
        arquivos = todos_os_arquivos(diretorio_raw)
    else:
        arquivos = arquivos_mais_recentes(diretorio_raw)

    if not arquivos:
        logger.error("Nenhum JSON encontrado em %s. Rode a extração primeiro.", diretorio_raw)
        return SAIDA_USO

    etapa = EtapaTransformacao(diretorio_dados / "staging")
    falhas = 0
    alertas = 0
    for caminho in arquivos:
        try:
            resultado = etapa.executar(caminho)
        except ErroDeTransformacao as erro:
            logger.error("%s", erro)
            falhas += 1
            continue
        alertas += sum(1 for relatorio in resultado.relatorios if relatorio.tem_alertas)

    logger.info(
        "Transformação concluída — %d/%d arquivos, %d tabelas com alerta.",
        len(arquivos) - falhas, len(arquivos), alertas,
    )
    return SAIDA_FALHA if falhas or alertas else SAIDA_OK


if __name__ == "__main__":
    raise SystemExit(main())
