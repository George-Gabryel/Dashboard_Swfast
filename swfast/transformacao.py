"""Etapa 2 — Transformação: do JSON bruto às tabelas tipadas.

Recebe os registros que a extração trouxe da API, aplica as regras das
planilhas e devolve uma tabela Polars por aba. Tudo em memória: nada aqui
chama a API nem toca no banco; a saída é o insumo da carga (`banco.py`).

Regras de dados faltantes:

- Texto vazio (`""` ou só espaços) vira NULL.
- NULL continua NULL.
- Lista vazia (`"descontos": []`) não gera nenhuma linha na tabela filha.
  Um item da lista com todos os campos vazios ou nulos também não.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from .esquemas import ESQUEMAS_POR_FONTE, Coluna, EsquemaTabela, Tipo

logger = logging.getLogger(__name__)

# Formatos de data aceitos, em ordem de tentativa. A API manda o primeiro;
# os outros cobrem variações sem custo.
FORMATOS_DATA_HORA = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S")
FORMATOS_DATA = ("%Y-%m-%d",)

TIPOS_POLARS: dict[Tipo, pl.DataType] = {
    Tipo.TEXTO: pl.String(),
    Tipo.INTEIRO: pl.Int64(),
    Tipo.DECIMAL: pl.Float64(),
    Tipo.DATA: pl.Date(),
    Tipo.DATA_HORA: pl.Datetime("us"),
}

CAMPO_ID_PAI = "id"
# Chave interna onde o id da venda viaja junto de cada item da lista. O
# prefixo evita colisão com qualquer campo que a API venha a mandar.
ID_DO_PAI = "__id_venda__"


class ErroDeTransformacao(RuntimeError):
    """Não há esquema para a fonte pedida."""


# ---------------------------------------------------------------------------
# Limpeza de valores
# ---------------------------------------------------------------------------
def limpar(valor: Any) -> Any:
    """Texto vazio vira None. Todo o resto passa intacto."""
    if isinstance(valor, str) and not valor.strip():
        return None
    return valor


def item_vazio(item: Any) -> bool:
    """Um item de lista sem nenhum dado: `{}`, ou só campos vazios/nulos."""
    if not isinstance(item, dict):
        return True
    return all(limpar(valor) is None for valor in item.values())


def como_texto(valor: Any) -> str | None:
    """Tudo entra como texto e só depois é convertido para o tipo da planilha.

    Assim um campo que vem como número em um registro e como texto em outro
    não quebra a montagem da tabela: a conversão é feita uma vez, na coluna
    inteira, e as falhas são contadas.
    """
    valor = limpar(valor)
    if valor is None:
        return None
    if isinstance(valor, bool):
        return "true" if valor else "false"
    if isinstance(valor, (dict, list)):
        return json.dumps(valor, ensure_ascii=False)
    return str(valor)


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------
@dataclass
class RelatorioTabela:
    """O que aconteceu com uma tabela — vai para o log e para os testes."""

    tabela: str
    linhas: int = 0
    itens_vazios_ignorados: int = 0
    sem_chave_descartadas: int = 0
    duplicadas_descartadas: int = 0
    falhas_de_conversao: dict[str, int] = field(default_factory=dict)
    campos_ausentes: list[str] = field(default_factory=list)
    campos_desconhecidos: list[str] = field(default_factory=list)

    @property
    def tem_alertas(self) -> bool:
        return bool(
            self.sem_chave_descartadas
            or self.duplicadas_descartadas
            or self.falhas_de_conversao
            or self.campos_ausentes
            or self.campos_desconhecidos
        )


# ---------------------------------------------------------------------------
# Transformador
# ---------------------------------------------------------------------------
class Transformador:
    """Converte os registros de uma fonte nas tabelas da planilha."""

    def __init__(self, esquemas: Sequence[EsquemaTabela]) -> None:
        self.esquemas = tuple(esquemas)

    @classmethod
    def para_fonte(cls, fonte: str) -> "Transformador":
        try:
            return cls(ESQUEMAS_POR_FONTE[fonte])
        except KeyError as erro:
            conhecidas = ", ".join(sorted(ESQUEMAS_POR_FONTE))
            raise ErroDeTransformacao(
                f"Não há esquema para a fonte '{fonte}'. Fontes conhecidas: {conhecidas}."
            ) from erro

    def transformar(
        self, registros: Iterable[dict[str, Any]]
    ) -> tuple[dict[str, pl.DataFrame], list[RelatorioTabela]]:
        registros = [registro for registro in registros if isinstance(registro, dict)]
        tabelas: dict[str, pl.DataFrame] = {}
        relatorios: list[RelatorioTabela] = []
        for esquema in self.esquemas:
            tabela, relatorio = self.transformar_tabela(registros, esquema)
            tabelas[esquema.nome] = tabela
            relatorios.append(relatorio)
        return tabelas, relatorios

    # -- uma tabela ---------------------------------------------------------
    def transformar_tabela(
        self, registros: list[dict[str, Any]], esquema: EsquemaTabela
    ) -> tuple[pl.DataFrame, RelatorioTabela]:
        relatorio = RelatorioTabela(esquema.nome)
        linhas = list(self._linhas(registros, esquema, relatorio))
        self._conferir_campos(linhas, esquema, relatorio)

        colunas = [self._converter(linhas, coluna, relatorio) for coluna in esquema.colunas]
        # Sem linhas, a tabela sai vazia mas com as colunas e os tipos certos.
        tabela = pl.DataFrame(colunas)

        tabela = self._aplicar_chave_primaria(tabela, esquema, relatorio)
        relatorio.linhas = tabela.height
        return tabela, relatorio

    def _linhas(
        self, registros: list[dict[str, Any]], esquema: EsquemaTabela, relatorio: RelatorioTabela
    ) -> Iterator[dict[str, Any]]:
        """Uma linha por registro, ou uma por item da lista, com o id do pai."""
        if esquema.lista is None:
            yield from registros
            return

        for registro in registros:
            # `null` no lugar da lista é tratado como lista vazia.
            for item in registro.get(esquema.lista) or []:
                if item_vazio(item):
                    relatorio.itens_vazios_ignorados += 1
                    continue
                yield {**item, ID_DO_PAI: registro.get(CAMPO_ID_PAI)}

    def _conferir_campos(
        self, linhas: list[dict[str, Any]], esquema: EsquemaTabela, relatorio: RelatorioTabela
    ) -> None:
        """Avisa quando o JSON e a planilha deixam de bater."""
        if not linhas:
            return
        presentes = {chave for linha in linhas for chave in linha if chave != ID_DO_PAI}
        relatorio.campos_ausentes = sorted(esquema.origens - presentes)
        relatorio.campos_desconhecidos = sorted(presentes - esquema.origens - esquema.descartados)

    def _converter(
        self, linhas: list[dict[str, Any]], coluna: Coluna, relatorio: RelatorioTabela
    ) -> pl.Series:
        """Monta a coluna como texto e converte para o tipo da planilha."""
        textos = pl.Series(
            coluna.destino,
            [como_texto(linha.get(coluna.origem or ID_DO_PAI)) for linha in linhas],
            dtype=pl.String,
        )
        if coluna.substituir:
            textos = textos.replace(coluna.substituir)

        convertida = converter(textos, coluna.tipo)

        # Valor que existia e sumiu na conversão = dado que não cabe no tipo.
        falhas = int((textos.is_not_null() & convertida.is_null()).sum())
        if falhas:
            relatorio.falhas_de_conversao[coluna.destino] = falhas
        return convertida.alias(coluna.destino)

    def _aplicar_chave_primaria(
        self, tabela: pl.DataFrame, esquema: EsquemaTabela, relatorio: RelatorioTabela
    ) -> pl.DataFrame:
        if tabela.is_empty():
            return tabela

        chave = list(esquema.chave_primaria)
        completas = tabela.drop_nulls(subset=chave)
        relatorio.sem_chave_descartadas = tabela.height - completas.height

        # O mesmo registro pode vir duas vezes no mesmo arquivo; fica o último.
        unicas = completas.unique(subset=chave, keep="last", maintain_order=True)
        relatorio.duplicadas_descartadas = completas.height - unicas.height
        return unicas


def converter(textos: pl.Series, tipo: Tipo) -> pl.Series:
    """Texto -> tipo da planilha. O que não converte vira NULL (e é contado)."""
    if tipo is Tipo.TEXTO:
        return textos

    if tipo is Tipo.DECIMAL:
        return textos.str.strip_chars().cast(pl.Float64, strict=False)

    if tipo is Tipo.INTEIRO:
        # Passa por Float64 porque a API às vezes manda inteiros como 12.0.
        # Um valor com casas decimais de verdade (12.5) não é inteiro: vira NULL.
        numeros = textos.str.strip_chars().cast(pl.Float64, strict=False)
        inteiros = pl.select(
            pl.when(numeros == numeros.round(0)).then(numeros).otherwise(None).cast(pl.Int64)
        ).to_series()
        return inteiros

    if tipo is Tipo.DATA_HORA:
        return _primeiro_formato(textos, FORMATOS_DATA_HORA, pl.Datetime("us"))

    if tipo is Tipo.DATA:
        # A API manda a data como 2026-10-17T00:00:00: lê com hora e descarta a hora.
        com_hora = _primeiro_formato(textos, FORMATOS_DATA_HORA, pl.Datetime("us")).dt.date()
        so_data = _primeiro_formato(textos, FORMATOS_DATA, pl.Date())
        return pl.select(pl.coalesce(com_hora, so_data)).to_series()

    raise ValueError(f"Tipo não suportado: {tipo}")


def _primeiro_formato(textos: pl.Series, formatos: Sequence[str], destino: pl.DataType) -> pl.Series:
    texto = textos.str.strip_chars()
    tentativas = [
        texto.str.to_datetime(formato, strict=False, time_unit="us")
        if isinstance(destino, pl.Datetime)
        else texto.str.to_date(formato, strict=False)
        for formato in formatos
    ]
    return pl.select(pl.coalesce(tentativas)).to_series()


def registrar_no_log(relatorio: RelatorioTabela) -> None:
    nome = relatorio.tabela
    logger.info("[%s] %d linhas", nome, relatorio.linhas)
    if relatorio.itens_vazios_ignorados:
        logger.info("[%s] %d itens vazios ignorados", nome, relatorio.itens_vazios_ignorados)
    if relatorio.sem_chave_descartadas:
        logger.warning("[%s] %d linhas sem chave primária descartadas", nome, relatorio.sem_chave_descartadas)
    if relatorio.duplicadas_descartadas:
        logger.warning("[%s] %d linhas duplicadas descartadas", nome, relatorio.duplicadas_descartadas)
    for coluna, quantidade in relatorio.falhas_de_conversao.items():
        logger.warning("[%s] %d valores de '%s' não couberam no tipo e viraram NULL", nome, quantidade, coluna)
    if relatorio.campos_ausentes:
        logger.warning("[%s] campos da planilha ausentes no JSON: %s", nome, ", ".join(relatorio.campos_ausentes))
    if relatorio.campos_desconhecidos:
        logger.warning(
            "[%s] campos novos no JSON, fora da planilha: %s",
            nome, ", ".join(relatorio.campos_desconhecidos),
        )

