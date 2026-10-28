"""Testes das correções de robustez — sem bater na API nem no banco.

Rodar: pytest -q
"""

from __future__ import annotations

import json
import sys
from datetime import date

import psycopg
import pytest

import main as ponto_de_entrada
from swfast.banco import ErroDeBanco
from swfast.cliente import ClienteApi, ErroDeApi
from swfast.config import Configuracao
from swfast.esquemas import DADOS_VENDA, Tipo
from swfast.extratores import ResultadoExtracao
from swfast.gravador import ManifestoExecucao
from swfast.periodo import Periodo, PeriodoInvalido
from swfast.pipeline import PipelineExtracao
from swfast.transformacao import RelatorioTabela

PERIODO = Periodo(inicio=date(2026, 10, 15), fim=date(2026, 10, 21))


def venda_completa() -> dict:
    """Uma venda com todos os campos da planilha, para a transformação sair sem alertas."""
    exemplo = {
        Tipo.TEXTO: "x",
        Tipo.INTEIRO: 1,
        Tipo.DECIMAL: 1.5,
        Tipo.DATA: "2026-10-17T00:00:00",
        Tipo.DATA_HORA: "2026-10-17T15:31:50",
    }
    registro = {coluna.origem: exemplo[coluna.tipo] for coluna in DADOS_VENDA.colunas if coluna.origem}
    registro.update(id="V1", produtos=[], pagamentos=[], descontos=[], acrescimos=[], fiscais=[])
    return registro


# --- cliente ------------------------------------------------------------------

class RespostaFalsa:
    def __init__(self, status: int, headers: dict | None = None, corpo: bytes = b"[]") -> None:
        self.status_code = status
        self.headers = headers or {}
        self.content = corpo

    def json(self):
        return json.loads(self.content)


def cliente_com_respostas(monkeypatch, respostas: list[RespostaFalsa]) -> tuple[ClienteApi, list[float]]:
    cliente = ClienteApi("teste", "https://api.exemplo", "chave", tentativas=3, espera_inicial=2.0)
    fila = iter(respostas)
    monkeypatch.setattr(cliente.sessao, "get", lambda *_, **__: next(fila))
    esperas: list[float] = []
    monkeypatch.setattr("swfast.cliente.time.sleep", esperas.append)
    return cliente, esperas


def test_status_nao_tratado_vira_erro_de_api(monkeypatch):
    cliente, _ = cliente_com_respostas(monkeypatch, [RespostaFalsa(422)])
    with pytest.raises(ErroDeApi, match="422"):
        cliente.get("/common/vendas")


def test_429_respeita_retry_after_com_teto(monkeypatch):
    cliente, esperas = cliente_com_respostas(monkeypatch, [
        RespostaFalsa(429, {"Retry-After": "10"}),
        RespostaFalsa(429, {"Retry-After": "3600"}),
        RespostaFalsa(200),
    ])
    assert cliente.get("/common/vendas") == []
    assert esperas == [10.0, 60.0]


# --- periodo ------------------------------------------------------------------

def test_de_texto_mostra_o_erro_de_limite_e_nao_o_de_formato():
    with pytest.raises(PeriodoInvalido, match="máximo"):
        Periodo.de_texto("2026-10-01", "2026-10-20")


def test_de_texto_com_formato_errado_cita_a_data():
    with pytest.raises(PeriodoInvalido, match="15/10/2026"):
        Periodo.de_texto("15/10/2026", "2026-10-21")


def test_fatiar_recusa_janela_sem_dias():
    with pytest.raises(PeriodoInvalido, match="entre 1 e"):
        Periodo.fatiar(date(2026, 10, 1), date(2026, 10, 20), 0)


# --- manifesto ----------------------------------------------------------------

def ler_manifesto(destino) -> dict:
    return json.loads(destino.read_text(encoding="utf-8"))


