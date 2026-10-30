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
        banco, "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
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


# -- views do painel -----------------------------------------------------------------

def test_views_do_painel_existem(banco):
    from swfast.visoes import VISOES

    views = {linha[0] for linha in consultar(banco, "SELECT table_name FROM information_schema.views WHERE table_schema = 'public'")}
    assert views == set(VISOES)


def test_painel_vendas_calcula_hora_dia_da_semana_e_faturamento(banco):
    carregar(
        banco,
        venda("V1", valorFaturamento=100.0, qtdPessoas=2, dataHoraTransacao="2026-10-23T20:15:00", data="2026-10-23T00:00:00"),
        venda("V2", valorFaturamento=50.0, qtdPessoas=1, dataHoraTransacao="2026-10-24T12:05:00", data="2026-10-24T00:00:00"),
    )
    linhas = consultar(
        banco, "SELECT id, hora, dia_semana, dia_semana_numero, valor_faturamento FROM painel_vendas ORDER BY id"
    )
    assert linhas == [("V1", 20, "Sexta", 5, 100.0), ("V2", 12, "Sábado", 6, 50.0)]
    assert consultar(banco, "SELECT avg(valor_faturamento), sum(qtd_pessoas) FROM painel_vendas") == [(75.0, 3)]


def test_painel_produtos_e_pagamentos_trazem_a_data_da_venda(banco):
    carregar(banco, venda("V1", pagamentos=[
        {"id": "G1", "formaPagamento": "MASTER CREDITO", "valorPagamento": 60.0},
        {"id": "G2", "formaPagamento": "PIX", "valorPagamento": 40.0},
        {"id": "G3", "formaPagamento": "VR VOUCHER", "valorPagamento": 10.0},
    ]))
    assert consultar(banco, "SELECT data FROM painel_produtos") == [(date(2026, 10, 17),)]
    assert consultar(banco, "SELECT forma_pagamento, tipo_pagamento FROM painel_pagamentos ORDER BY id") == [
        ("MASTER CREDITO", "Crédito"), ("PIX", "PIX"), ("VR VOUCHER", "Voucher"),
    ]


def test_painel_cancelamentos_usa_a_data_do_cancelamento(banco):
    carregar(banco, {
        "id": "C1",
        "operador": "KARINE",
        "tipoCancelamento": "Total",
        "dataHoraCancelamento": "2026-10-17T15:31:25",
        "motivoCancelamento": "DESISTÊNCIA",
        "produtos": [{"id": "PC1", "produto": "AGUA", "qtdProdutos": 2, "valorProduto": 19.8}],
    }, fonte="cancelamentos")
    assert consultar(
        banco, "SELECT data, motivo_cancelamento, tipo_cancelamento, operador_caixa, valor_produto FROM painel_cancelamentos"
    ) == [(date(2026, 10, 17), "DESISTÊNCIA", "Total", "KARINE", 19.8)]


def test_views_sobrevivem_a_criar_tabelas_de_novo(banco):
    carregar(banco, venda("V1"))
    banco.criar_tabelas()
    assert consultar(banco, "SELECT count(*) FROM painel_vendas") == [(1,)]


def test_painel_vendas_separa_bruto_desconto_e_liquido(banco):
    # Como a API manda: o faturamento já vem sem convênio e sem cortesia.
    carregar(banco, venda(
        "V1", valorProdutos=200.0, valorTaxaServicos=20.0, valorDescontos=10.0,
        valorCortesias=30.0, valorFaturamento=180.0,
    ))
    assert consultar(
        banco, "SELECT valor_bruto, valor_desconto_total, valor_liquido FROM painel_vendas"
    ) == [(220.0, 40.0, 180.0)]


def test_painel_calendario_cobre_hoje_e_o_proximo_ano(banco):
    assert consultar(
        banco,
        "SELECT bool_or(data = current_date), max(data) >= current_date + 365, min(data) FROM painel_calendario",
    ) == [(True, True, date(2020, 1, 1))]


def test_cancelamento_total_usa_a_hora_da_venda_e_item_a_hora_do_item(banco):
    carregar(banco, {
        "id": "C1", "tipoCancelamento": "Total", "dataHoraCancelamento": "2026-10-17T22:10:00",
        "produtos": [{"id": "PT", "tipoCancelamento": "Total", "dataHoraCancelamento": "2026-10-17T14:00:00"}],
    }, {
        "id": "C2", "tipoCancelamento": "Item", "dataHoraCancelamento": "2026-10-18T09:00:00",
        "produtos": [{"id": "PI", "tipoCancelamento": "Item", "dataHoraCancelamento": "2026-10-18T23:45:00"}],
    }, fonte="cancelamentos")
    assert consultar(banco, "SELECT id, data, hora FROM painel_cancelamentos ORDER BY id") == [
        ("PI", date(2026, 10, 18), 23),  # Item: hora do item
        ("PT", date(2026, 10, 17), 22),  # Total: hora da venda cancelada
    ]


def sql_do_kpi(nome: str, inicio: str, fim: str) -> str:
    """O SQL de um bloco comparado com o filtro Período já expandido, como o Metabase faz."""
    from swfast.metabase import PERGUNTAS

    pergunta = next(p for p in PERGUNTAS if p.nome == nome)
    return pergunta.sql.replace(
        "{{data}}", f"\"public\".\"painel_calendario\".\"data\" BETWEEN date '{inicio}' AND date '{fim}'"
    )


@pytest.mark.parametrize("inicio, fim, anterior", [
    ("2026-10-01", "2026-10-31", date(2026, 9, 1)),   # Este mês → setembro inteiro
    ("2026-09-01", "2026-09-30", date(2026, 8, 1)),   # Mês passado → agosto
    ("2026-10-23", "2026-10-29", date(2026, 10, 16)),  # Últimos 7 dias → os 7 anteriores
    ("2026-10-25", "2026-10-31", date(2026, 10, 18)),  # Esta semana → semana passada
])
def test_kpi_compara_com_o_periodo_anterior_certo(banco, inicio, fim, anterior):
    linhas = consultar(banco, sql_do_kpi("Faturamento bruto", inicio, fim))
    assert [linha[0] for linha in linhas] == [anterior, date.fromisoformat(inicio)]


def test_kpi_soma_cada_periodo_separado(banco):
    carregar(
        banco,
        venda("SET", data="2026-09-30T00:00:00", valorProdutos=100.0, valorTaxaServicos=0.0, valorFaturamento=100.0),
        venda("OUT", data="2026-10-01T00:00:00", valorProdutos=150.0, valorTaxaServicos=0.0, valorFaturamento=150.0),
        venda("AGO", data="2026-08-31T00:00:00", valorProdutos=999.0, valorTaxaServicos=0.0, valorFaturamento=999.0),
    )
    # Outubro inteiro contra setembro inteiro: 31/08 fica de fora.
    assert consultar(banco, sql_do_kpi("Faturamento bruto", "2026-10-01", "2026-10-31")) == [
        (date(2026, 9, 1), 100.0), (date(2026, 10, 1), 150.0),
    ]
