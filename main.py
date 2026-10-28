"""Ponto de entrada do pipeline: extração (E), transformação (T) e carga (L).

Uso:
    python main.py                                  # últimos 7 dias
    python main.py --inicio 2026-10-01 --fim 2026-10-07
    python main.py --historico 2026-01-01 2026-10-21
    python main.py --fontes vendas                  # só uma fonte
    python main.py --continuo                       # o modo do container: roda para sempre

Cada fonte vai da API direto para o PostgreSQL, sem arquivo intermediário.
O que aconteceu em cada etapa fica no manifesto: data/_manifesto/*.json.

Modo --continuo: roda um ciclo ao iniciar e depois nos horários fixos a cada
INTERVALO_HORAS (padrão 3: 00h, 03h, 06h... 21h). Antes de cada ciclo espera
o banco ficar disponível — quando a máquina liga, banco e aplicação sobem
juntos. Um ciclo com falha não interrompe o agendamento.

Códigos de saída (é por eles que o agendador sabe se a execução falhou):
    0    tudo coletado, transformado e gravado no banco, sem alertas
    1    alguma fonte falhou (em qualquer etapa), houve alerta de dados
         (ver o log), banco inacessível ou erro inesperado
    2    erro de configuração ou de uso
    130  interrompido com Ctrl+C ou docker stop
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from swfast import Configuracao, ConfiguracaoInvalida, ManifestoExecucao, Periodo, PeriodoInvalido
from swfast.banco import Banco, ErroDeBanco
from swfast.periodo import data_de_texto
from swfast.pipeline import PipelineExtracao

SAIDA_OK = 0
SAIDA_FALHA = 1
SAIDA_USO = 2
SAIDA_INTERROMPIDO = 130

# Espera pelo banco antes de cada ciclo do modo contínuo: 30 x 10s = 5 min.
TENTATIVAS_BANCO = 30
ESPERA_BANCO_SEGUNDOS = 10
# A espera até o próximo ciclo é feita em pedaços: se a máquina hibernar,
# o ciclo atrasado roda logo ao acordar, em vez de dormir o que faltava.
PEDACO_DE_ESPERA_SEGUNDOS = 60

FORMATO_LOG = logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)


def configurar_log(diretorio: Path) -> None:
    """Loga na tela e em arquivo, um arquivo por dia.

    O arquivo é o que sobra quando roda via agendador. Pode ser chamada de
    novo: se o dia mudou, troca de arquivo — no modo contínuo o processo
    vive semanas e continua gerando um pipeline_AAAAMMDD.log por dia.
    """
    diretorio.mkdir(parents=True, exist_ok=True)
    arquivo = (diretorio / f"pipeline_{datetime.now():%Y%m%d}.log").resolve()
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)

    nossos = [handler for handler in raiz.handlers if getattr(handler, "do_pipeline", False)]
    if any(Path(getattr(handler, "baseFilename", "")) == arquivo for handler in nossos):
        return
    for handler in nossos:
        raiz.removeHandler(handler)
        handler.close()
    for handler in (logging.FileHandler(arquivo, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        handler.setFormatter(FORMATO_LOG)
        handler.do_pipeline = True
        raiz.addHandler(handler)


def montar_argumentos() -> argparse.Namespace:
    analisador = argparse.ArgumentParser(description="ETL das APIs Swfast: extração, transformação e carga no PostgreSQL.")
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
        "--continuo",
        action="store_true",
        help="Roda para sempre: um ciclo agora e depois a cada INTERVALO_HORAS.",
    )
    argumentos = analisador.parse_args()

    # Combinações inválidas param aqui, antes de ler o .env. O error() do
    # argparse mostra o uso e sai com código 2.
    if argumentos.historico and (argumentos.inicio or argumentos.fim):
        analisador.error("--historico não pode ser combinado com --inicio/--fim.")
    if bool(argumentos.inicio) != bool(argumentos.fim):
        analisador.error("Informe --inicio e --fim juntos.")
    if argumentos.continuo and (argumentos.inicio or argumentos.historico):
        analisador.error("--continuo usa sempre a janela padrão: não combine com --inicio/--fim/--historico.")
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
        f"{coletas - falhas}/{coletas} fontes processadas com sucesso, {registros} registros."
    )


def executar_ciclo(configuracao: Configuracao, argumentos: argparse.Namespace, logger: logging.Logger) -> int:
    """Um ciclo de ETL, do jeito que os argumentos pedem. Devolve o código de saída."""
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
    except ErroDeBanco as erro:
        logger.error("%s", erro)
        return SAIDA_FALHA
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
    # Falhas de cada etapa viram resultados no manifesto, sem exceção: é
    # aqui que elas viram código de saída, para o agendador não marcar sucesso.
    falhou = any(manifesto.falhas for manifesto in manifestos)
    alertas = sum(manifesto.tabelas_com_alerta for manifesto in manifestos)
    if falhou or alertas:
        return SAIDA_FALHA
    return SAIDA_OK


# --- modo contínuo ----------------------------------------------------------------

def proxima_execucao(agora: datetime, horas: int) -> datetime:
    """O próximo horário múltiplo de `horas` contado de 00:00, estritamente depois de `agora`.

    Com 3h: 10:15 -> 12:00, 12:00 -> 15:00, 23:30 -> 00:00 do dia seguinte.
    """
    meia_noite = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    return meia_noite + timedelta(hours=horas * (agora.hour // horas + 1))


def aguardar_banco(
    configuracao: Configuracao,
    logger: logging.Logger,
    tentativas: int = TENTATIVAS_BANCO,
    espera: float = ESPERA_BANCO_SEGUNDOS,
    esperar: Callable[[float], None] = time.sleep,
) -> bool:
    """Tenta conectar até o banco responder. False se não respondeu a tempo."""
    for tentativa in range(1, tentativas + 1):
        banco = Banco(configuracao.conninfo, configuracao.endereco_banco)
        try:
            banco.conectar()
        except ErroDeBanco as erro:
            logger.warning("Banco ainda indisponível (tentativa %d/%d): %s", tentativa, tentativas, erro)
            if tentativa < tentativas:
                esperar(espera)
            continue
        banco.fechar()
        return True
    return False


def aguardar_ate(
    momento: datetime,
    agora: Callable[[], datetime] = datetime.now,
    esperar: Callable[[float], None] = time.sleep,
) -> None:
    """Dorme até `momento`, em pedaços, olhando o relógio a cada pedaço."""
    while (falta := (momento - agora()).total_seconds()) > 0:
        esperar(min(falta, PEDACO_DE_ESPERA_SEGUNDOS))


def executar_continuamente(
    configuracao: Configuracao,
    argumentos: argparse.Namespace,
    logger: logging.Logger,
    *,
    ciclo: Callable[..., int] = executar_ciclo,
    esperar_banco: Callable[..., bool] = aguardar_banco,
    esperar_ate: Callable[[datetime], None] = aguardar_ate,
    agora: Callable[[], datetime] = datetime.now,
) -> int:
    """Um ciclo agora e depois um a cada `intervalo_horas`, até ser interrompido."""
    logger.info(
        "Modo contínuo — um ciclo agora e depois a cada %dh, em horários fixos.",
        configuracao.intervalo_horas,
    )
    try:
        while True:
            configurar_log(configuracao.diretorio_logs)  # troca de arquivo se o dia mudou
            if esperar_banco(configuracao, logger):
                codigo = ciclo(configuracao, argumentos, logger)
                logger.info("Ciclo encerrado com código %d.", codigo)
                if codigo in (SAIDA_USO, SAIDA_INTERROMPIDO):
                    # Erro de uso se repetiria em todo ciclo; interrupção é para parar.
                    return codigo
            else:
                logger.error("Banco indisponível em %s; ciclo pulado.", configuracao.endereco_banco)

            proxima = proxima_execucao(agora(), configuracao.intervalo_horas)
            logger.info("Próxima execução às %s.", f"{proxima:%d/%m %H:%M}")
            esperar_ate(proxima)
    except KeyboardInterrupt:
        logger.warning("Modo contínuo encerrado.")
        return SAIDA_INTERROMPIDO


def _interromper(*_) -> None:
    """`docker stop` manda SIGTERM: vira a mesma saída limpa do Ctrl+C."""
    raise KeyboardInterrupt


def main() -> int:
    argumentos = montar_argumentos()

    try:
        configuracao = Configuracao.a_partir_do_ambiente()
    except ConfiguracaoInvalida as erro:
        # Ainda não há arquivo de log: a pasta dele vem justamente da configuração.
        print(f"Erro de configuração: {erro}", file=sys.stderr)
        return SAIDA_USO

    configurar_log(configuracao.diretorio_logs)
    logger = logging.getLogger("main")

    if argumentos.continuo:
        signal.signal(signal.SIGTERM, _interromper)
        return executar_continuamente(configuracao, argumentos, logger)
    return executar_ciclo(configuracao, argumentos, logger)


if __name__ == "__main__":
    raise SystemExit(main())
