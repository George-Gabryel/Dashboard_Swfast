"""Testes da instalação do dashboard — com um Metabase falso, sem rede.

Rodar: pytest -q
"""

from __future__ import annotations

from datetime import date

import pytest
import requests

from swfast import metabase
from swfast.config import Configuracao, ConfiguracaoMetabase
from swfast.metabase import ABAS, PERGUNTAS, ClienteMetabase
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


def test_monta_o_dashboard_com_abas_filtro_e_todas_as_perguntas():
    cliente = MetabaseFalso()
    campos = metabase.aguardar_sincronizacao(cliente, 7)
    metabase.montar_dashboard(cliente, 7, campos, 3)

    painel = cliente.dashboard
    assert [aba["name"] for aba in painel["tabs"]] == list(ABAS)
    assert painel["parameters"][0]["default"] == "past30days"
    assert len(painel["dashcards"]) == len(PERGUNTAS)
    assert all(c["parameter_mappings"][0]["parameter_id"] == "periodo" for c in painel["dashcards"])
    # Cada aba recebe pelo menos uma pergunta.
    assert {c["dashboard_tab_id"] for c in painel["dashcards"]} == {aba["id"] for aba in painel["tabs"]}


def test_reinstalar_atualiza_sem_duplicar():
    cliente = MetabaseFalso(configurado=True, ja_instalado=True)
    campos = metabase.aguardar_sincronizacao(cliente, 7)
    metabase.montar_dashboard(cliente, 7, campos, 3)

    assert ("POST", "/card") not in cliente.chamadas
    assert ("POST", "/dashboard") not in cliente.chamadas
    assert sum(1 for metodo, caminho in cliente.chamadas if metodo == "PUT" and caminho.startswith("/card/")) == len(PERGUNTAS)
    # As abas existentes mantêm o id.
    assert [aba["id"] for aba in cliente.dashboard["tabs"]] == [50, 51, 52, 53]


def test_perguntas_tem_filtro_de_periodo_na_view_certa():
    corpo = metabase.corpo_da_pergunta(PERGUNTAS[0], banco_id=7, campo_data=801, colecao_id=3)
    tag = corpo["dataset_query"]["native"]["template-tags"]["data"]
    assert tag["dimension"] == ["field", 801, None]
    assert "{{data}}" in corpo["dataset_query"]["native"]["query"]
    # O id da tag é estável entre execuções.
    assert tag["id"] == metabase.corpo_da_pergunta(PERGUNTAS[0], 7, 801, 3)["dataset_query"]["native"]["template-tags"]["data"]["id"]


def test_toda_pergunta_usa_uma_view_existente_e_o_filtro():
    for pergunta in PERGUNTAS:
        assert pergunta.view in VISOES, pergunta.nome
        assert pergunta.view in pergunta.sql and "{{data}}" in pergunta.sql, pergunta.nome
        assert pergunta.coluna + pergunta.largura <= 24, pergunta.nome
    assert len({pergunta.nome for pergunta in PERGUNTAS}) == len(PERGUNTAS)


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
