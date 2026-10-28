"""Testes da carga no PostgreSQL.

Os de DDL rodam sempre. Os de integração sobem um Postgres de verdade com
testcontainers e são pulados quando o Docker não está rodando.

Rodar: pytest -q
"""

from __future__ import annotations

from datetime import date, datetime

import psycopg
import pytest

from swfast.banco import Banco, ddl
from swfast.esquemas import DADOS_FISCAL, DADOS_PRODUTOS, DADOS_VENDA
from swfast.transformacao import Transformador


# -- DDL -------------------------------------------------------------------------

def test_ddl_tem_tipos_e_chave_primaria():
    texto = ddl(DADOS_VENDA)[0].as_string(None)
    assert texto.startswith('CREATE TABLE IF NOT EXISTS "dados_venda"')
    assert '"nr_mesa" bigint' in texto
    assert '"valor_faturamento" double precision' in texto
    assert '"data" date' in texto
    assert '"data_hora_transacao" timestamp' in texto
    assert '"codigo_loja" text' in texto
    assert 'PRIMARY KEY ("id")' in texto
    # A venda não é filha de ninguém: não precisa de índice em id_venda.
    assert len(ddl(DADOS_VENDA)) == 1


def test_ddl_fiscal_tem_chave_composta_e_indice_em_id_venda():
    tabela, indice = (comando.as_string(None) for comando in ddl(DADOS_FISCAL))
    assert 'PRIMARY KEY ("id_venda", "emissor_numero_item", "imposto")' in tabela
    assert 'ON "dados_fiscal" ("id_venda")' in indice


def test_ddl_filha_tem_indice_em_id_venda():
    assert len(ddl(DADOS_PRODUTOS)) == 2


# -- integração --------------------------------------------------------------------

@pytest.fixture(scope="module")
def conninfo():
    try:
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer("postgres:16-alpine", driver=None)
        container.start()
    except Exception as erro:  # Docker parado, imagem indisponível...
        pytest.skip(f"Postgres de teste indisponível (o Docker está rodando?): {erro}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
def banco(conninfo):
    with Banco(conninfo, "teste") as banco:
        with banco.conexao.cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        banco.conexao.commit()
        banco.criar_tabelas()
        yield banco


def venda(id_venda="V1", produtos=None, **campos) -> dict:
    registro = {
        "id": id_venda,
        "emissor": "NF-e",
        "nrMesa": 10,
        "data": "2026-10-17T00:00:00",
        "dataHoraTransacao": "2026-10-17T15:31:50",
        "valorFaturamento": 98.8,
        "clienteDocumento": "12345678900",
        "produtos": produtos if produtos is not None else [{"id": "P1", "produto": "AGUA", "valorProduto": 9.9}],
        "pagamentos": [],
        "descontos": [],
        "acrescimos": [],
        "fiscais": [{"emissorNumero_Item": 1, "imposto": "PIS", "cst": "01"}],
    }
    registro.update(campos)
    return registro


def carregar(banco, *registros, fonte="vendas") -> dict[str, int]:
    tabelas, _ = Transformador.para_fonte(fonte).transformar(list(registros))
    return banco.carregar(fonte, tabelas)


def consultar(banco, comando: str) -> list[tuple]:
    with banco.conexao.cursor() as cursor:
        cursor.execute(comando)
        return cursor.fetchall()


def test_criar_tabelas_e_idempotente(banco):
    banco.criar_tabelas()
    tabelas = {linha[0] for linha in consultar(
        banco, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )}
    assert tabelas == {
        "dados_venda", "dados_produtos", "dados_pagamento", "dados_desconto",
        "dados_acrescimo", "dados_fiscal", "dados_venda_cancelamento", "dados_produto_cancelamento",
    }


def test_carrega_uma_venda_com_os_tipos_certos(banco):
    carregadas = carregar(banco, venda())
    assert carregadas["dados_venda"] == 1
    assert carregadas["dados_produtos"] == 1
    assert carregadas["dados_fiscal"] == 1

    emissor, mesa, dia, momento, faturamento = consultar(
        banco, "SELECT emissor, nr_mesa, data, data_hora_transacao, valor_faturamento FROM dados_venda"
    )[0]
    assert (emissor, mesa, faturamento) == ("BNF", 10, 98.8)
    assert dia == date(2026, 10, 17)
    assert momento == datetime(2026, 10, 17, 15, 31, 50)
    assert consultar(banco, "SELECT id_venda, cst FROM dados_fiscal") == [("V1", "01")]


def test_recarregar_a_mesma_venda_atualiza_sem_duplicar(banco):
    carregar(banco, venda(valorFaturamento=10.0))
    carregar(banco, venda(valorFaturamento=20.0))
    assert consultar(banco, "SELECT count(*), max(valor_faturamento) FROM dados_venda") == [(1, 20.0)]
    assert consultar(banco, "SELECT count(*) FROM dados_produtos") == [(1,)]


def test_item_que_sumiu_da_venda_sai_do_banco(banco):
    carregar(banco, venda(produtos=[{"id": "P1"}, {"id": "P2"}]))
    carregar(banco, venda(produtos=[{"id": "P1"}]))
    assert consultar(banco, "SELECT id FROM dados_produtos") == [("P1",)]


def test_carga_de_uma_venda_nao_mexe_nas_outras(banco):
    carregar(banco, venda("V1"), venda("V2", produtos=[{"id": "P2"}]))
    carregar(banco, venda("V1", produtos=[]))
    assert consultar(banco, "SELECT id_venda, id FROM dados_produtos") == [("V2", "P2")]


def test_lista_vazia_nao_insere_nada(banco):
    carregadas = carregar(banco, venda(descontos=[], acrescimos=[]))
    assert carregadas["dados_desconto"] == 0
    assert consultar(banco, "SELECT count(*) FROM dados_desconto") == [(0,)]


def test_erro_numa_tabela_filha_desfaz_a_fonte_inteira(banco, monkeypatch):
    carregar(banco, venda(valorFaturamento=10.0))

    original = Banco._upsert

    def falha_nos_produtos(self, cursor, esquema, dados):
        if esquema.nome == "dados_produtos":
            raise psycopg.errors.UniqueViolation("simulado")
        return original(self, cursor, esquema, dados)

    monkeypatch.setattr(Banco, "_upsert", falha_nos_produtos)
    with pytest.raises(psycopg.Error):
        carregar(banco, venda(valorFaturamento=99.0, produtos=[{"id": "P9"}]))

    # Nada da segunda carga ficou: a venda e os itens continuam os da primeira.
    assert consultar(banco, "SELECT valor_faturamento FROM dados_venda") == [(10.0,)]
    assert consultar(banco, "SELECT id FROM dados_produtos") == [("P1",)]


def test_cancelamentos(banco):
    cancelamento = {
        "id": "C1",
        "tipoCancelamento": "Total",
        "dataHoraCancelamento": "2026-10-17T15:31:25",
        "produtos": [{"id": "PC1", "nrItem": 1, "dataHoraCancelamento": "2026-10-17T15:31:25"}],
    }
    carregadas = carregar(banco, cancelamento, fonte="cancelamentos")
    assert carregadas == {"dados_venda_cancelamento": 1, "dados_produto_cancelamento": 1}
    assert consultar(banco, "SELECT id_venda FROM dados_produto_cancelamento") == [("C1",)]
