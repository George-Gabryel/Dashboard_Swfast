"""Cliente HTTP das APIs Swfast.

Uma instância por versão da API (uma para a v1, outra para a v5), porque
cada uma tem URL base e chave próprias. O cliente não sabe nada sobre
vendas, estoque ou pagamentos: ele só sabe fazer requisições autenticadas,
insistir quando a falha é temporária e desistir quando não é.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Erros que valem uma nova tentativa: instabilidade de rede ou do servidor.
STATUS_TEMPORARIOS = {429, 500, 502, 503, 504}


class ErroDeApi(RuntimeError):
    """Falha ao conversar com a API."""


class ErroDeAutenticacao(ErroDeApi):
    """401/403 — chave inválida, ausente ou sem permissão para o recurso."""


class ErroDeRequisicao(ErroDeApi):
    """400 — parâmetros malformados, normalmente data fora do formato esperado."""


class ClienteApi:
    """Faz chamadas GET autenticadas a uma das APIs Swfast."""

    def __init__(
        self,
        nome: str,
        url_base: str,
        api_key: str,
        timeout: int = 120,
        tentativas: int = 3,
        espera_inicial: float = 2.0,
    ) -> None:
        self.nome = nome
        self.url_base = url_base.rstrip("/")
        self.timeout = timeout
        self.tentativas = tentativas
        self.espera_inicial = espera_inicial

        # A sessão reaproveita a conexão TCP entre chamadas e guarda os
        # headers fixos — inclusive a API-Key, que assim nunca aparece
        # espalhada pelo código.
        self.sessao = requests.Session()
        self.sessao.headers.update(
            {
                "Accept": "application/json",
                "API-Key": api_key,
                "User-Agent": "pipeline-swfast/1.0",
            }
        )

    def get(self, caminho: str, parametros: dict[str, Any] | None = None) -> Any:
        """Executa o GET e devolve o JSON já convertido para objetos Python."""
        url = f"{self.url_base}/{caminho.lstrip('/')}"
        espera = self.espera_inicial
        ultimo_erro: Exception | None = None

        for tentativa in range(1, self.tentativas + 1):
            try:
                resposta = self.sessao.get(url, params=parametros, timeout=self.timeout)
            except requests.RequestException as erro:
                ultimo_erro = erro
                logger.warning(
                    "[%s] falha de rede em %s (tentativa %d/%d): %s",
                    self.nome, caminho, tentativa, self.tentativas, erro,
                )
            else:
                status = resposta.status_code

                if status in (401, 403):
                    # Insistir não resolve: a chave está errada ou sem acesso.
                    raise ErroDeAutenticacao(
                        f"[{self.nome}] {status} em {caminho}. "
                        "Confira a API-Key no .env e se ela tem acesso a este endpoint."
                    )
                if status == 400:
                    raise ErroDeRequisicao(
                        f"[{self.nome}] 400 em {caminho} com {parametros}. "
                        "Normalmente é formato de data ou nome de parâmetro errado."
                    )
                if status == 404:
                    raise ErroDeApi(f"[{self.nome}] 404: o caminho {caminho} não existe.")

                if status in STATUS_TEMPORARIOS:
                    ultimo_erro = ErroDeApi(f"[{self.nome}] {status} em {caminho}")
                    logger.warning(
                        "[%s] %s em %s (tentativa %d/%d)",
                        self.nome, status, caminho, tentativa, self.tentativas,
                    )
                else:
                    resposta.raise_for_status()
                    if not resposta.content:
                        return []
                    try:
                        return resposta.json()
                    except ValueError as erro:
                        raise ErroDeApi(
                            f"[{self.nome}] a resposta de {caminho} não é um JSON válido."
                        ) from erro

            if tentativa < self.tentativas:
                # Espera progressiva: 2s, 4s, 8s... dá tempo de a API se recuperar.
                time.sleep(espera)
                espera *= 2

        raise ErroDeApi(
            f"[{self.nome}] {caminho} falhou após {self.tentativas} tentativas."
        ) from ultimo_erro

    def fechar(self) -> None:
        self.sessao.close()

    def __enter__(self) -> "ClienteApi":
        return self

    def __exit__(self, *_: object) -> None:
        self.fechar()
