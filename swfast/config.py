"""Configuração central do pipeline, carregada a partir do arquivo .env.

Nenhuma outra parte do código lê variáveis de ambiente: tudo passa por aqui.
Assim existe um único lugar para saber de onde vem cada valor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from psycopg.conninfo import make_conninfo


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

    postgres_banco: str
    postgres_usuario: str
    postgres_senha: str

    diretorio_manifesto: Path
    diretorio_logs: Path

    postgres_host: str = "localhost"
    postgres_porta: int = 5432

    dias_por_janela: int = 7
    intervalo_entre_chamadas: float = 5.0
    timeout_segundos: int = 120
    tentativas: int = 3
    espera_inicial_retentativa: float = 2.0
    #: Modo --continuo: um ciclo a cada N horas, em horários fixos a partir de 00:00.
    intervalo_horas: int = 3

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
            postgres_banco=_obrigatorio("POSTGRES_DB"),
            postgres_usuario=_obrigatorio("POSTGRES_USER"),
            postgres_senha=_obrigatorio("POSTGRES_PASSWORD"),
            postgres_host=_opcional("POSTGRES_HOST", "localhost"),
            postgres_porta=_inteiro("POSTGRES_PORT", 5432),
            diretorio_manifesto=diretorio_dados / "_manifesto",
            diretorio_logs=diretorio_dados / "_logs",
            dias_por_janela=_inteiro("DIAS_POR_JANELA", 7),
            intervalo_entre_chamadas=float(_inteiro("INTERVALO_ENTRE_CHAMADAS", 5)),
            timeout_segundos=_inteiro("TIMEOUT_SEGUNDOS", 120),
            tentativas=_inteiro("TENTATIVAS", 3),
            espera_inicial_retentativa=float(_inteiro("ESPERA_INICIAL_RETENTATIVA", 2)),
            intervalo_horas=_inteiro("INTERVALO_HORAS", 3),
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
        if self.intervalo_horas < 1 or 24 % self.intervalo_horas:
            # Os horários são fixos a partir de 00:00; com 5h o último ciclo do
            # dia (20h) e o primeiro do seguinte (00h) ficariam a 4h um do outro.
            raise ConfiguracaoInvalida(
                "INTERVALO_HORAS precisa dividir 24 exatamente: 1, 2, 3, 4, 6, 8, 12 ou 24."
            )

    @property
    def conninfo(self) -> str:
        """String de conexão do psycopg. A senha não aparece em `endereco_banco`."""
        return make_conninfo(
            host=self.postgres_host,
            port=self.postgres_porta,
            dbname=self.postgres_banco,
            user=self.postgres_usuario,
            password=self.postgres_senha,
        )

    @property
    def endereco_banco(self) -> str:
        """Para mensagens de log: host:porta/banco, sem credenciais."""
        return f"{self.postgres_host}:{self.postgres_porta}/{self.postgres_banco}"

    def preparar_diretorios(self) -> None:
        self.diretorio_manifesto.mkdir(parents=True, exist_ok=True)
        self.diretorio_logs.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class ConfiguracaoMetabase:
    """O que `python -m swfast.metabase instalar` precisa além da Configuracao.

    Vem do .env.meta, separado do .env: o ETL não usa nada disto e o
    container do Metabase não precisa da chave da API Swfast.
    """

    url: str
    url_publica: str
    admin_email: str
    admin_senha: str
    app_usuario: str
    app_senha: str
    leitor_usuario: str
    leitor_senha: str
    #: Como o Metabase enxerga o Postgres (pela rede dos containers).
    banco_host: str = "postgres"
    banco_porta: int = 5432

    @classmethod
    def a_partir_do_ambiente(cls, caminho_env: str | Path | None = None) -> "ConfiguracaoMetabase":
        load_dotenv(caminho_env, override=False)
        return cls(
            url=_opcional("METABASE_URL", "http://metabase:3000").rstrip("/"),
            # O endereço que o navegador do cliente usa — não o da rede interna.
            url_publica=_opcional("METABASE_URL_PUBLICA", "http://127.0.0.1:3000").rstrip("/"),
            admin_email=_obrigatorio("METABASE_ADMIN_EMAIL"),
            admin_senha=_obrigatorio("METABASE_ADMIN_SENHA"),
            app_usuario=_opcional("MB_DB_USER", "metabase_app"),
            app_senha=_obrigatorio("MB_DB_PASS"),
            leitor_usuario=_opcional("METABASE_LEITOR_USUARIO", "metabase_leitor"),
            leitor_senha=_obrigatorio("METABASE_LEITOR_SENHA"),
            banco_host=_opcional("METABASE_BANCO_HOST", "postgres"),
            banco_porta=_inteiro("METABASE_BANCO_PORTA", 5432),
        )
