"""Ponto de entrada do pipeline.

Uso:
    python main.py                                  # últimos 7 dias
    python main.py --inicio 2026-10-01 --fim 2026-10-07
    python main.py --historico 2026-01-01 2026-10-21
    python main.py --fontes vendas                  # só uma fonte
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from swfast import Configuracao, ConfiguracaoInvalida, Periodo, PeriodoInvalido
from swfast.pipeline import PipelineExtracao


def configurar_log(diretorio: Path) -> None:
    """Loga na tela e em arquivo. O arquivo é o que sobra quando roda via agendador."""
    diretorio.mkdir(parents=True, exist_ok=True)
    arquivo = diretorio / f"pipeline_{datetime.now():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(arquivo, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def montar_argumentos() -> argparse.Namespace:
    analisador = argparse.ArgumentParser(description="Extração de dados das APIs Swfast.")
    analisador.add_argument("--inicio", help="Data inicial YYYY-MM-DD.")
    analisador.add_argument("--fim", help="Data final YYYY-MM-DD.")
    analisador.add_argument(
        "--historico",
        nargs=2,
        metavar=("INICIO", "FIM"),
        help="Carga histórica: fatia o intervalo em janelas de até 7 dias.",
    )
    analisador.add_argument(
        "--fontes",
        help="Lista de fontes separadas por vírgula. Sem isso, roda todas as ativas.",
    )
    return analisador.parse_args()


def main() -> int:
    argumentos = montar_argumentos()

    try:
        configuracao = Configuracao.a_partir_do_ambiente()
    except ConfiguracaoInvalida as erro:
        print(f"Erro de configuração: {erro}", file=sys.stderr)
        return 2

    configurar_log(configuracao.diretorio_manifesto.parent / "_logs")
    logger = logging.getLogger("main")

    pipeline = PipelineExtracao(configuracao)

    if argumentos.fontes:
        pedidas = {nome.strip() for nome in argumentos.fontes.split(",") if nome.strip()}
        for extrator in pipeline.extratores:
            extrator.ativo = extrator.nome in pedidas
        desconhecidas = pedidas - {extrator.nome for extrator in pipeline.extratores}
        if desconhecidas:
            logger.warning("Fontes não encontradas no catálogo: %s", ", ".join(desconhecidas))

    try:
        if argumentos.historico:
            inicio = datetime.strptime(argumentos.historico[0], "%Y-%m-%d").date()
            fim = datetime.strptime(argumentos.historico[1], "%Y-%m-%d").date()
            pipeline.executar_historico(inicio, fim)
        else:
            periodo = None
            if argumentos.inicio and argumentos.fim:
                periodo = Periodo.de_texto(argumentos.inicio, argumentos.fim)
            elif argumentos.inicio or argumentos.fim:
                print("Informe --inicio e --fim juntos.", file=sys.stderr)
                return 2
            manifesto = pipeline.executar(periodo)
            logger.info(manifesto.resumo_em_texto())
    except (PeriodoInvalido, ValueError) as erro:
        print(f"Erro: {erro}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        logger.warning("Execução interrompida pelo usuário.")
        return 130
    finally:
        pipeline.encerrar()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
