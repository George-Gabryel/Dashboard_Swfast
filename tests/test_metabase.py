"""Testes da instalação do dashboard — com um Metabase falso, sem rede.

Rodar: pytest -q
"""

from __future__ import annotations

import re

import pytest
import requests

from swfast import metabase
from swfast.config import Configuracao, ConfiguracaoMetabase
from swfast.metabase import ABAS, PARAMETROS, PERGUNTAS, ClienteMetabase
from swfast.visoes import VISOES

MB = ConfiguracaoMetabase(
    url="http://metabase:3000", url_publica="http://127.0.0.1:3000",
    admin_email="admin@exemplo.com.br", admin_senha="senha-admin",
    app_usuario="metabase_app", app_senha="senha-app",
    leitor_usuario="metabase_leitor", leitor_senha="senha-leitor",
)


class MetabaseFalso(ClienteMetabase):
    """Responde como a API do Metabase e guarda o que recebeu."""

    def __init__(self, configurado: bool = False, ja_instalado: bool = False) -> None:
        super().__init__("http://metabase:3000", requests.Session(), esperar=lambda _: None)
        self.configurado = configurado
        self.chamadas: list[tuple[str, str]] = []
        self.proximo_id = 100
        self.cards: dict[str, int] = {}
        self.dashboard: dict | None = None
        if ja_instalado:
            self.cards = {pergunta.nome: indice for indice, pergunta in enumerate(PERGUNTAS, start=1)}
            self.dashboard = {"id": 9, "tabs": [{"id": 50 + i, "name": nome} for i, nome in enumerate(ABAS)]}

    def _novo_id(self) -> int:
        self.proximo_id += 1
        return self.proximo_id

    def pedir(self, metodo, caminho, corpo=None, **parametros):
        self.chamadas.append((metodo, caminho))
        rotas = {
            ("GET", "/health"): lambda: {"status": "ok"},
            ("GET", "/session/properties"): lambda: {"has-user-setup": self.configurado, "setup-token": "tok"},
            ("POST", "/setup"): lambda: {"id": "sessao-setup"},
            ("POST", "/session"): lambda: {"id": "sessao-login"},
            ("GET", "/database"): lambda: {"data": [{"id": 7, "name": "Swfast"}] if self.dashboard else []},
            ("POST", "/database"): lambda: {"id": 7},
            ("PUT", "/database/7"): lambda: {"id": 7},
            ("POST", "/database/7/sync_schema"): lambda: {},
            ("GET", "/database/7/metadata"): lambda: {"tables": [
                {"name": nome, "fields": [{"name": "id", "id": 900 + i}, {"name": "data", "id": 800 + i}]}
                for i, nome in enumerate(VISOES)
            ]},
            ("GET", "/collection"): lambda: [{"id": 3, "name": "Swfast"}] if self.dashboard else [{"id": "root"}],
            ("POST", "/collection"): lambda: {"id": 3},
            ("PUT", "/setting/enable-public-sharing"): lambda: None,
            ("PUT", "/setting/custom-formatting"): lambda: None,
        }
        if (metodo, caminho) in rotas:
            return rotas[(metodo, caminho)]()
        if caminho == "/collection/3/items":
            if parametros.get("models") == "card":
                return {"data": [{"id": i, "name": n, "model": "card"} for n, i in self.cards.items()]}
            painel = [{"id": 9, "name": metabase.NOME_DASHBOARD, "model": "dashboard"}] if self.dashboard else []
            return {"data": painel}
        if (metodo, caminho) == ("POST", "/card"):
            self.cards[corpo["name"]] = self._novo_id()
            return {"id": self.cards[corpo["name"]]}
        if metodo == "PUT" and caminho.startswith("/card/"):
            return {"id": int(caminho.rsplit("/", 1)[1])}
        if (metodo, caminho) == ("POST", "/dashboard"):
            self.dashboard = {"id": 9, "tabs": []}
            return {"id": 9}
        if (metodo, caminho) == ("GET", "/dashboard/9"):
            return self.dashboard
        if (metodo, caminho) == ("PUT", "/dashboard/9"):
            self.dashboard = {"id": 9, **corpo}
            return self.dashboard
        if (metodo, caminho) == ("POST", "/dashboard/9/public_link"):
            return {"uuid": "abc-123"}
        raise AssertionError(f"rota não prevista: {metodo} {caminho}")


