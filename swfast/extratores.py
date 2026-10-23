"""O extrator da API v5 da Swfast.

Uma classe só. Os dois endpoints em uso — `/common/vendas` e
`/common/vendas/cancelamentos` — são GET e seguem a mesma convenção
(`dataInicio`/`dataFim` na query string), então não há variação de
comportamento que justifique herança: o que muda entre as duas fontes
é o caminho e os filtros, e isso são argumentos, não subclasses.

Se um dia entrar um endpoint com outra convenção (os `/aggregates` da
v5, por exemplo, que são POST com o período no corpo), a hora de
extrair uma classe base é essa — com dois casos reais na mão, não
antes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests

from .cliente import ClienteApi, ErroDeApi
from .periodo import Periodo

logger = logging.getLogger(__name__)


@dataclass
class ResultadoExtracao:
    """O que uma extração produziu — dados e metadados da execução."""

    fonte: str
    caminho: str
    periodo: Periodo
    registros: list[dict[str, Any]] = field(default_factory=list)
    duracao_segundos: float = 0.0
    executado_em: datetime = field(default_factory=datetime.now)
    erro: str | None = None

    @property
    def sucesso(self) -> bool:
        return self.erro is None

    @property
    def quantidade(self) -> int:
        return len(self.registros)

    def resumo(self) -> dict[str, Any]:
        """Linha do manifesto de execução — sem os dados, só os números."""
        return {
            "fonte": self.fonte,
            "caminho": self.caminho,
            "periodo_inicio": self.periodo.inicio_iso,
            "periodo_fim": self.periodo.fim_iso,
            "registros": self.quantidade,
            "duracao_segundos": round(self.duracao_segundos, 2),
            "executado_em": self.executado_em.isoformat(timespec="seconds"),
            "sucesso": self.sucesso,
            "erro": self.erro,
        }


class Extrator:
    """Coleta uma fonte da API v5 em um período.

    Os filtros de loja só valem para lojas centrais e só em
    `/common/vendas`; o endpoint de cancelamentos é construído sem eles.
    """

    def __init__(
        self,
        nome: str,
        caminho: str,
        cliente: ClienteApi,
        ativo: bool = True,
        lista_de_lojas: str | None = None,
        usar_loja_id_externo: bool | None = None,
    ) -> None:
        self.nome = nome
        self.caminho = caminho
        self.cliente = cliente
        #: Quando False, o pipeline ignora a fonte sem removê-la do catálogo.
        self.ativo = ativo
        self.lista_de_lojas = lista_de_lojas
        self.usar_loja_id_externo = usar_loja_id_externo

    def montar_parametros(self, periodo: Periodo) -> dict[str, Any]:
        """Traduz o período para a query string que a v5 espera."""
        parametros: dict[str, Any] = {
            "dataInicio": periodo.inicio_iso,
            "dataFim": periodo.fim_iso,
        }
        if self.lista_de_lojas:
            parametros["listaDeLojas"] = self.lista_de_lojas
            # Só faz sentido declarar o tipo do código quando há lista de lojas.
            if self.usar_loja_id_externo is not None:
                parametros["usarLojaIdExterno"] = str(self.usar_loja_id_externo).lower()
        return parametros

    def normalizar(self, carga: Any) -> list[dict[str, Any]]:
        """Garante que o retorno seja sempre uma lista de registros.

        O endpoint devolve uma lista de vendas, um objeto único quando há
        só uma, ou `null` quando o período não teve movimento. Quem
        consome o resultado não deveria precisar saber disso.
        """
        if carga is None:
            return []
        if isinstance(carga, list):
            registros = [item for item in carga if isinstance(item, dict)]
            descartados = len(carga) - len(registros)
            if descartados:
                # Sinal de que o formato da API mudou: melhor saber pelo log
                # do que por uma contagem que caiu sem explicação.
                logger.warning(
                    "[%s] %d itens da lista não são objetos e foram descartados.",
                    self.nome, descartados,
                )
            return registros
        if isinstance(carga, dict):
            return [carga]
        logger.warning("[%s] retorno em formato inesperado: %s", self.nome, type(carga))
        return []

    def extrair(self, periodo: Periodo) -> ResultadoExtracao:
        """Executa a chamada e devolve o resultado, com erro capturado.

        Um erro aqui não derruba o ciclo: ele vira um resultado sem sucesso,
        registrado no manifesto. Uma fonte instável não pode impedir que a
        outra seja coletada.
        """
        parametros = self.montar_parametros(periodo)
        inicio = time.monotonic()

        logger.info("[%s] extraindo %s", self.nome, periodo)
        try:
            carga = self.cliente.get(self.caminho, parametros)
        except (ErroDeApi, requests.RequestException) as erro:
            # O cliente já traduz tudo para ErroDeApi; o RequestException é
            # rede de segurança para a regra "uma fonte não derruba o ciclo".
            logger.error("[%s] %s", self.nome, erro)
            return ResultadoExtracao(
                fonte=self.nome,
                caminho=self.caminho,
                periodo=periodo,
                duracao_segundos=time.monotonic() - inicio,
                erro=str(erro),
            )

        registros = self.normalizar(carga)
        duracao = time.monotonic() - inicio
        logger.info("[%s] %d registros em %.1fs", self.nome, len(registros), duracao)

        return ResultadoExtracao(
            fonte=self.nome,
            caminho=self.caminho,
            periodo=periodo,
            registros=registros,
            duracao_segundos=duracao,
        )
