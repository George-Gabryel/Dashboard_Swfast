"""Configuração central do pipeline, carregada a partir do arquivo .env.

Nenhuma outra parte do código lê variáveis de ambiente: tudo passa por aqui.
Assim existe um único lugar para saber de onde vem cada valor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfiguracaoInvalida(RuntimeError):
    """Erro de configuração: variável obrigatória ausente ou com valor inválido."""


def _obrigatorio(nome: str) -> str:
    valor = os.getenv(nome, "").strip()
    if not valor:
        raise ConfiguracaoInvalida(
            f"A variável {nome} não está definida no .env. "
            "Copie o arquivo .env.example para .env e preencha os valores."
        )
    return valor


def _opcional(nome: str, padrao: str = "") -> str:
    return os.getenv(nome, padrao).strip()


def _inteiro(nome: str, padrao: int) -> int:
    bruto = _opcional(nome)
    if not bruto:
        return padrao
    try:
        return int(bruto)
    except ValueError as erro:
        raise ConfiguracaoInvalida(f"{nome} precisa ser um número inteiro.") from erro


def _booleano(nome: str, padrao: bool) -> bool:
    bruto = _opcional(nome).lower()
    if not bruto:
        return padrao
    return bruto in {"1", "true", "sim", "yes", "y"}


@dataclass(frozen=True)
class Configuracao:
    """Todos os parâmetros de execução do pipeline.

    É `frozen` de propósito: depois de carregada, a configuração não muda
    durante a execução. Se algum valor precisar mudar, muda no .env e roda
    de novo.
    """

    url_base: str
    api_key: str

    diretorio_raw: Path
    diretorio_manifesto: Path

    dias_por_janela: int = 7
    intervalo_entre_chamadas: float = 5.0
    timeout_segundos: int = 120
    tentativas: int = 3
    espera_inicial_retentativa: float = 2.0

    lista_de_lojas: str | None = None
    usar_loja_id_externo: bool = True

    @classmethod
    def a_partir_do_ambiente(cls, caminho_env: str | Path | None = None) -> "Configuracao":
        """Lê o .env e devolve a configuração já validada."""
        load_dotenv(caminho_env, override=False)

        diretorio_dados = Path(_opcional("DIRETORIO_DADOS", "data")).expanduser()

        configuracao = cls(
            url_base=_opcional("SWFAST_URL", "https://analytics-api.swfast.com.br/v5"),
            api_key=_obrigatorio("SWFAST_API_KEY"),
            diretorio_raw=diretorio_dados / "raw",
            diretorio_manifesto=diretorio_dados / "_manifesto",
            dias_por_janela=_inteiro("DIAS_POR_JANELA", 7),
            intervalo_entre_chamadas=float(_inteiro("INTERVALO_ENTRE_CHAMADAS", 5)),
            timeout_segundos=_inteiro("TIMEOUT_SEGUNDOS", 120),
            tentativas=_inteiro("TENTATIVAS", 3),
            espera_inicial_retentativa=float(_inteiro("ESPERA_INICIAL_RETENTATIVA", 2)),
            lista_de_lojas=_opcional("LISTA_DE_LOJAS") or None,
            usar_loja_id_externo=_booleano("USAR_LOJA_ID_EXTERNO", True),
        )
        configuracao.validar()
        return configuracao

    def validar(self) -> None:
        if not 1 <= self.dias_por_janela <= 7:
            raise ConfiguracaoInvalida(
                "DIAS_POR_JANELA precisa ficar entre 1 e 7 — a API recusa períodos maiores."
            )
        if self.tentativas < 1:
            raise ConfiguracaoInvalida("TENTATIVAS precisa ser no mínimo 1.")
        if self.intervalo_entre_chamadas < 0:
            raise ConfiguracaoInvalida("INTERVALO_ENTRE_CHAMADAS não pode ser negativo.")

    def preparar_diretorios(self) -> None:
        self.diretorio_raw.mkdir(parents=True, exist_ok=True)
        self.diretorio_manifesto.mkdir(parents=True, exist_ok=True)
