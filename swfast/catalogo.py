"""Catálogo de fontes: a lista do que o pipeline coleta.

Este é o único arquivo que precisa mudar para incluir, remover ou desligar
uma fonte. A ordem da lista é a ordem de execução — vendas primeiro,
cancelamentos depois, porque o cancelamento só faz sentido ao lado da
venda que ele anula.
"""

from __future__ import annotations

from .cliente import ClienteApi
from .config import Configuracao
from .extratores import Extrator


def construir_catalogo(configuracao: Configuracao) -> list[Extrator]:
    """Monta os extratores já conectados ao cliente da API v5."""

    cliente = ClienteApi(
        nome="v5",
        url_base=configuracao.url_base,
        api_key=configuracao.api_key,
        timeout=configuracao.timeout_segundos,
        tentativas=configuracao.tentativas,
        espera_inicial=configuracao.espera_inicial_retentativa,
    )

    return [
        Extrator(
            "vendas",
            "/common/vendas",
            cliente,
            lista_de_lojas=configuracao.lista_de_lojas,
            usar_loja_id_externo=configuracao.usar_loja_id_externo,
        ),
        # Cancelamentos não aceita filtro de loja: só dataInicio e dataFim.
        Extrator(
            "cancelamentos",
            "/common/vendas/cancelamentos",
            cliente,
        ),
    ]
