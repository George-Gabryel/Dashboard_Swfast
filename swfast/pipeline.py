"""O orquestrador do ciclo de extração.

Percorre o catálogo em ordem, uma fonte de cada vez, com pausa entre as
chamadas. Nada roda em paralelo: a máquina do cliente também serve o
restaurante, e o pipeline não pode competir com ela.
"""

from __future__ import annotations

import logging
import time
from datetime import date

from .catalogo import construir_catalogo
from .config import Configuracao
from .extratores import Extrator
from .gravador import GravadorBruto, ManifestoExecucao
from .periodo import Periodo

logger = logging.getLogger(__name__)


class PipelineExtracao:
    """Executa um ciclo completo de extração."""

    def __init__(self, configuracao: Configuracao, extratores: list[Extrator] | None = None) -> None:
        self.configuracao = configuracao
        self.extratores = extratores if extratores is not None else construir_catalogo(configuracao)
        self.gravador = GravadorBruto(configuracao.diretorio_raw)

    @property
    def fontes_ativas(self) -> list[Extrator]:
        return [extrator for extrator in self.extratores if extrator.ativo]

    def executar(self, periodo: Periodo | None = None) -> ManifestoExecucao:
        """Roda um ciclo sobre um período. Sem período, usa a janela padrão."""
        self.configuracao.preparar_diretorios()
        janela = periodo or Periodo.ultimos_dias(self.configuracao.dias_por_janela)
        manifesto = ManifestoExecucao(self.configuracao.diretorio_manifesto)

        ativos = self.fontes_ativas
        logger.info("Ciclo iniciado — %d fontes, período %s", len(ativos), janela)

        for indice, extrator in enumerate(ativos, start=1):
            logger.info("(%d/%d) %s", indice, len(ativos), extrator.nome)

            resultado = extrator.extrair(janela)
            arquivo = self.gravador.gravar(resultado)
            manifesto.registrar(resultado, arquivo)

            # Pausa entre fontes: respira a API e libera a CPU da máquina.
            if indice < len(ativos) and self.configuracao.intervalo_entre_chamadas > 0:
                time.sleep(self.configuracao.intervalo_entre_chamadas)

        manifesto.gravar()
        logger.info("Ciclo concluído — %s", manifesto.resumo_em_texto())
        return manifesto

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
        """Fecha as sessões HTTP dos clientes usados pelos extratores."""
        for cliente in {extrator.cliente for extrator in self.extratores}:
            cliente.fechar()