def test_manifesto_e_um_json_legivel_com_as_tres_etapas(tmp_path):
    manifesto = ManifestoExecucao(tmp_path, PERIODO)
    resultado = ResultadoExtracao("vendas", "/common/vendas", PERIODO, registros=[{"id": "V1"}])
    manifesto.registrar(
        resultado,
        relatorios=[RelatorioTabela("dados_venda", linhas=1)],
        carregadas={"dados_venda": 1},
        duracao_transformacao=0.1,
        duracao_carga=0.2,
    )
    destino = manifesto.gravar()

    assert destino.suffix == ".json"
    assert list(tmp_path.glob("*.tmp")) == []
    dados = ler_manifesto(destino)
    assert dados["sucesso"] is True
    assert dados["periodo"] == {"inicio": "2026-10-15", "fim": "2026-10-21"}
    fonte = dados["fontes"][0]
    assert fonte["extracao"]["registros"] == 1
    assert fonte["transformacao"] == {"duracao_segundos": 0.1}
    assert fonte["carga"] == {"duracao_segundos": 0.2}
    assert fonte["tabelas"]["dados_venda"]["transformadas"] == 1
    assert fonte["tabelas"]["dados_venda"]["carregadas"] == 1


def test_manifesto_diz_em_qual_etapa_a_fonte_falhou(tmp_path):
    manifesto = ManifestoExecucao(tmp_path)
    manifesto.registrar(ResultadoExtracao("vendas", "/common/vendas", PERIODO, erro="503"))
    manifesto.registrar(ResultadoExtracao("cancelamentos", "/x", PERIODO), erro="carga: sem conexão")
    fontes = ler_manifesto(manifesto.gravar())["fontes"]

    assert [fonte["erro"] for fonte in fontes] == ["extracao: 503", "carga: sem conexão"]
    assert fontes[0]["transformacao"] is None and fontes[0]["carga"] is None
    assert len(manifesto.falhas) == 2


def test_manifestos_seguidos_nao_se_sobrescrevem(tmp_path):
    for _ in range(3):
        manifesto = ManifestoExecucao(tmp_path)
        manifesto.registrar(ResultadoExtracao("vendas", "/common/vendas", PERIODO))
        manifesto.gravar()
    assert len(list(tmp_path.glob("execucao_*.json"))) == 3


# --- pipeline: extração -> transformação -> carga -------------------------------

class ClienteFalso:
    def fechar(self) -> None:
        pass


class ExtratorComDados:
    def __init__(self, nome: str, registros: list[dict]) -> None:
        self.nome = nome
        self.ativo = True
        self.cliente = ClienteFalso()
        self.registros = registros

    def extrair(self, periodo) -> ResultadoExtracao:
        return ResultadoExtracao(self.nome, "/x", periodo, registros=self.registros)


class BancoFalso:
    """Guarda o que receberia; `falhar_em` simula erro de carga numa fonte."""

    def __init__(self, falhar_em: str | None = None) -> None:
        self.falhar_em = falhar_em
        self.carregado: dict[str, dict] = {}

    def conectar(self):
        return self

    def criar_tabelas(self) -> None:
        pass

    def carregar(self, fonte, tabelas):
        if fonte == self.falhar_em:
            raise psycopg.errors.UniqueViolation("chave duplicada")
        self.carregado[fonte] = tabelas
        return {nome: tabela.height for nome, tabela in tabelas.items()}

    def fechar(self) -> None:
        pass


def rodar_pipeline(tmp_path, banco: BancoFalso) -> ManifestoExecucao:
    configuracao = Configuracao(
        url_base="https://api.exemplo", api_key="chave",
        postgres_banco="b", postgres_usuario="u", postgres_senha="s",
        diretorio_manifesto=tmp_path / "_manifesto", diretorio_logs=tmp_path / "_logs",
        intervalo_entre_chamadas=0,
    )
    extratores = [ExtratorComDados("vendas", [venda_completa()]), ExtratorComDados("cancelamentos", [])]
    return PipelineExtracao(configuracao, extratores, banco).executar(PERIODO)


def test_pipeline_transforma_e_carrega_cada_fonte(tmp_path):
    banco = BancoFalso()
    manifesto = rodar_pipeline(tmp_path, banco)

    assert banco.carregado["vendas"]["dados_venda"]["id"].to_list() == ["V1"]
    assert not manifesto.falhas
    assert manifesto.linhas[0]["tabelas"]["dados_venda"]["carregadas"] == 1
    assert list((tmp_path / "_manifesto").glob("execucao_*.json"))


