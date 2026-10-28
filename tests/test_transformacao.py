"""Testes da transformação — regras das planilhas e de dados faltantes.

Rodar: pytest -q
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from swfast.esquemas import (
    DADOS_DESCONTO,
    DADOS_FISCAL,
    DADOS_PRODUTOS,
    DADOS_VENDA,
    ESQUEMAS_POR_FONTE,
    Coluna,
    EsquemaTabela,
    Tipo,
)
from swfast.transformacao import (
    ErroDeTransformacao,
    Transformador,
    converter,
    limpar,
)


def venda(**campos) -> dict:
    """Uma venda mínima no formato da API, com as listas vazias."""
    base = {
        "id": "V1",
        "emissor": "NFC-e",
        "nrMesa": 10,
        "data": "2026-10-17T00:00:00",
        "dataHoraTransacao": "2026-10-17T15:31:50",
        "valorFaturamento": 98.8,
        "produtos": [],
        "pagamentos": [],
        "descontos": [],
        "acrescimos": [],
        "fiscais": [],
    }
    base.update(campos)
    return base


def tabelas_de(*registros, fonte="vendas") -> dict[str, pl.DataFrame]:
    tabelas, _ = Transformador.para_fonte(fonte).transformar(list(registros))
    return tabelas


# -- dados faltantes -----------------------------------------------------------

def test_texto_vazio_vira_null_e_null_continua_null():
    tabela = tabelas_de(venda(operador="", setor="   ", entregador=None, loja="YUGO"))["dados_venda"]
    linha = tabela.row(0, named=True)
    assert linha["operador"] is None
    assert linha["setor"] is None
    assert linha["entregador"] is None
    assert linha["loja"] == "YUGO"


def test_listas_vazias_nao_geram_linhas():
    tabelas = tabelas_de(venda(descontos=[], acrescimos=[], fiscais=[]))
    assert tabelas["dados_venda"].height == 1
    for nome in ("dados_desconto", "dados_acrescimo", "dados_fiscal"):
        assert tabelas[nome].is_empty(), nome
        # Vazia, mas com as colunas e tipos da planilha.
        assert tabelas[nome].columns == next(
            e for e in ESQUEMAS_POR_FONTE["vendas"] if e.nome == nome
        ).destinos


def test_lista_nula_e_itens_sem_dados_tambem_nao_geram_linhas():
    registro = venda(descontos=None, acrescimos=[{}, {"id": "", "valorTaxa": None}])
    tabelas = tabelas_de(registro)
    assert tabelas["dados_desconto"].is_empty()
    assert tabelas["dados_acrescimo"].is_empty()


def test_campo_vazio_dentro_de_um_item_vira_null_mas_a_linha_fica():
    registro = venda(descontos=[{"id": "D1", "motivo": "", "valorDesconto": 10.0}])
    desconto = tabelas_de(registro)["dados_desconto"].row(0, named=True)
    assert desconto["id"] == "D1"
    assert desconto["motivo"] is None
    assert desconto["valor_desconto"] == 10.0


# -- regras da planilha --------------------------------------------------------

def test_tabelas_filhas_recebem_o_id_da_venda():
    registro = venda(
        id="VENDA-9",
        produtos=[{"id": "P1", "nrItem": 1}, {"id": "P2", "nrItem": 2}],
        pagamentos=[{"id": "G1", "valorPagamento": 50}],
    )
    tabelas = tabelas_de(registro)
    assert tabelas["dados_produtos"]["id_venda"].to_list() == ["VENDA-9", "VENDA-9"]
    assert tabelas["dados_pagamento"]["id_venda"].to_list() == ["VENDA-9"]


def test_nfe_vira_bnf_e_nfce_fica_igual():
    tabela = tabelas_de(venda(id="A", emissor="NF-e"), venda(id="B", emissor="NFC-e"))["dados_venda"]
    assert dict(zip(tabela["id"], tabela["emissor"])) == {"A": "BNF", "B": "NFC-e"}


def test_dados_de_cliente_nao_sao_armazenados():
    registro = venda(cliente="Fulano", clienteFone="81999999999", clienteDocumento="12345678900")
    for fonte, registros in (("vendas", [registro]), ("cancelamentos", [registro])):
        tabelas, relatorios = Transformador.para_fonte(fonte).transformar(registros)
        for tabela in tabelas.values():
            valores = {str(v) for coluna in tabela.columns for v in tabela[coluna].to_list()}
            assert not valores & {"Fulano", "81999999999", "12345678900"}
        # Descartados de propósito: não podem aparecer como "campo desconhecido".
        assert not any("cliente" in c.lower() for r in relatorios for c in r.campos_desconhecidos)


def test_tipos_de_destino():
    registro = venda(
        nrAbertura=3217,           # texto na planilha, inteiro no JSON
        emissor_Serie="2",         # inteiro na planilha, texto no JSON
        emissor_Numero=38487,      # texto
        qtdPagamentos=2,           # float
        produtos=[{"id": "P1", "codigoProduto": 762, "fatorCompra": 12.0}],
    )
    tabelas = tabelas_de(registro)
    linha = tabelas["dados_venda"].row(0, named=True)
    assert linha["nr_abertura"] == "3217"
    assert linha["emissor_serie"] == 2
    assert linha["emissor_numero"] == "38487"
    assert linha["qtd_pagamentos"] == 2.0
    assert linha["data"] == date(2026, 10, 17)
    assert linha["data_hora_transacao"] == datetime(2026, 10, 17, 15, 31, 50)
    produto = tabelas["dados_produtos"].row(0, named=True)
    assert produto["codigo_produto"] == "762"
    assert produto["fator_compra"] == 12


def test_codigos_fiscais_mantem_zeros_a_esquerda():
    registro = venda(fiscais=[{"emissorNumero_Item": 1, "imposto": "PIS", "ncm": "00000000", "cst": "01"}])
    fiscal = tabelas_de(registro)["dados_fiscal"].row(0, named=True)
    assert fiscal["ncm"] == "00000000"
    assert fiscal["cst"] == "01"


def test_fiscal_tem_uma_linha_por_item_e_por_imposto():
    fiscais = [
        {"emissorNumero_Item": item, "imposto": imposto}
        for item in (1, 2)
        for imposto in ("ICMS", "PIS", "COFINS")
    ]
    tabela = tabelas_de(venda(fiscais=fiscais))["dados_fiscal"]
    assert tabela.height == 6
    assert DADOS_FISCAL.chave_primaria == ("id_venda", "emissor_numero_item", "imposto")


def test_valor_desconto_item_fiscal_e_mantido_como_float():
    registro = venda(fiscais=[{"emissorNumero_Item": 1, "imposto": "PIS", "valorDescontoItem": 1}])
    tabelas, relatorios = Transformador([DADOS_FISCAL]).transformar([registro])
    assert tabelas["dados_fiscal"].schema["valor_desconto_item"] == pl.Float64
    assert tabelas["dados_fiscal"]["valor_desconto_item"].to_list() == [1.0]
    assert "valorDescontoItem" not in relatorios[0].campos_desconhecidos


# -- conversões ----------------------------------------------------------------

@pytest.mark.parametrize(
    ("valores", "tipo", "esperado"),
    [
        (["12", "12.0", " 7 "], Tipo.INTEIRO, [12, 12, 7]),
        (["12.5", "abc"], Tipo.INTEIRO, [None, None]),
        (["19.98", "0"], Tipo.DECIMAL, [19.98, 0.0]),
        (["2026-10-17T00:00:00", "2026-10-18"], Tipo.DATA, [date(2026, 10, 17), date(2026, 10, 18)]),
        (
            ["2026-10-17T15:31:50", "2026-10-17 15:31:50", "2026-10-17T15:31:50.250"],
            Tipo.DATA_HORA,
            [datetime(2026, 10, 17, 15, 31, 50)] * 2 + [datetime(2026, 10, 17, 15, 31, 50, 250000)],
        ),
        (["17/10/2026"], Tipo.DATA, [None]),
    ],
)
def test_converter(valores, tipo, esperado):
    assert converter(pl.Series(valores, dtype=pl.String), tipo).to_list() == esperado


def test_valor_que_nao_cabe_no_tipo_vira_null_e_e_contado():
    registro = venda(nrMesa="mesa 3")
    _, relatorios = Transformador.para_fonte("vendas").transformar([registro])
    venda_rel = next(r for r in relatorios if r.tabela == "dados_venda")
    assert venda_rel.falhas_de_conversao == {"nr_mesa": 1}
    assert venda_rel.tem_alertas


def test_limpar():
    assert limpar("") is None
    assert limpar("  \t") is None
    assert limpar(None) is None
    assert limpar(0) == 0
    assert limpar(" A ") == " A "


# -- chave primária e conferência de campos ------------------------------------

def test_linha_sem_chave_e_descartada_e_duplicada_fica_a_ultima():
    registros = [venda(id="A", nrMesa=1), venda(id="", nrMesa=2), venda(id="A", nrMesa=3)]
    _, relatorios = Transformador([DADOS_VENDA]).transformar(registros)
    tabela, _ = Transformador([DADOS_VENDA]).transformar_tabela(registros, DADOS_VENDA)
    assert tabela["nr_mesa"].to_list() == [3]
    assert relatorios[0].sem_chave_descartadas == 1
    assert relatorios[0].duplicadas_descartadas == 1


def test_campo_novo_no_json_gera_alerta():
    registro = venda(campoQueAApiInventou=1)
    _, relatorios = Transformador([DADOS_VENDA]).transformar([registro])
    assert relatorios[0].campos_desconhecidos == ["campoQueAApiInventou"]


def test_campo_da_planilha_ausente_no_json_vira_coluna_nula_com_alerta():
    esquema = EsquemaTabela("t", (Coluna("id", "id"), Coluna("sumiu", "sumiu", Tipo.DECIMAL)), ("id",))
    tabela, relatorio = Transformador([esquema]).transformar_tabela([{"id": "1"}], esquema)
    assert tabela["sumiu"].to_list() == [None]
    assert tabela["sumiu"].dtype == pl.Float64
    assert relatorio.campos_ausentes == ["sumiu"]


def test_fonte_desconhecida():
    with pytest.raises(ErroDeTransformacao):
        Transformador.para_fonte("estoques")


# -- esquemas ------------------------------------------------------------------

def test_cada_tabela_tem_chave_primaria_que_existe_nas_colunas():
    for esquemas in ESQUEMAS_POR_FONTE.values():
        for esquema in esquemas:
            assert set(esquema.chave_primaria) <= set(esquema.destinos), esquema.nome
            assert len(esquema.destinos) == len(set(esquema.destinos)), esquema.nome
    assert DADOS_PRODUTOS.lista == "produtos"
    assert DADOS_DESCONTO.lista == "descontos"