def test_metabase_novo_faz_a_configuracao_inicial():
    cliente = MetabaseFalso(configurado=False)
    cliente.entrar(MB)
    assert ("POST", "/setup") in cliente.chamadas
    assert cliente.sessao.headers["X-Metabase-Session"] == "sessao-setup"


def test_metabase_ja_configurado_faz_login():
    cliente = MetabaseFalso(configurado=True)
    cliente.entrar(MB)
    assert ("POST", "/setup") not in cliente.chamadas
    assert cliente.sessao.headers["X-Metabase-Session"] == "sessao-login"


def montar(cliente: MetabaseFalso) -> dict:
    campos = metabase.aguardar_sincronizacao(cliente, 7)
    metabase.montar_dashboard(cliente, 7, campos, 3)
    return cliente.dashboard


def test_monta_o_dashboard_com_abas_filtros_e_todas_as_perguntas():
    painel = montar(MetabaseFalso())

    assert [aba["name"] for aba in painel["tabs"]] == list(ABAS)
    assert {parametro["id"] for parametro in painel["parameters"]} == {"periodo", "dias", "metrica"}
    assert sum(len(pergunta.posicoes) for pergunta in PERGUNTAS) == len(painel["dashcards"])
    # Cada cartão só liga os filtros que a sua pergunta usa.
    for cartao in painel["dashcards"]:
        assert {m["parameter_id"] for m in cartao["parameter_mappings"]} <= set(PARAMETROS)
    # Cada aba recebe pelo menos uma pergunta.
    assert {c["dashboard_tab_id"] for c in painel["dashcards"]} == {aba["id"] for aba in painel["tabs"]}


def test_desconto_aparece_em_duas_abas_com_um_card_so():
    cliente = MetabaseFalso()
    painel = montar(cliente)
    card = cliente.cards["Desconto"]
    abas = {c["dashboard_tab_id"] for c in painel["dashcards"] if c["card_id"] == card}
    nomes = {aba["name"] for aba in painel["tabs"] if aba["id"] in abas}
    assert nomes == {"Faturamento", "Pagamentos e descontos"}


def test_filtro_exibir_liga_como_variavel_de_texto():
    cliente = MetabaseFalso()
    painel = montar(cliente)
    top10 = cliente.cards["Top 10 produtos"]
    ligacoes = next(c["parameter_mappings"] for c in painel["dashcards"] if c["card_id"] == top10)
    assert {"parameter_id": "metrica", "card_id": top10,
            "target": ["variable", ["template-tag", "metrica"]]} in ligacoes

    pergunta = next(p for p in PERGUNTAS if p.nome == "Top 10 produtos")
    campos = {nome: 800 + i for i, nome in enumerate(VISOES)}
    tag = metabase.corpo_da_pergunta(pergunta, 7, campos, 3)["dataset_query"]["native"]["template-tags"]["metrica"]
    assert tag["type"] == "text" and tag["default"] == "Valor (R$)"


def test_reinstalar_atualiza_sem_duplicar_e_arquiva_o_que_saiu():
    cliente = MetabaseFalso(configurado=True, ja_instalado=True)
    cliente.cards["Itens em promoção"] = 999
    montar(cliente)

    assert ("POST", "/card") not in cliente.chamadas
    assert ("POST", "/dashboard") not in cliente.chamadas
    assert ("PUT", "/card/999") in cliente.chamadas  # arquivada
    # As abas existentes mantêm o id.
    assert [aba["id"] for aba in cliente.dashboard["tabs"]] == [50, 51, 52, 53]


def test_kpi_comparado_usa_o_calendario_e_devolve_duas_linhas():
    pergunta = next(p for p in PERGUNTAS if p.nome == "Faturamento bruto")
    assert pergunta.display == "smartscalar"
    assert "FROM painel_calendario WHERE {{data}}" in pergunta.sql and "UNION ALL" in pergunta.sql
    assert pergunta.filtros[0].view == "painel_calendario"
    desconto = next(p for p in PERGUNTAS if p.nome == "Desconto")
    assert desconto.ajustes["scalar.switch_positive_negative"] is True


