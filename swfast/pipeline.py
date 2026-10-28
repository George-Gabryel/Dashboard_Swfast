"""O orquestrador do ciclo: extração, transformação e carga.

Percorre o catálogo em ordem, uma fonte de cada vez, com pausa entre as
chamadas. Nada roda em paralelo: a máquina do cliente também serve o
restaurante, e o pipeline não pode competir com ela.

Para cada fonte: API -> transformação em memória -> Postgres. Uma falha
em qualquer etapa vira erro no manifesto e não impede a fonte seguinte.
"""

from __future__ import annotations

import logging
import time
from datetime import date

import psycopg

from .banco import Banco
from .catalogo import construir_catalogo
from .config import Configuracao
from .extratores import Extrator
from .gravador import ManifestoExecucao
from .periodo import Periodo
from .transformacao import Transformador, registrar_no_log

logger = logging.getLogger(__name__)


class PipelineExtracao:
    """Executa um ciclo completo de ETL."""

    def __init__(
        self,
        configuracao: Configuracao,
        extratores: list[Extrator] | None = None,
        banco: Banco | None = None,
    ) -> None:
        self.configuracao = configuracao
        self.extratores = extratores if extratores is not None else construir_catalogo(configuracao)
        self.banco = banco or Banco(configuracao.conninfo, configuracao.endereco_banco)
        self._tabelas_prontas = False

    @property
    def fontes_ativas(self) -> list[Extrator]:
        return [extrator for extrator in self.extratores if extrator.ativo]

    def _preparar_banco(self) -> None:
        """Conecta e cria as tabelas uma vez por execução (não por janela)."""
        self.banco.conectar()
        if not self._tabelas_prontas:
            self.banco.criar_tabelas()
            self._tabelas_prontas = True

    def executar(self, periodo: Periodo | None = None) -> ManifestoExecucao:
        """Roda um ciclo sobre um período. Sem período, usa a janela padrão."""
        self.configuracao.preparar_diretorios()
        self._preparar_banco()
        janela = periodo or Periodo.ultimos_dias(self.configuracao.dias_por_janela)
        manifesto = ManifestoExecucao(self.configuracao.diretorio_manifesto, janela)

        ativos = self.fontes_ativas
        logger.info("Ciclo iniciado — %d fontes, período %s", len(ativos), janela)

        for indice, extrator in enumerate(ativos, start=1):
            logger.info("(%d/%d) %s", indice, len(ativos), extrator.nome)
            self._processar_fonte(extrator, janela, manifesto)

            # Pausa entre fontes: respira a API e libera a CPU da máquina.
            if indice < len(ativos) and self.configuracao.intervalo_entre_chamadas > 0:
                time.sleep(self.configuracao.intervalo_entre_chamadas)

        manifesto.gravar()
        logger.info("Ciclo concluído — %s", manifesto.resumo_em_texto())
        return manifesto

    def _processar_fonte(self, extrator: Extrator, janela: Periodo, manifesto: ManifestoExecucao) -> None:
        resultado = extrator.extrair(janela)
        if not resultado.sucesso:
            manifesto.registrar(resultado)
            return

        inicio = time.monotonic()
        try:
            tabelas, relatorios = Transformador.para_fonte(extrator.nome).transformar(resultado.registros)
        except Exception as erro:
            logger.exception("[%s] falha na transformação", extrator.nome)
            manifesto.registrar(
                resultado, duracao_transformacao=time.monotonic() - inicio, erro=f"transformacao: {erro}"
            )
            return
        duracao_transformacao = time.monotonic() - inicio
        for relatorio in relatorios:
            registrar_no_log(relatorio)

        inicio = time.monotonic()
        try:
            carregadas = self.banco.carregar(extrator.nome, tabelas)
        except psycopg.Error as erro:
            # A transação da fonte já foi desfeita: o banco fica como estava.
            logger.error("[%s] falha na carga, nada gravado: %s", extrator.nome, erro)
            manifesto.registrar(
                resultado,
                relatorios=relatorios,
                duracao_transformacao=duracao_transformacao,
                duracao_carga=time.monotonic() - inicio,
                erro=f"carga: {erro}",
            )
            return

        logger.info(
            "[%s] carregado — %s",
            extrator.nome, ", ".join(f"{tabela}={linhas}" for tabela, linhas in carregadas.items()),
        )
        manifesto.registrar(
            resultado,
            relatorios=relatorios,
            carregadas=carregadas,
            duracao_transformacao=duracao_transformacao,
            duracao_carga=time.monotonic() - inicio,
        )

    def executar_historico(self, inicio: date, fim: date) -> list[ManifestoExecucao]:
        """Carga inicial: quebra um intervalo longo em janelas e roda cada uma.

        Usado uma vez, para popular o histórico. Depois o ciclo de 3h
        mantém os dados em dia sozinho.
        """
        janelas = Periodo.fatiar(inicio, fim, self.configuracao.dias_por_janela)
        logger.info("Carga histórica: %d janelas de até %d dias",
                    len(janelas), self.configuracao.dias_por_janela)

        manifestos = []
        for indice, janela in enumerate(janelas, start=1):
            logger.info("Janela %d/%d — %s", indice, len(janelas), janela)
            manifestos.append(self.executar(janela))
        return manifestos

    def encerrar(self) -> None:
        """Fecha as sessões HTTP dos clientes e a conexão com o banco."""
        for cliente in {extrator.cliente for extrator in self.extratores}:
            cliente.fechar()
        self.banco.fechar()
