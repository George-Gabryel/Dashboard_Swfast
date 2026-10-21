"""Testes da etapa de extração — sem bater na API.

Rodar: pytest -q
"""

from __future__ import annotations

from datetime import date

import pytest

from swfast.extratores import Extrator
from swfast.periodo import LIMITE_DIAS_API, Periodo, PeriodoInvalido


def test_ultimos_dias_conta_as_duas_pontas():
    periodo = Periodo.ultimos_dias(7, referencia=date(2026, 10, 21))
    assert periodo.inicio == date(2026, 10, 15)
    assert periodo.fim == date(2026, 10, 21)
    assert periodo.dias == 7


def test_periodo_acima_do_limite_e_recusado():
    with pytest.raises(PeriodoInvalido):
        Periodo(inicio=date(2026, 10, 1), fim=date(2026, 10, 31))


def test_fim_antes_do_inicio_e_recusado():
    with pytest.raises(PeriodoInvalido):
        Periodo(inicio=date(2026, 10, 10), fim=date(2026, 10, 1))


def test_fatiar_cobre_o_intervalo_inteiro_sem_buraco_nem_sobreposicao():
    janelas = Periodo.fatiar(date(2026, 1, 1), date(2026, 3, 31), LIMITE_DIAS_API)
    assert janelas[0].inicio == date(2026, 1, 1)
    assert janelas[-1].fim == date(2026, 3, 31)
    assert all(janela.dias <= LIMITE_DIAS_API for janela in janelas)
    for anterior, proxima in zip(janelas, janelas[1:]):
        assert (proxima.inicio - anterior.fim).days == 1


def test_parametros_no_formato_da_v5():
    periodo = Periodo(inicio=date(2026, 10, 15), fim=date(2026, 10, 21))
    extrator = Extrator("cancelamentos", "/common/vendas/cancelamentos", cliente=None)
    assert extrator.montar_parametros(periodo) == {
        "dataInicio": "2026-10-15",
        "dataFim": "2026-10-21",
    }


def test_filtro_de_loja_entra_apenas_quando_configurado():
    periodo = Periodo(inicio=date(2026, 10, 15), fim=date(2026, 10, 21))
    com_filtro = Extrator(
        "vendas", "/common/vendas", cliente=None,
        lista_de_lojas="101,102", usar_loja_id_externo=True,
    )
    parametros = com_filtro.montar_parametros(periodo)
    assert parametros["listaDeLojas"] == "101,102"
    assert parametros["usarLojaIdExterno"] == "true"

    sem_filtro = Extrator("vendas", "/common/vendas", cliente=None)
    assert "listaDeLojas" not in sem_filtro.montar_parametros(periodo)


def test_normalizar_aceita_objeto_lista_e_vazio():
    extrator = Extrator("vendas", "/common/vendas", cliente=None)
    assert extrator.normalizar(None) == []
    assert extrator.normalizar({"ID": "1"}) == [{"ID": "1"}]
    assert extrator.normalizar([{"ID": "1"}, {"ID": "2"}]) == [{"ID": "1"}, {"ID": "2"}]


def test_catalogo_tem_as_duas_fontes_da_v5(monkeypatch):
    from swfast.catalogo import construir_catalogo
    from swfast.config import Configuracao

    monkeypatch.setenv("SWFAST_API_KEY", "chave-de-teste")
    configuracao = Configuracao.a_partir_do_ambiente()
    fontes = construir_catalogo(configuracao)

    assert [fonte.nome for fonte in fontes] == ["vendas", "cancelamentos"]
    assert all(fonte.caminho.startswith("/common/vendas") for fonte in fontes)