def test_falha_na_carga_de_uma_fonte_nao_impede_a_outra(tmp_path):
    banco = BancoFalso(falhar_em="vendas")
    manifesto = rodar_pipeline(tmp_path, banco)

    assert manifesto.linhas[0]["erro"].startswith("carga: ")
    assert manifesto.linhas[0]["tabelas"]["dados_venda"]["transformadas"] == 1
    assert manifesto.linhas[1]["sucesso"] is True
    assert "cancelamentos" in banco.carregado


# --- main: códigos de saída -----------------------------------------------------

class ExtratorFalso:
    def __init__(self, nome: str) -> None:
        self.nome = nome
        self.ativo = True
        self.cliente = None


class PipelineFalso:
    """Substitui o pipeline real: devolve manifestos prontos, sem rede nem banco."""

    erro: str | None = None
    alerta: bool = False
    erro_de_banco: bool = False

    def __init__(self, configuracao) -> None:
        self.configuracao = configuracao
        self.extratores = [ExtratorFalso("vendas"), ExtratorFalso("cancelamentos")]

    @property
    def fontes_ativas(self):
        return [extrator for extrator in self.extratores if extrator.ativo]

    def executar(self, periodo=None) -> ManifestoExecucao:
        if self.erro_de_banco:
            raise ErroDeBanco("Postgres inacessível em localhost:5432/swfast")
        manifesto = ManifestoExecucao(self.configuracao.diretorio_manifesto)
        relatorio = RelatorioTabela("dados_venda", campos_desconhecidos=["novo"] if self.alerta else [])
        for extrator in self.fontes_ativas:
            resultado = ResultadoExtracao(extrator.nome, "/x", PERIODO, erro=self.erro)
            manifesto.registrar(resultado, relatorios=[relatorio])
        return manifesto

    def executar_historico(self, inicio, fim) -> list[ManifestoExecucao]:
        return [self.executar(janela) for janela in Periodo.fatiar(inicio, fim)]

    def encerrar(self) -> None:
        pass


@pytest.fixture
def rodar(monkeypatch, tmp_path):
    variaveis = {
        "SWFAST_API_KEY": "chave-de-teste",
        "POSTGRES_DB": "swfast",
        "POSTGRES_USER": "swfast",
        "POSTGRES_PASSWORD": "senha",
        "DIRETORIO_DADOS": str(tmp_path),
    }
    for nome, valor in variaveis.items():
        monkeypatch.setenv(nome, valor)

    def _rodar(*argumentos: str, **comportamento) -> int:
        pipeline = type("Pipeline", (PipelineFalso,), comportamento)
        monkeypatch.setattr(ponto_de_entrada, "PipelineExtracao", pipeline)
        monkeypatch.setattr(sys, "argv", ["main.py", *argumentos])
        return ponto_de_entrada.main()

    return _rodar


def test_saida_zero_quando_tudo_foi_coletado(rodar):
    assert rodar() == 0


def test_saida_um_quando_alguma_fonte_falha(rodar):
    assert rodar(erro="[v5] 503 em /common/vendas") == 1


def test_saida_um_quando_falha_na_carga_historica(rodar):
    assert rodar("--historico", "2026-09-01", "2026-09-30", erro="falhou") == 1


def test_saida_um_quando_ha_alerta_de_dados(rodar):
    assert rodar(alerta=True) == 1


def test_saida_um_quando_o_banco_esta_inacessivel(rodar):
    assert rodar(erro_de_banco=True) == 1


def test_saida_dois_sem_as_variaveis_do_postgres(rodar, monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD")
    monkeypatch.setattr("swfast.config.load_dotenv", lambda *_, **__: None)
    assert rodar() == 2


def test_saida_dois_quando_fontes_nao_ativam_nada(rodar):
    assert rodar("--fontes", "venda") == 2


def test_saida_dois_com_periodo_acima_do_limite(rodar):
    assert rodar("--inicio", "2026-10-01", "--fim", "2026-10-20") == 2


def test_historico_nao_combina_com_inicio_e_fim(rodar):
    with pytest.raises(SystemExit) as saida:
        rodar("--historico", "2026-09-01", "2026-09-30", "--inicio", "2026-10-01", "--fim", "2026-10-02")
    assert saida.value.code == 2


def test_inicio_sem_fim_e_erro_de_uso(rodar):
    with pytest.raises(SystemExit) as saida:
        rodar("--inicio", "2026-10-01")
    assert saida.value.code == 2
