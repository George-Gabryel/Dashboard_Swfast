"""Etapa 3 — Carga: das tabelas transformadas para o PostgreSQL.

As tabelas do banco nascem de `esquemas.py`: cada `EsquemaTabela` vira um
`CREATE TABLE IF NOT EXISTS`, com os tipos e a chave primária da planilha.
Não existe arquivo .sql para manter em paralelo.

Como a carga funciona, por fonte, numa única transação:

1. Tabela pai (a venda): upsert pela chave primária. A mesma venda volta em
   várias janelas de 7 dias, e precisa ser atualizada, não duplicada.
2. Tabelas filhas (produtos, pagamentos...): apaga as linhas das vendas do
   lote e grava as que vieram agora. Um item que sumiu da venda na API
   também some do banco.
3. Qualquer erro desfaz a fonte inteira: nunca fica venda sem itens nem
   item sem a venda atualizada.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import polars as pl
import psycopg
from psycopg import sql

from .esquemas import ESQUEMAS_POR_FONTE, EsquemaTabela, Tipo

logger = logging.getLogger(__name__)

TIPOS_SQL: dict[Tipo, str] = {
    Tipo.TEXTO: "text",
    Tipo.INTEIRO: "bigint",
    Tipo.DECIMAL: "double precision",
    Tipo.DATA: "date",
    Tipo.DATA_HORA: "timestamp",
}

COLUNA_DO_PAI = "id_venda"


class ErroDeBanco(RuntimeError):
    """Não foi possível conectar ao PostgreSQL."""


def ddl(esquema: EsquemaTabela) -> list[sql.Composed]:
    """Os comandos que criam a tabela (e o índice de id_venda nas filhas)."""
    colunas = sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(coluna.destino), sql.SQL(TIPOS_SQL[coluna.tipo]))
        for coluna in esquema.colunas
    )
    comandos = [
        sql.SQL("CREATE TABLE IF NOT EXISTS {} ({}, PRIMARY KEY ({}))").format(
            sql.Identifier(esquema.nome),
            colunas,
            sql.SQL(", ").join(map(sql.Identifier, esquema.chave_primaria)),
        )
    ]
    if esquema.lista is not None:
        # As filhas são apagadas por id_venda a cada carga: sem índice, cada
        # DELETE varreria a tabela inteira.
        comandos.append(
            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} ({})").format(
                sql.Identifier(f"ix_{esquema.nome}_{COLUNA_DO_PAI}"),
                sql.Identifier(esquema.nome),
                sql.Identifier(COLUNA_DO_PAI),
            )
        )
    return comandos


class Banco:
    """Uma conexão com o PostgreSQL, usada como `with Banco(...) as banco:`."""

    def __init__(self, conninfo: str, endereco: str = "") -> None:
        self.conninfo = conninfo
        #: host:porta/banco, para mensagens. Nunca contém a senha.
        self.endereco = endereco
        self.conexao: psycopg.Connection | None = None

    # -- conexão ------------------------------------------------------------
    def conectar(self) -> "Banco":
        if self.conexao is None or self.conexao.closed:
            try:
                self.conexao = psycopg.connect(self.conninfo, connect_timeout=10)
            except psycopg.OperationalError as erro:
                endereco = self.endereco or "endereço configurado"
                if "authentication failed" in str(erro):
                    # O servidor respondeu: o problema é a credencial, não o container.
                    motivo = "usuário ou senha recusados — confira POSTGRES_USER e POSTGRES_PASSWORD no .env"
                else:
                    motivo = "inacessível — o container está rodando?"
                raise ErroDeBanco(f"Postgres em {endereco}: {motivo}. Detalhe: {erro}") from erro
            logger.info("Conectado ao Postgres em %s", self.endereco)
        return self

    def fechar(self) -> None:
        if self.conexao is not None and not self.conexao.closed:
            self.conexao.close()

    def __enter__(self) -> "Banco":
        return self.conectar()

    def __exit__(self, *_) -> None:
        self.fechar()

    # -- estrutura ----------------------------------------------------------
    def criar_tabelas(self, esquemas: Sequence[EsquemaTabela] | None = None) -> None:
        """Cria o que ainda não existe. Rodar de novo não altera nada."""
        if esquemas is None:
            esquemas = [esquema for lista in ESQUEMAS_POR_FONTE.values() for esquema in lista]
        with self.conexao.transaction(), self.conexao.cursor() as cursor:
            for esquema in esquemas:
                for comando in ddl(esquema):
                    cursor.execute(comando)

    # -- carga --------------------------------------------------------------
    def carregar(self, fonte: str, tabelas: dict[str, pl.DataFrame]) -> dict[str, int]:
        """Grava as tabelas de uma fonte. Devolve as linhas gravadas por tabela."""
        esquemas = ESQUEMAS_POR_FONTE[fonte]
        pai = next(esquema for esquema in esquemas if esquema.lista is None)
        ids_do_lote = tabelas[pai.nome]["id"].to_list()

        carregadas: dict[str, int] = {}
        with self.conexao.transaction(), self.conexao.cursor() as cursor:
            for esquema in esquemas:
                dados = tabelas[esquema.nome]
                if esquema.lista is not None and ids_do_lote:
                    cursor.execute(
                        sql.SQL("DELETE FROM {} WHERE {} = ANY(%s)").format(
                            sql.Identifier(esquema.nome), sql.Identifier(COLUNA_DO_PAI)
                        ),
                        (ids_do_lote,),
                    )
                carregadas[esquema.nome] = self._upsert(cursor, esquema, dados)
        return carregadas

    def _upsert(self, cursor: psycopg.Cursor, esquema: EsquemaTabela, dados: pl.DataFrame) -> int:
        """COPY para uma tabela temporária e INSERT ... ON CONFLICT na definitiva.

        O COPY é o caminho rápido do Postgres para muitas linhas; a tabela
        temporária existe porque o COPY não sabe fazer upsert sozinho.
        Mesmo nas filhas o upsert fica: se um item mudar de venda, o id dele
        já existe no banco e um INSERT simples quebraria a carga.
        """
        if dados.is_empty():
            return 0

        tabela = sql.Identifier(esquema.nome)
        temporaria = sql.Identifier(f"tmp_{esquema.nome}")
        colunas = esquema.destinos
        lista_colunas = sql.SQL(", ").join(map(sql.Identifier, colunas))

        cursor.execute(
            sql.SQL("CREATE TEMP TABLE {} (LIKE {}) ON COMMIT DROP").format(temporaria, tabela)
        )
        with cursor.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(temporaria, lista_colunas)) as copia:
            for linha in dados.select(colunas).iter_rows():
                copia.write_row(linha)

        atualizaveis = [coluna for coluna in colunas if coluna not in esquema.chave_primaria]
        ao_conflitar = (
            sql.SQL("DO UPDATE SET {}").format(
                sql.SQL(", ").join(
                    sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(coluna)) for coluna in atualizaveis
                )
            )
            if atualizaveis
            else sql.SQL("DO NOTHING")
        )
        cursor.execute(
            sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} ON CONFLICT ({}) {}").format(
                tabela,
                lista_colunas,
                lista_colunas,
                temporaria,
                sql.SQL(", ").join(map(sql.Identifier, esquema.chave_primaria)),
                ao_conflitar,
            )
        )
        return cursor.rowcount
