"""Instalação do dashboard no Metabase — de ponta a ponta, sem clique.

Uso (container de instalação, na rede do banco, com .env + .env.meta):
    docker compose run --rm instalar-dashboard

O que faz, em ordem, e pode rodar quantas vezes quiser sem duplicar nada:
1. No Postgres: cria o banco "metabase" (configurações do próprio Metabase)
   com o dono `metabase_app`, e o usuário `metabase_leitor`, só de leitura,
   com que o Metabase consulta o banco swfast.
2. Espera o Metabase responder.
3. Primeira vez: cria o administrador. Depois: entra com ele.
4. Liga o compartilhamento público e conecta o banco swfast.
5. Monta o dashboard "Swfast — Vendas": 4 abas, filtro de período, perguntas
   em SQL sobre as views `painel_*` (`visoes.py`). Se ele já existe, as
   perguntas são atualizadas no lugar e o link público continua o mesmo.
6. Grava o link público em data/dashboard_url.txt — é o que o atalho abre.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import requests
from psycopg import sql

from .config import Configuracao, ConfiguracaoInvalida, ConfiguracaoMetabase
from .visoes import VISOES

logger = logging.getLogger(__name__)

NOME_CONEXAO = "Swfast"
NOME_COLECAO = "Swfast"
NOME_DASHBOARD = "Swfast — Vendas"
ARQUIVO_URL = "dashboard_url.txt"

ABAS = ("Faturamento", "Produtos", "Pagamentos e descontos", "Cancelamentos")
FATURAMENTO, PRODUTOS, PAGAMENTOS, CANCELAMENTOS = ABAS

# Filtros do dashboard. Cada pergunta diz quais usa (ver `Filtro`).
QUANTIDADE, VALOR = "Quantidade", "Valor (R$)"
PARAMETROS: dict[str, dict[str, Any]] = {
    "periodo": {
        "id": "periodo", "name": "Período", "slug": "periodo",
        "type": "date/all-options", "default": "past30days",
    },
    # Exclusivo do "Faturamento por dia": o gráfico mostra no máximo os 7
    # últimos dias do intervalo escolhido (o Metabase não trava o seletor).
    "dias": {
        "id": "dias", "name": "Dias (máx. 7)", "slug": "dias",
        "type": "date/all-options", "default": "past7days",
    },
    # O "botão" Valor/Quantidade da aba Produtos: o Metabase não tem botão de
    # alternar, então é um filtro de lista com duas opções e uma sempre marcada.
    "metrica": {
        "id": "metrica", "name": "Exibir", "slug": "exibir",
        "type": "string/=", "sectionId": "string",
        "values_query_type": "list", "values_source_type": "static-list",
        "values_source_config": {"values": [VALOR, QUANTIDADE]},
        "default": [VALOR], "required": True, "isMultiSelect": False,
    },
}


class ErroDeMetabase(RuntimeError):
    """O Metabase recusou um pedido ou não ficou pronto a tempo."""


# ---------------------------------------------------------------------------
# As perguntas do dashboard
# ---------------------------------------------------------------------------
def moeda(*colunas: str) -> dict[str, Any]:
    """Ajuste de visualização: as colunas aparecem como R$."""
    return {
        "column_settings": {
            json.dumps(["name", coluna], ensure_ascii=False): {
                "number_style": "currency", "currency": "BRL", "currency_style": "symbol",
            }
            for coluna in colunas
        }
    }


def eixos(dimensao: str, *metricas: str, **extra: Any) -> dict[str, Any]:
    # Sem o agrupamento automático em "Outros" (barras e pizza). No gráfico
    # de linhas horizontais ("row") quem decide é a altura do card.
    return {
        "graph.dimensions": [dimensao], "graph.metrics": list(metricas),
        "graph.max_categories_enabled": False, **extra,
    }


def combo(dimensao: str, barra: str, linha: str, valores_nas_barras: bool = False) -> dict[str, Any]:
    """Barras + linha no mesmo gráfico, cada uma com seu eixo (imagens 3 e 4)."""
    return {
        **eixos(dimensao, barra, linha),
        "graph.y_axis.auto_split": True,
        "graph.show_values": valores_nas_barras,
        "series_settings": {
            barra: {"display": "bar", "show_series_values": valores_nas_barras},
            linha: {"display": "line", "show_series_values": False},
        },
    }


# Números e datas no padrão brasileiro em todo o Metabase (inclusive no link público).
FORMATACAO_BRASILEIRA = {
    "type/Number": {"number_separators": ",."},
    "type/Currency": {"currency": "BRL", "currency_style": "symbol"},
    "type/Temporal": {"date_style": "D/M/YYYY", "date_abbreviate": False, "time_style": "HH:mm"},
}


@dataclass(frozen=True)
class Filtro:
    """Liga uma variável `{{tag}}` do SQL a um filtro do dashboard.

    Com `view`, é um filtro de data ("field filter") sobre a coluna `data`
    dessa view — e a view não pode ter apelido no SQL, porque o Metabase
    escreve o nome completo da tabela. Sem `view`, é uma variável de texto.
    """

    tag: str
    parametro: str
    view: str | None = None


def periodo(view: str) -> Filtro:
    return Filtro("data", "periodo", view)


DIAS = Filtro("dias", "dias", "painel_calendario")
METRICA = Filtro("metrica", "metrica")


@dataclass(frozen=True)
class Posicao:
    """Onde um card fica: aba e retângulo na grade de 24 colunas."""

    aba: str
    linha: int
    coluna: int
    largura: int
    altura: int


@dataclass(frozen=True)
class Pergunta:
    """Uma pergunta (card): o SQL, o gráfico, os filtros e onde aparece.

    Uma pergunta pode aparecer em mais de uma aba (ex.: Desconto); é um
    card só no Metabase, com um cartão no dashboard para cada posição.
    """

    nome: str
    sql: str
    display: str
    posicoes: tuple[Posicao, ...]
    filtros: tuple[Filtro, ...]
    ajustes: dict[str, Any] = field(default_factory=dict)


def _p(nome, consulta, display, posicoes, filtros, ajustes=None) -> Pergunta:
    if isinstance(posicoes, Posicao):
        posicoes = (posicoes,)
    return Pergunta(nome, " ".join(consulta.split()), display, tuple(posicoes), tuple(filtros), ajustes or {})


def kpi_comparado(nome: str, expressao: str, view: str, posicoes, negativo_e_bom: bool = False,
                  em_reais: bool = True) -> Pergunta:
    """Bloco com o valor do período e a variação contra o período anterior.

    O filtro "Período" age no calendário: daí sai o início e o fim escolhidos
    (até em "Últimos 30 dias" ou "Este mês"). O período anterior termina na
    véspera do início e:
    - se o escolhido são meses cheios (ex.: "Este mês" = 1 a 31/10), volta o
      mesmo número de meses: outubro compara com setembro inteiro;
    - senão, volta o mesmo número de dias: últimos 7 dias contra os 7
      anteriores, esta semana contra a semana passada.
    O SQL devolve duas linhas (anterior e atual) e o gráfico "Tendência" do
    Metabase mostra o atual e o % de diferença.
    """
    consulta = f"""
        WITH escolhido AS (
            SELECT min(data) AS inicio, max(data) AS fim FROM painel_calendario WHERE {{{{data}}}}
        ),
        medida AS (
            SELECT inicio, fim,
                   inicio = date_trunc('month', inicio)::date
                       AND fim = (date_trunc('month', fim) + interval '1 month - 1 day')::date AS meses_cheios,
                   ((extract(year FROM fim) - extract(year FROM inicio)) * 12
                       + extract(month FROM fim) - extract(month FROM inicio) + 1)::int AS meses
            FROM escolhido
        ),
        periodos AS (
            SELECT CASE WHEN meses_cheios THEN (inicio - make_interval(months => meses))::date
                        ELSE inicio - (fim - inicio + 1) END AS inicio,
                   inicio - 1 AS fim
            FROM medida
            UNION ALL
            SELECT inicio, fim FROM medida
        )
        SELECT periodos.inicio AS "Período",
               (SELECT {expressao} FROM {view} WHERE {view}.data BETWEEN periodos.inicio AND periodos.fim) AS "{nome}"
        FROM periodos
        ORDER BY 1
    """
    ajustes = {
        "scalar.field": nome,
        "scalar.comparisons": [{"id": "anterior", "type": "previousValue"}],
        # Desconto subindo é ruim: inverte as cores (subida em vermelho).
        "scalar.switch_positive_negative": negativo_e_bom,
        **(moeda(nome) if em_reais else {}),
    }
    return _p(nome, consulta, "smartscalar", posicoes, (Filtro("data", "periodo", "painel_calendario"),), ajustes)


# A medida escolhida no filtro "Exibir".
MEDIDA_ESCOLHIDA = f"CASE WHEN {{{{metrica}}}} = '{QUANTIDADE}' THEN sum(qtd_produtos) ELSE sum(valor_produto) END"


def pizza_top5(nome: str, coluna: str, rotulo: str, posicao: Posicao) -> Pergunta:
    """Os 5 maiores pela medida do "Exibir" e o resto somado em OUTROS.

    Legenda com os nomes, % dentro das fatias e total no centro. O Metabase
    não mostra uma segunda medida no tooltip da pizza: a outra (valor ou
    quantidade) aparece trocando o "Exibir".
    """
    consulta = f"""
        WITH medidas AS (
            SELECT coalesce({coluna}, 'Sem {rotulo.lower()}') AS nome, {MEDIDA_ESCOLHIDA} AS total
            FROM painel_produtos WHERE {{{{data}}}} GROUP BY 1
        ),
        ranking AS (SELECT nome, total, row_number() OVER (ORDER BY total DESC) AS posicao FROM medidas)
        SELECT CASE WHEN posicao <= 5 THEN nome ELSE 'OUTROS' END AS "{rotulo}", sum(total) AS "Total"
        FROM ranking GROUP BY 1 ORDER BY min(posicao)
    """
    ajustes = {
        "pie.dimension": rotulo, "pie.metric": "Total",
        "pie.show_legend": True, "pie.show_total": True,
        "pie.percent_visibility": "inside", "pie.show_labels": False,
        # Sem agrupar fatias pequenas: OUTROS já vem do SQL. O Metabase ordena
        # as fatias por tamanho, então OUTROS fica onde o seu valor o põe.
        "pie.slice_threshold": 0,
    }
    return _p(nome, consulta, "pie", posicao, (periodo(PRODUTOS_V), METRICA), ajustes)
HORAS = "generate_series(0, 23) AS horas(hora)"
DIAS_DA_SEMANA = (
    "(VALUES (1, 'Segunda'), (2, 'Terça'), (3, 'Quarta'), (4, 'Quinta'), "
    "(5, 'Sexta'), (6, 'Sábado'), (7, 'Domingo')) AS semana(numero, nome)"
)
VENDAS, PRODUTOS_V, PAGAMENTOS_V = "painel_vendas", "painel_produtos", "painel_pagamentos"
DESCONTOS_V, ACRESCIMOS_V, CANCELAMENTOS_V = "painel_descontos", "painel_acrescimos", "painel_cancelamentos"


def _pos(aba, linha, coluna, largura, altura) -> Posicao:
    return Posicao(aba, linha, coluna, largura, altura)


PERGUNTAS: tuple[Pergunta, ...] = (
    # -- Faturamento ----------------------------------------------------------
    kpi_comparado("Faturamento bruto", "sum(valor_bruto)", VENDAS, _pos(FATURAMENTO, 0, 0, 6, 4)),
    kpi_comparado("Faturamento líquido", "sum(valor_liquido)", VENDAS, _pos(FATURAMENTO, 0, 6, 6, 4)),
    kpi_comparado(
        "Desconto", "coalesce(sum(valor_desconto_total), 0)", VENDAS,
        (_pos(FATURAMENTO, 0, 12, 6, 4), _pos(PAGAMENTOS, 0, 0, 12, 4)),
        negativo_e_bom=True,
    ),
    kpi_comparado(
        "Ticket médio", "sum(valor_liquido) / nullif(sum(qtd_pessoas), 0)", VENDAS, _pos(FATURAMENTO, 0, 18, 6, 4),
    ),
    _p("Vendas", 'SELECT count(*) AS "Vendas" FROM painel_vendas WHERE {{data}}',
       "scalar", _pos(FATURAMENTO, 4, 0, 8, 3), (periodo(VENDAS),)),
    _p("Pessoas atendidas", 'SELECT sum(qtd_pessoas) AS "Pessoas" FROM painel_vendas WHERE {{data}}',
       "scalar", _pos(FATURAMENTO, 4, 8, 8, 3), (periodo(VENDAS),)),
    _p("Taxa de serviço",
       'SELECT sum(valor_taxa_servicos) AS "Taxa de serviço" FROM painel_vendas WHERE {{data}}',
       "scalar", _pos(FATURAMENTO, 4, 16, 8, 3), (periodo(VENDAS),), moeda("Taxa de serviço")),
    # Só as horas que tiveram venda; o eixo pula as outras (ex.: 0, 13, 14...).
    _p("Faturamento por hora",
       'SELECT hora AS "Hora", sum(valor_liquido) AS "Faturamento", count(*) AS "Vendas" '
       "FROM painel_vendas WHERE {{data}} GROUP BY 1 ORDER BY 1",
       "combo", _pos(FATURAMENTO, 7, 0, 24, 8), (periodo(VENDAS),),
       {**combo("Hora", "Faturamento", "Vendas"), "graph.x_axis.scale": "ordinal", **moeda("Faturamento")}),
    _p("Faturamento por dia da semana",
       f'SELECT semana.nome AS "Dia da semana", coalesce(sum(valor_liquido), 0) AS "Faturamento" '
       f"FROM {DIAS_DA_SEMANA} "
       "LEFT JOIN painel_vendas ON painel_vendas.dia_semana_numero = semana.numero AND {{data}} "
       "GROUP BY semana.numero, semana.nome ORDER BY semana.numero",
       "line", _pos(FATURAMENTO, 15, 0, 12, 7), (periodo(VENDAS),),
       {**eixos("Dia da semana", "Faturamento"), **moeda("Faturamento")}),
    _p("Faturamento por dia (máx. 7 dias)",
       'SELECT painel_calendario.data AS "Dia", coalesce(sum(valor_liquido), 0) AS "Faturamento" '
       "FROM painel_calendario LEFT JOIN painel_vendas ON painel_vendas.data = painel_calendario.data "
       "WHERE {{dias}} AND painel_calendario.data <= current_date "
       "AND painel_calendario.data > (SELECT least(max(data), current_date) FROM painel_calendario WHERE {{dias}}) - 7 "
       "GROUP BY 1 ORDER BY 1",
       "line", _pos(FATURAMENTO, 15, 12, 12, 7), (DIAS,),
       {**eixos("Dia", "Faturamento"), **moeda("Faturamento")}),
    _p("Faturamento por operador de caixa",
       'SELECT operador AS "Operador", sum(valor_liquido) AS "Faturamento", count(*) AS "Vendas" '
       "FROM painel_vendas WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "bar", _pos(FATURAMENTO, 22, 0, 12, 7), (periodo(VENDAS),),
       {**eixos("Operador", "Faturamento"), **moeda("Faturamento")}),
    _p("Vendas por atendente",
       'SELECT coalesce(atendente, \'Sem atendente\') AS "Atendente", sum(valor_produto) AS "Valor" '
       "FROM painel_produtos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "row", _pos(FATURAMENTO, 22, 12, 12, 7), (periodo(PRODUTOS_V),),
       {**eixos("Atendente", "Valor"), **moeda("Valor")}),
    # -- Produtos (segue o filtro "Exibir: Valor / Quantidade") ----------------
    kpi_comparado("Valor dos produtos vendidos", "sum(valor_produto)", PRODUTOS_V, _pos(PRODUTOS, 0, 0, 24, 4)),
    _p("Top 10 produtos",
       f'SELECT produto AS "Produto", {MEDIDA_ESCOLHIDA} AS "Total" '
       "FROM painel_produtos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
       "row", _pos(PRODUTOS, 4, 0, 24, 9), (periodo(PRODUTOS_V), METRICA),
       eixos("Produto", "Total")),
    pizza_top5("Top 5 grupos", "grupo", "Grupo", _pos(PRODUTOS, 13, 0, 12, 9)),
    pizza_top5("Top 5 subgrupos", "sub_grupo", "Subgrupo", _pos(PRODUTOS, 13, 12, 12, 9)),
    # -- Pagamentos e descontos (o bloco Desconto vem da aba Faturamento) -------
    _p("Total de acréscimos",
       'SELECT coalesce(sum(valor_taxa), 0) AS "Acréscimos" FROM painel_acrescimos WHERE {{data}}',
       "scalar", _pos(PAGAMENTOS, 0, 12, 12, 4), (periodo(ACRESCIMOS_V),), moeda("Acréscimos")),
    _p("Faturamento por forma de pagamento",
       'SELECT forma_pagamento AS "Forma de pagamento", sum(valor_pagamento) AS "Faturamento", '
       'count(DISTINCT id_venda) AS "Vendas" FROM painel_pagamentos WHERE {{data}} '
       "GROUP BY 1 ORDER BY 2 DESC LIMIT 5",
       "combo", _pos(PAGAMENTOS, 4, 0, 16, 8), (periodo(PAGAMENTOS_V),),
       {**combo("Forma de pagamento", "Faturamento", "Vendas", valores_nas_barras=True), **moeda("Faturamento")}),
    _p("Pagamentos por tipo",
       'SELECT tipo_pagamento AS "Tipo", sum(valor_pagamento) AS "Valor" '
       "FROM painel_pagamentos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "pie", _pos(PAGAMENTOS, 4, 16, 8, 8), (periodo(PAGAMENTOS_V),),
       {"pie.dimension": "Tipo", "pie.metric": "Valor", **moeda("Valor")}),
    _p("Desconto por tipo",
       'SELECT tipo AS "Tipo", valor AS "Valor" FROM ('
       "SELECT 1 AS ordem, 'Convênios' AS tipo, coalesce(sum(valor_descontos), 0) AS valor FROM painel_vendas WHERE {{data}} "
       "UNION ALL SELECT 2, 'Combos', coalesce(sum(valor_combos), 0) FROM painel_vendas WHERE {{data}} "
       "UNION ALL SELECT 3, 'Cortesias', coalesce(sum(valor_cortesias), 0) FROM painel_vendas WHERE {{data}}"
       ") AS tipos ORDER BY ordem",
       "bar", _pos(PAGAMENTOS, 12, 0, 12, 7), (periodo(VENDAS),),
       {**eixos("Tipo", "Valor"), "graph.show_values": True, **moeda("Valor")}),
    _p("Acréscimos por tipo",
       'SELECT tipo_taxa AS "Tipo", sum(valor_taxa) AS "Valor" '
       "FROM painel_acrescimos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "bar", _pos(PAGAMENTOS, 12, 12, 12, 7), (periodo(ACRESCIMOS_V),),
       {**eixos("Tipo", "Valor"), **moeda("Valor")}),
    _p("Descontos por motivo",
       'SELECT desconto AS "Desconto", motivo AS "Motivo", '
       'count(*) AS "Quantidade", sum(valor_desconto) AS "Valor" '
       "FROM painel_descontos WHERE {{data}} GROUP BY 1, 2 ORDER BY 4 DESC",
       "table", _pos(PAGAMENTOS, 19, 0, 24, 7), (periodo(DESCONTOS_V),), moeda("Valor")),
    # -- Cancelamentos ------------------------------------------------------------
    _p("Valor cancelado",
       'SELECT coalesce(sum(valor_produto), 0) AS "Valor cancelado" FROM painel_cancelamentos WHERE {{data}}',
       "scalar", _pos(CANCELAMENTOS, 0, 0, 12, 3), (periodo(CANCELAMENTOS_V),), moeda("Valor cancelado")),
    _p("Itens cancelados",
       'SELECT coalesce(sum(qtd_produtos), 0) AS "Itens cancelados" FROM painel_cancelamentos WHERE {{data}}',
       "scalar", _pos(CANCELAMENTOS, 0, 12, 12, 3), (periodo(CANCELAMENTOS_V),)),
    _p("Cancelamentos por hora",
       f'SELECT horas.hora AS "Hora", sum(valor_produto) AS "Valor cancelado" FROM {HORAS} '
       "LEFT JOIN painel_cancelamentos ON painel_cancelamentos.hora = horas.hora AND {{data}} "
       "GROUP BY 1 ORDER BY 1",
       "line", _pos(CANCELAMENTOS, 3, 0, 24, 7), (periodo(CANCELAMENTOS_V),),
       # Horas sem cancelamento ficam vazias e a linha liga os pontos (imagem 5).
       {**eixos("Hora", "Valor cancelado"), "graph.x_axis.scale": "ordinal",
        "series_settings": {"Valor cancelado": {"line.missing": "interpolate", "line.marker_enabled": True}},
        **moeda("Valor cancelado")}),
    _p("Cancelamentos por motivo",
       'SELECT motivo_cancelamento AS "Motivo", sum(valor_produto) AS "Valor" '
       "FROM painel_cancelamentos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "bar", _pos(CANCELAMENTOS, 10, 0, 12, 8), (periodo(CANCELAMENTOS_V),),
       {**eixos("Motivo", "Valor"), **moeda("Valor")}),
    _p("Cancelamentos por operador",
       'SELECT operador_cancelamento AS "Operador", sum(valor_produto) AS "Valor" '
       "FROM painel_cancelamentos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "pie", _pos(CANCELAMENTOS, 10, 12, 12, 8), (periodo(CANCELAMENTOS_V),),
       {"pie.dimension": "Operador", "pie.metric": "Valor", **moeda("Valor")}),
    _p("Últimos cancelamentos",
       'SELECT data_hora_cancelamento AS "Quando", produto AS "Produto", qtd_produtos AS "Qtd", '
       'valor_produto AS "Valor", motivo_cancelamento AS "Motivo", '
       'operador_cancelamento AS "Cancelado por", tipo_cancelamento AS "Tipo" '
       "FROM painel_cancelamentos WHERE {{data}} ORDER BY 1 DESC LIMIT 100",
       "table", _pos(CANCELAMENTOS, 18, 0, 24, 7), (periodo(CANCELAMENTOS_V),), moeda("Valor")),
)


# ---------------------------------------------------------------------------
# 1. Postgres
# ---------------------------------------------------------------------------
def preparar_banco(configuracao: Configuracao, mb: ConfiguracaoMetabase) -> None:
    """Banco interno do Metabase e usuário de leitura. Idempotente.

    Senhas são (re)aplicadas toda vez: trocar no .env e rodar de novo basta.
    """
    # CREATE DATABASE não roda dentro de transação: conexão em autocommit.
    with psycopg.connect(configuracao.conninfo, autocommit=True, connect_timeout=10) as conexao:
        for usuario, senha in ((mb.app_usuario, mb.app_senha), (mb.leitor_usuario, mb.leitor_senha)):
            existe = conexao.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (usuario,)).fetchone()
            comando = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if existe else "CREATE ROLE {} WITH LOGIN PASSWORD {}"
            conexao.execute(sql.SQL(comando).format(sql.Identifier(usuario), sql.Literal(senha)))

        if not conexao.execute("SELECT 1 FROM pg_database WHERE datname = 'metabase'").fetchone():
            conexao.execute(sql.SQL("CREATE DATABASE metabase OWNER {}").format(sql.Identifier(mb.app_usuario)))
            logger.info("Banco 'metabase' criado.")

        leitor = sql.Identifier(mb.leitor_usuario)
        for comando in (
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(configuracao.postgres_banco), leitor),
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(leitor),
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(leitor),
            # Views e tabelas recriadas pelo ETL nascem já com a permissão.
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(
                sql.Identifier(configuracao.postgres_usuario), leitor
            ),
        ):
            conexao.execute(comando)
    logger.info("Postgres pronto: banco 'metabase' e usuário de leitura '%s'.", mb.leitor_usuario)


# ---------------------------------------------------------------------------
# 2-6. Metabase
# ---------------------------------------------------------------------------
class ClienteMetabase:
    """O mínimo da API REST do Metabase, com a sessão no cabeçalho."""

    def __init__(
        self, url: str, sessao: requests.Session | None = None, esperar: Callable[[float], None] = time.sleep
    ) -> None:
        self.url = url.rstrip("/")
        self.sessao = sessao or requests.Session()
        self.esperar = esperar

    def pedir(self, metodo: str, caminho: str, corpo: Any = None, **parametros: Any) -> Any:
        try:
            resposta = self.sessao.request(
                metodo, f"{self.url}/api{caminho}", json=corpo, params=parametros or None, timeout=60
            )
        except requests.RequestException as erro:
            raise ErroDeMetabase(f"Metabase inacessível em {self.url}: {erro}") from erro
        if resposta.status_code >= 400:
            raise ErroDeMetabase(f"{metodo} {caminho} -> {resposta.status_code}: {resposta.text[:500]}")
        return resposta.json() if resposta.content else None

    def get(self, caminho: str, **parametros: Any) -> Any:
        return self.pedir("GET", caminho, **parametros)

    def post(self, caminho: str, corpo: Any = None) -> Any:
        return self.pedir("POST", caminho, corpo)

    def put(self, caminho: str, corpo: Any = None) -> Any:
        return self.pedir("PUT", caminho, corpo)

    # -- prontidão e sessão ---------------------------------------------------
    def aguardar_ficar_pronto(self, limite_segundos: float = 600, intervalo: float = 5) -> None:
        """Na primeira subida o Metabase leva alguns minutos migrando o banco interno."""
        inicio = time.monotonic()
        while True:
            try:
                if (self.get("/health") or {}).get("status") == "ok":
                    return
            except ErroDeMetabase:
                pass
            if time.monotonic() - inicio > limite_segundos:
                raise ErroDeMetabase(f"O Metabase em {self.url} não ficou pronto em {limite_segundos:.0f}s.")
            self.esperar(intervalo)

    def entrar(self, mb: ConfiguracaoMetabase) -> None:
        """Primeira vez: configuração inicial com o administrador. Depois: login."""
        propriedades = self.get("/session/properties")
        if not propriedades.get("has-user-setup"):
            logger.info("Metabase novo: criando o administrador %s.", mb.admin_email)
            resposta = self.post("/setup", {
                "token": propriedades["setup-token"],
                "user": {
                    "email": mb.admin_email,
                    "password": mb.admin_senha,
                    "first_name": "Administrador",
                    "last_name": "Swfast",
                    "site_name": "Swfast",
                },
                "prefs": {"site_name": "Swfast", "site_locale": "pt_BR", "allow_tracking": False},
            })
        else:
            resposta = self.post("/session", {"username": mb.admin_email, "password": mb.admin_senha})
        self.sessao.headers["X-Metabase-Session"] = resposta["id"]


def garantir_conexao(cliente: ClienteMetabase, configuracao: Configuracao, mb: ConfiguracaoMetabase) -> int:
    """Conexão do Metabase com o banco swfast, pelo usuário de leitura."""
    detalhes = {
        "host": mb.banco_host,
        "port": mb.banco_porta,
        "dbname": configuracao.postgres_banco,
        "user": mb.leitor_usuario,
        "password": mb.leitor_senha,
        "ssl": False,
    }
    resposta = cliente.get("/database")
    bancos = resposta["data"] if isinstance(resposta, dict) else resposta
    existente = next((banco for banco in bancos if banco["name"] == NOME_CONEXAO), None)
    if existente:
        cliente.put(f"/database/{existente['id']}", {"details": detalhes})
        banco_id = existente["id"]
    else:
        banco_id = cliente.post("/database", {"engine": "postgres", "name": NOME_CONEXAO, "details": detalhes})["id"]
        logger.info("Banco swfast conectado ao Metabase.")
    cliente.post(f"/database/{banco_id}/sync_schema")
    return banco_id


def aguardar_sincronizacao(
    cliente: ClienteMetabase, banco_id: int, limite_segundos: float = 300, intervalo: float = 3
) -> dict[str, int]:
    """Espera o Metabase enxergar as views. Devolve o id do campo `data` de cada uma."""
    inicio = time.monotonic()
    while True:
        metadados = cliente.get(f"/database/{banco_id}/metadata", include_hidden="true")
        campos = {
            tabela["name"]: campo["id"]
            for tabela in metadados.get("tables", [])
            for campo in tabela.get("fields", [])
            if tabela["name"] in VISOES and campo["name"] == "data"
        }
        if set(campos) == set(VISOES):
            return campos
        if time.monotonic() - inicio > limite_segundos:
            faltam = ", ".join(sorted(set(VISOES) - set(campos)))
            raise ErroDeMetabase(f"O Metabase não sincronizou as views: {faltam}. O ETL já rodou?")
        cliente.esperar(intervalo)


def garantir_colecao(cliente: ClienteMetabase) -> int:
    for colecao in cliente.get("/collection"):
        if colecao.get("name") == NOME_COLECAO and not colecao.get("archived"):
            return colecao["id"]
    return cliente.post("/collection", {"name": NOME_COLECAO})["id"]


def itens_da_colecao(cliente: ClienteMetabase, colecao_id: int, modelo: str) -> dict[str, int]:
    resposta = cliente.get(f"/collection/{colecao_id}/items", models=modelo)
    itens = resposta["data"] if isinstance(resposta, dict) else resposta
    return {item["name"]: item["id"] for item in itens if item.get("model") == modelo}


def corpo_da_pergunta(pergunta: Pergunta, banco_id: int, campos: dict[str, int], colecao_id: int) -> dict[str, Any]:
    """`campos`: id no Metabase da coluna `data` de cada view (ver aguardar_sincronizacao)."""
    tags = {}
    for filtro in pergunta.filtros:
        parametro = PARAMETROS[filtro.parametro]
        tag = {
            # id estável: rodar de novo não troca a ligação com o filtro.
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"swfast/{pergunta.nome}/{filtro.tag}")),
            "name": filtro.tag,
            "display-name": parametro["name"],
        }
        if filtro.view is not None:
            tag |= {
                "type": "dimension",
                "dimension": ["field", campos[filtro.view], None],
                "widget-type": "date/all-options",
            }
        else:
            # Variável de texto com o mesmo padrão do filtro: a pergunta
            # aberta sozinha (fora do dashboard) também funciona.
            tag |= {"type": "text", "default": parametro["default"][0], "required": True}
        tags[filtro.tag] = tag
    return {
        "name": pergunta.nome,
        "collection_id": colecao_id,
        "display": pergunta.display,
        "visualization_settings": pergunta.ajustes,
        "dataset_query": {
            "database": banco_id,
            "type": "native",
            "native": {"query": pergunta.sql, "template-tags": tags},
        },
    }


def ligacoes_dos_filtros(pergunta: Pergunta, card_id: int) -> list[dict[str, Any]]:
    """Os `parameter_mappings` de um cartão: só os filtros que a pergunta usa."""
    return [
        {
            "parameter_id": filtro.parametro,
            "card_id": card_id,
            "target": (
                ["dimension", ["template-tag", filtro.tag]]
                if filtro.view is not None
                else ["variable", ["template-tag", filtro.tag]]
            ),
        }
        for filtro in pergunta.filtros
    ]


def montar_dashboard(cliente: ClienteMetabase, banco_id: int, campos: dict[str, int], colecao_id: int) -> int:
    """Cria ou atualiza perguntas e dashboard. Devolve o id do dashboard."""
    existentes = itens_da_colecao(cliente, colecao_id, "card")
    ids: dict[str, int] = {}
    for pergunta in PERGUNTAS:
        corpo = corpo_da_pergunta(pergunta, banco_id, campos, colecao_id)
        if pergunta.nome in existentes:
            ids[pergunta.nome] = cliente.put(f"/card/{existentes[pergunta.nome]}", corpo)["id"]
        else:
            ids[pergunta.nome] = cliente.post("/card", corpo)["id"]

    # Perguntas que saíram do código (ex.: "Itens em promoção") vão para a lixeira.
    for nome, card_id in existentes.items():
        if nome not in ids:
            cliente.put(f"/card/{card_id}", {"archived": True})
            logger.info("Pergunta '%s' arquivada (não está mais no dashboard).", nome)

    dashboards = itens_da_colecao(cliente, colecao_id, "dashboard")
    if NOME_DASHBOARD in dashboards:
        dashboard_id = dashboards[NOME_DASHBOARD]
    else:
        dashboard_id = cliente.post("/dashboard", {"name": NOME_DASHBOARD, "collection_id": colecao_id})["id"]
        logger.info("Dashboard '%s' criado.", NOME_DASHBOARD)

    # Abas que já existem mantêm o id; as novas vão com id negativo.
    atuais = {aba["name"]: aba["id"] for aba in cliente.get(f"/dashboard/{dashboard_id}").get("tabs", [])}
    abas = [{"id": atuais.get(nome, -indice), "name": nome} for indice, nome in enumerate(ABAS, start=1)]
    id_da_aba = {aba["name"]: aba["id"] for aba in abas}

    cartoes = []
    for pergunta in PERGUNTAS:
        for posicao in pergunta.posicoes:
            cartoes.append({
                "id": -(len(cartoes) + 1),  # sempre novos: o dashboard é redesenhado inteiro
                "card_id": ids[pergunta.nome],
                "dashboard_tab_id": id_da_aba[posicao.aba],
                "row": posicao.linha,
                "col": posicao.coluna,
                "size_x": posicao.largura,
                "size_y": posicao.altura,
                "parameter_mappings": ligacoes_dos_filtros(pergunta, ids[pergunta.nome]),
                "visualization_settings": {},
            })

    usados = {filtro.parametro for pergunta in PERGUNTAS for filtro in pergunta.filtros}
    cliente.put(f"/dashboard/{dashboard_id}", {
        "name": NOME_DASHBOARD,
        "parameters": [parametro for chave, parametro in PARAMETROS.items() if chave in usados],
        "tabs": abas,
        "dashcards": cartoes,
    })
    logger.info("Dashboard com %d perguntas (%d cartões) em %d abas.", len(PERGUNTAS), len(cartoes), len(ABAS))
    return dashboard_id


def link_publico(cliente: ClienteMetabase, dashboard_id: int, mb: ConfiguracaoMetabase) -> str:
    """Liga o compartilhamento público e devolve o link — o mesmo a cada execução."""
    cliente.put("/setting/enable-public-sharing", {"value": True})
    cliente.put("/setting/custom-formatting", {"value": FORMATACAO_BRASILEIRA})
    identificador = cliente.post(f"/dashboard/{dashboard_id}/public_link")["uuid"]
    return f"{mb.url_publica}/public/dashboard/{identificador}"


def instalar(
    configuracao: Configuracao, mb: ConfiguracaoMetabase, cliente: ClienteMetabase | None = None
) -> str:
    """Todos os passos, em ordem. Devolve o link público."""
    preparar_banco(configuracao, mb)

    cliente = cliente or ClienteMetabase(mb.url)
    logger.info("Esperando o Metabase em %s...", mb.url)
    cliente.aguardar_ficar_pronto()
    cliente.entrar(mb)

    banco_id = garantir_conexao(cliente, configuracao, mb)
    campos = aguardar_sincronizacao(cliente, banco_id)
    colecao_id = garantir_colecao(cliente)
    dashboard_id = montar_dashboard(cliente, banco_id, campos, colecao_id)
    url = link_publico(cliente, dashboard_id, mb)

    destino = Path(configuracao.diretorio_manifesto).parent / ARQUIVO_URL
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(url + "\n", encoding="utf-8")
    logger.info("Link público gravado em %s", destino)
    return url


def main(argv: list[str] | None = None) -> int:
    """Também lê o .env.meta do diretório atual, se existir (rodando fora do container)."""
    argumentos = sys.argv[1:] if argv is None else argv
    if argumentos != ["instalar"]:
        print("Uso: python -m swfast.metabase instalar", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        configuracao = Configuracao.a_partir_do_ambiente()
        mb = ConfiguracaoMetabase.a_partir_do_ambiente(Path(".env.meta") if Path(".env.meta").exists() else None)
    except ConfiguracaoInvalida as erro:
        print(f"Erro de configuração: {erro}", file=sys.stderr)
        return 2

    try:
        url = instalar(configuracao, mb)
    except (ErroDeMetabase, psycopg.Error) as erro:
        logger.error("Instalação do dashboard falhou: %s", erro)
        return 1
    print(f"\nDashboard pronto: {url}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
