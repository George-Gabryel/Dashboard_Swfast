"""Ponto de entrada do pipeline: extração (E) e transformação (T) num comando só.

Uso:
    python main.py                                  # últimos 7 dias, extrai e transforma
    python main.py --inicio 2026-10-01 --fim 2026-10-07
    python main.py --historico 2026-01-01 2026-10-21
    python main.py --fontes vendas                  # só uma fonte

    python main.py --so-transformar                 # o JSON mais recente de cada fonte, sem API
    python main.py --so-transformar --todos         # todos os JSONs de data/raw/
    python main.py --so-transformar --arquivo data/raw/vendas/dt=2026-10-23/vendas_20261017_20261023.json

Depois da extração, os JSONs gravados no ciclo são transformados em tabelas
Parquet em data/staging/. Com --so-transformar a API não é chamada e a
API-Key não é exigida: serve para reprocessar o que já está em data/raw/.

Códigos de saída (é por eles que o agendador sabe se a execução falhou):
    0    tudo coletado e transformado, sem alertas
    1    alguma fonte falhou, algum arquivo não transformou, houve alerta
         de dados (ver o log) ou erro inesperado
    2    erro de configuração ou de uso, ou nenhum JSON para transformar
    130  interrompido com Ctrl+C
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from swfast import Configuracao, ConfiguracaoInvalida, ManifestoExecucao, Periodo, PeriodoInvalido
from swfast.config import diretorio_dados_do_ambiente
from swfast.periodo import data_de_texto
from swfast.pipeline import PipelineExtracao
from swfast.transformacao import (
    EtapaTransformacao,
    ErroDeTransformacao,
    arquivos_mais_recentes,
    todos_os_arquivos,
)

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
    analisador = argparse.ArgumentParser(description="Extração e transformação dos dados das APIs Swfast.")
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
    analisador.add_argument(
        "--so-transformar",
        action="store_true",
        help="Não chama a API: só transforma JSONs que já estão em data/raw/.",
    )
    analisador.add_argument("--todos", action="store_true", help="Com --so-transformar: todos os JSONs de data/raw/.")
    analisador.add_argument(
        "--arquivo", action="append", type=Path, help="Com --so-transformar: JSON específico (pode repetir)."
    )
    argumentos = analisador.parse_args()

    # Combinações inválidas param aqui, antes de ler o .env. O error() do
    # argparse mostra o uso e sai com código 2.
    if argumentos.historico and (argumentos.inicio or argumentos.fim):
        analisador.error("--historico não pode ser combinado com --inicio/--fim.")
    if bool(argumentos.inicio) != bool(argumentos.fim):
        analisador.error("Informe --inicio e --fim juntos.")
    if (argumentos.todos or argumentos.arquivo) and not argumentos.so_transformar:
        analisador.error("--todos e --arquivo só valem junto com --so-transformar.")
    if argumentos.todos and argumentos.arquivo:
        analisador.error("Use --todos ou --arquivo, não os dois.")
    if argumentos.so_transformar and (
        argumentos.inicio or argumentos.historico or argumentos.fontes
    ):
        analisador.error("--so-transformar não chama a API: não combine com --inicio/--fim/--historico/--fontes.")
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


def transformar_arquivos(arquivos: list[Path], diretorio_staging: Path, logger: logging.Logger) -> int:
    """Transforma cada JSON bruto e devolve quantos problemas houve.

    Problema = arquivo que não transformou ou tabela com alerta de dados.
    Um arquivo com erro não impede os outros, e o JSON bruto continua em
    data/raw/ para reprocessar com --so-transformar.
    """
    etapa = EtapaTransformacao(diretorio_staging)
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
    return falhas + alertas


def so_transformar(argumentos: argparse.Namespace) -> int:
    """Modo --so-transformar: lê data/raw/, grava data/staging/, sem API."""
    diretorio_dados = diretorio_dados_do_ambiente()
    configurar_log(diretorio_dados / "_logs")
    logger = logging.getLogger("main")

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

    problemas = transformar_arquivos(arquivos, diretorio_dados / "staging", logger)
    return SAIDA_FALHA if problemas else SAIDA_OK


def main() -> int:
    argumentos = montar_argumentos()

    if argumentos.so_transformar:
        return so_transformar(argumentos)

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

        logger.info(resumir(manifestos))

        # Só o que foi gravado neste ciclo; fonte com falha ou sem registros
        # não tem arquivo e fica de fora.
        gravados = [
            Path(linha["arquivo"])
            for manifesto in manifestos
            for linha in manifesto.linhas
            if linha["arquivo"]
        ]
        problemas = 0
        if gravados:
            problemas = transformar_arquivos(gravados, configuracao.diretorio_raw.parent / "staging", logger)
        else:
            logger.info("Nenhum JSON novo neste ciclo; nada a transformar.")
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

    # O extrair() transforma falhas em resultados, sem exceção: é aqui que
    # elas viram código de saída, para o agendador não marcar sucesso.
    if problemas or any(manifesto.falhas for manifesto in manifestos):
        return SAIDA_FALHA
    return SAIDA_OK


if __name__ == "__main__":
    raise SystemExit(main())