def test_tag_de_data_aponta_para_a_view_certa_com_id_estavel():
    pergunta = next(p for p in PERGUNTAS if p.nome == "Vendas")
    campos = {nome: 800 + i for i, nome in enumerate(VISOES)}
    tag = metabase.corpo_da_pergunta(pergunta, 7, campos, 3)["dataset_query"]["native"]["template-tags"]["data"]
    assert tag["dimension"] == ["field", campos["painel_vendas"], None]
    assert tag["id"] == metabase.corpo_da_pergunta(pergunta, 7, campos, 3)["dataset_query"]["native"]["template-tags"]["data"]["id"]


def test_toda_pergunta_usa_so_filtros_declarados():
    for pergunta in PERGUNTAS:
        tags_no_sql = set(re.findall(r"\{\{(\w+)\}\}", pergunta.sql))
        assert tags_no_sql == {filtro.tag for filtro in pergunta.filtros}, pergunta.nome
        for filtro in pergunta.filtros:
            assert filtro.parametro in PARAMETROS, pergunta.nome
            if filtro.view is not None:
                assert filtro.view in VISOES and filtro.view in pergunta.sql, pergunta.nome
    assert len({pergunta.nome for pergunta in PERGUNTAS}) == len(PERGUNTAS)


def test_layout_cabe_na_grade_e_nada_se_sobrepoe():
    ocupado: dict[str, set[tuple[int, int]]] = {aba: set() for aba in ABAS}
    for pergunta in PERGUNTAS:
        for pos in pergunta.posicoes:
            assert pos.aba in ABAS and pos.coluna + pos.largura <= 24, pergunta.nome
            celulas = {(pos.linha + l, pos.coluna + c) for l in range(pos.altura) for c in range(pos.largura)}
            assert not celulas & ocupado[pos.aba], f"{pergunta.nome} sobrepõe outro cartão em {pos.aba}"
            ocupado[pos.aba] |= celulas


def test_instalar_grava_o_link_publico(tmp_path, monkeypatch):
    monkeypatch.setattr(metabase, "preparar_banco", lambda *_: None)
    configuracao = Configuracao(
        url_base="https://api.exemplo", api_key="chave",
        postgres_banco="swfast", postgres_usuario="sa", postgres_senha="s",
        diretorio_manifesto=tmp_path / "_manifesto", diretorio_logs=tmp_path / "_logs",
    )
    url = metabase.instalar(configuracao, MB, MetabaseFalso())

    assert url == "http://127.0.0.1:3000/public/dashboard/abc-123"
    assert (tmp_path / "dashboard_url.txt").read_text(encoding="utf-8").strip() == url


def test_sincronizacao_que_nao_termina_vira_erro(monkeypatch):
    cliente = MetabaseFalso()
    cliente.pedir = lambda *_, **__: {"tables": []}
    monkeypatch.setattr(metabase.time, "monotonic", iter(range(0, 10_000, 100)).__next__)
    with pytest.raises(metabase.ErroDeMetabase, match="painel_vendas"):
        metabase.aguardar_sincronizacao(cliente, 7, limite_segundos=200)


def test_sem_argumento_mostra_o_uso():
    assert metabase.main([]) == 2


def test_faturamento_por_hora_mostra_so_horas_com_venda():
    pergunta = next(p for p in PERGUNTAS if p.nome == "Faturamento por hora")
    assert "generate_series" not in pergunta.sql
    assert pergunta.ajustes["graph.x_axis.scale"] == "ordinal"


def test_pizzas_no_estilo_top5_com_outros_e_total():
    for nome in ("Top 5 grupos", "Top 5 subgrupos"):
        pergunta = next(p for p in PERGUNTAS if p.nome == nome)
        assert "'OUTROS'" in pergunta.sql and "{{metrica}}" in pergunta.sql
        assert pergunta.ajustes["pie.percent_visibility"] == "inside"
        assert pergunta.ajustes["pie.show_total"] is True and pergunta.ajustes["pie.show_legend"] is True
        assert "·" not in pergunta.sql  # nomes limpos na legenda
