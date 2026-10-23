"""Ponto de entrada do pipeline.

Uso:
    python main.py                                  # últimos 7 dias
    python main.py --inicio 2026-10-01 --fim 2026-10-07
    python main.py --historico 2026-01-01 2026-10-21
    python main.py --fontes vendas                  # só uma fonte

Códigos de saída (é por eles que o agendador sabe se a execução falhou):
    0    todas as fontes coletadas
    1    alguma fonte falhou, ou erro inesperado
    2    erro de configuração ou de uso
    130  interrompido com Ctrl+C
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from swfast import Configuracao, ConfiguracaoInvalida, ManifestoExecucao, Periodo, PeriodoInvalido
from swfast.periodo import data_de_texto
from swfast.pipeline import PipelineExtracao

SAIDA_OK = 0
SAIDA_FALHA = 1
SAIDA_USO = 2
SAIDA_INTERROMPIDO = 130


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
    argumentos = analisador.parse_args()

    # Combinações inválidas param aqui, antes de ler o .env. O error() do
    # argparse mostra o uso e sai com código 2.
    if argumentos.historico and (argumentos.inicio or argumentos.fim):
        analisador.error("--historico não pode ser combinado com --inicio/--fim.")
    if bool(argumentos.inicio) != bool(argumentos.fim):
        analisador.error("Informe --inicio e --fim juntos.")
    return argumentos


def resumir(manifestos: list[ManifestoExecucao]) -> str:
    """Resumo final: o do ciclo, ou o total quando foram várias janelas."""
    if len(manifestos) == 1:
        return manifestos[0].resumo_em_texto()
    coletas = sum(len(manifesto.linhas) for manifesto in manifestos)
    falhas = sum(len(manifesto.falhas) for manifesto in manifestos)
    registros = sum(manifesto.total_registros for manifesto in manifestos)
    return (
        f"Carga histórica: {len(manifestos)} janelas, "
        f"{coletas - falhas}/{coletas} coletas com sucesso, {registros} registros."
    )


def main() -> int:
    argumentos = montar_argumentos()

    try:
        configuracao = Configuracao.a_partir_do_ambiente()
    except ConfiguracaoInvalida as erro:
        # Ainda não há arquivo de log: a pasta dele vem justamente da configuração.
        print(f"Erro de configuração: {erro}", file=sys.stderr)
        return SAIDA_USO

    configurar_log(configuracao.diretorio_manifesto.parent / "_logs")
    logger = logging.getLogger("main")

    pipeline = PipelineExtracao(configuracao)
    try:
        if argumentos.fontes:
            pedidas = {nome.strip() for nome in argumentos.fontes.split(",") if nome.strip()}
            for extrator in pipeline.extratores:
                extrator.ativo = extrator.nome in pedidas
            desconhecidas = pedidas - {extrator.nome for extrator in pipeline.extratores}
            if desconhecidas:
                logger.warning("Fontes não encontradas no catálogo: %s", ", ".join(desconhecidas))
            if not pipeline.fontes_ativas:
                logger.error("Nenhuma fonte ativa depois do filtro --fontes. Nada a executar.")
                return SAIDA_USO

        if argumentos.historico:
            inicio, fim = map(data_de_texto, argumentos.historico)
            manifestos = pipeline.executar_historico(inicio, fim)
        else:
            periodo = Periodo.de_texto(argumentos.inicio, argumentos.fim) if argumentos.inicio else None
            manifestos = [pipeline.executar(periodo)]
    except PeriodoInvalido as erro:
        logger.error("Período inválido: %s", erro)
        return SAIDA_USO
    except KeyboardInterrupt:
        logger.warning("Execução interrompida pelo usuário.")
        return SAIDA_INTERROMPIDO
    except Exception:
        # Sem isto, o traceback iria só para o stderr — que se perde quando
        # roda pelo agendador — e o arquivo de log pararia sem explicação.
        logger.exception("Erro inesperado; execução interrompida.")
        return SAIDA_FALHA
    finally:
        pipeline.encerrar()

    logger.info(resumir(manifestos))
    # O extrair() transforma falhas em resultados, sem exceção: é aqui que
    # elas viram código de saída, para o agendador não marcar sucesso.
    if any(manifesto.falhas for manifesto in manifestos):
        return SAIDA_FALHA
    return SAIDA_OK


if __name__ == "__main__":
    raise SystemExit(main())
