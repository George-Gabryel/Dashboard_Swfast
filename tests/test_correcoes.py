"""Testes das correções de robustez — sem bater na API.

Rodar: pytest -q
"""

from __future__ import annotations

import json
import sys
from datetime import date

import polars as pl
import pytest

import main as ponto_de_entrada
from swfast.cliente import ClienteApi, ErroDeApi
from swfast.extratores import ResultadoExtracao
from swfast.gravador import GravadorBruto, ManifestoExecucao
from swfast.periodo import Periodo, PeriodoInvalido

PERIODO = Periodo(inicio=date(2026, 10, 15), fim=date(2026, 10, 21))


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


# --- gravador -----------------------------------------------------------------

def test_gravacao_nao_deixa_temporario_e_grava_json_valido(tmp_path):
    resultado = ResultadoExtracao("vendas", "/common/vendas", PERIODO, registros=[{"ID": "1", "Produto": "Pão"}])
    destino = GravadorBruto(tmp_path).gravar(resultado)

    assert destino is not None and destino.exists()
    assert list(destino.parent.glob("*.tmp")) == []
    assert json.loads(destino.read_text(encoding="utf-8")) == [{"ID": "1", "Produto": "Pão"}]


def test_manifesto_tem_esquema_fixo_mesmo_sem_erros(tmp_path):
    manifesto = ManifestoExecucao(tmp_path)
    manifesto.registrar(ResultadoExtracao("vendas", "/common/vendas", PERIODO), arquivo=None)
    destino = manifesto.gravar()

    esquema = pl.read_parquet(destino).schema
    assert esquema["erro"] == pl.String
    assert esquema["arquivo"] == pl.String


def test_manifestos_seguidos_nao_se_sobrescrevem(tmp_path):
    for _ in range(3):
        manifesto = ManifestoExecucao(tmp_path)
        manifesto.registrar(ResultadoExtracao("vendas", "/common/vendas", PERIODO), arquivo=None)
        manifesto.gravar()
    assert len(list(tmp_path.glob("execucao_*.parquet"))) == 3


# --- main: códigos de saída -----------------------------------------------------

class ExtratorFalso:
    def __init__(self, nome: str) -> None:
        self.nome = nome
        self.ativo = True
        self.cliente = None


class PipelineFalso:
    """Substitui o pipeline real: devolve manifestos prontos, sem rede."""

    erro: str | None = None
    #: Quando definido, cada fonte grava estes registros como JSON bruto de verdade.
    registros: list[dict] | None = None

    def __init__(self, configuracao) -> None:
        self.configuracao = configuracao
        self.extratores = [ExtratorFalso("vendas"), ExtratorFalso("cancelamentos")]

    @property
    def fontes_ativas(self):
        return [extrator for extrator in self.extratores if extrator.ativo]

    def executar(self, periodo=None) -> ManifestoExecucao:
        manifesto = ManifestoExecucao(self.configuracao.diretorio_manifesto)
        gravador = GravadorBruto(self.configuracao.diretorio_raw)
        for extrator in self.fontes_ativas:
            resultado = ResultadoExtracao(
                extrator.nome, "/x", PERIODO, registros=self.registros or [], erro=self.erro
            )
            arquivo = gravador.gravar(resultado) if self.registros else None
            manifesto.registrar(resultado, arquivo=arquivo)
        return manifesto

    def executar_historico(self, inicio, fim) -> list[ManifestoExecucao]:
        return [self.executar(janela) for janela in Periodo.fatiar(inicio, fim)]

    def encerrar(self) -> None:
        pass


@pytest.fixture
def rodar(monkeypatch, tmp_path):
    monkeypatch.setenv("SWFAST_API_KEY", "chave-de-teste")
    monkeypatch.setenv("DIRETORIO_DADOS", str(tmp_path))

    def _rodar(*argumentos: str, erro: str | None = None, registros: list[dict] | None = None) -> int:
        pipeline = type("Pipeline", (PipelineFalso,), {"erro": erro, "registros": registros})
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


# --- main: extração + transformação no mesmo comando -----------------------------

def venda_completa() -> dict:
    """Uma venda com todos os campos da planilha, para a transformação sair sem alertas."""
    from swfast.esquemas import DADOS_VENDA, Tipo

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


def gravar_bruto(tmp_path, conteudo: str) -> None:
    pasta = tmp_path / "raw" / "vendas" / "dt=2026-10-23"
    pasta.mkdir(parents=True)
    (pasta / "vendas_20261017_20261023.json").write_text(conteudo, encoding="utf-8")


def test_extracao_ja_transforma_o_que_gravou(rodar, tmp_path):
    assert rodar("--fontes", "vendas", registros=[venda_completa()]) == 0
    assert list((tmp_path / "staging" / "dados_venda").glob("dt=*/*.parquet"))


def test_so_transformar_nao_precisa_da_api_key(rodar, tmp_path, monkeypatch):
    gravar_bruto(tmp_path, json.dumps([venda_completa()]))
    monkeypatch.delenv("SWFAST_API_KEY")
    monkeypatch.setattr("swfast.config.load_dotenv", lambda *_, **__: None)

    assert rodar("--so-transformar", "--todos") == 0
    tabela = pl.read_parquet(next((tmp_path / "staging" / "dados_venda").glob("dt=*/*.parquet")))
    assert tabela["id"].to_list() == ["V1"]


def test_so_transformar_sem_json_e_erro_de_uso(rodar):
    assert rodar("--so-transformar") == 2


def test_so_transformar_com_json_invalido_sai_com_falha(rodar, tmp_path):
    gravar_bruto(tmp_path, "{isto não é json")
    assert rodar("--so-transformar") == 1


@pytest.mark.parametrize("argumentos", [
    ("--todos",),
    ("--so-transformar", "--todos", "--arquivo", "x.json"),
    ("--so-transformar", "--fontes", "vendas"),
])
def test_combinacoes_invalidas_de_transformacao(rodar, argumentos):
    with pytest.raises(SystemExit) as saida:
        rodar(*argumentos)
    assert saida.value.code == 2
