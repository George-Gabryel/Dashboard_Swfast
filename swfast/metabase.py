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

ABAS = ("Visão geral", "Produtos", "Pagamentos e descontos", "Cancelamentos e equipe")
PARAMETRO_PERIODO = {
    "id": "periodo",
    "name": "Período",
    "slug": "periodo",
    "type": "date/all-options",
    "default": "past30days",
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
    # Sem o agrupamento automático em "Outros": um top 15 mostra os 15.
    return {
        "graph.dimensions": [dimensao], "graph.metrics": list(metricas),
        "graph.max_categories_enabled": False, **extra,
    }


# Números e datas no padrão brasileiro em todo o Metabase (inclusive no link público).
FORMATACAO_BRASILEIRA = {
    "type/Number": {"number_separators": ",."},
    "type/Currency": {"currency": "BRL", "currency_style": "symbol"},
    "type/Temporal": {"date_style": "D/M/YYYY", "date_abbreviate": False, "time_style": "HH:mm"},
}


@dataclass(frozen=True)
class Pergunta:
    """Uma pergunta (card) e onde ela fica no dashboard (grade de 24 colunas).

    `view` é de onde sai a coluna `data` que o filtro de período usa; o SQL
    tem `WHERE {{data}}` — sem período escolhido, o Metabase troca por 1=1.
    """

    nome: str
    aba: str
    view: str
    sql: str
    display: str
    linha: int
    coluna: int
    largura: int
    altura: int
    ajustes: dict[str, Any] = field(default_factory=dict)


def _p(nome, aba, view, consulta, display, posicao, ajustes=None) -> Pergunta:
    linha, coluna, largura, altura = posicao
    return Pergunta(nome, aba, view, " ".join(consulta.split()), display, linha, coluna, largura, altura, ajustes or {})


GERAL, PRODUTOS, PAGAMENTOS, CANCELAMENTOS = ABAS

PERGUNTAS: tuple[Pergunta, ...] = (
    # -- Visão geral --------------------------------------------------------
    _p("Faturamento", GERAL, "painel_vendas",
       'SELECT sum(valor_faturamento) AS "Faturamento" FROM painel_vendas WHERE {{data}}',
       "scalar", (0, 0, 8, 3), moeda("Faturamento")),
    _p("Vendas", GERAL, "painel_vendas",
       'SELECT count(*) AS "Vendas" FROM painel_vendas WHERE {{data}}',
       "scalar", (0, 8, 8, 3)),
    _p("Ticket médio", GERAL, "painel_vendas",
       'SELECT avg(valor_faturamento) AS "Ticket médio" FROM painel_vendas WHERE {{data}}',
       "scalar", (0, 16, 8, 3), moeda("Ticket médio")),
    _p("Pessoas atendidas", GERAL, "painel_vendas",
       'SELECT sum(qtd_pessoas) AS "Pessoas" FROM painel_vendas WHERE {{data}}',
       "scalar", (3, 0, 8, 3)),
    _p("Média por pessoa", GERAL, "painel_vendas",
       'SELECT sum(valor_faturamento) / nullif(sum(qtd_pessoas), 0) AS "Média por pessoa" '
       "FROM painel_vendas WHERE {{data}}",
       "scalar", (3, 8, 8, 3), moeda("Média por pessoa")),
    _p("Taxa de serviço", GERAL, "painel_vendas",
       'SELECT sum(valor_taxa_servicos) AS "Taxa de serviço" FROM painel_vendas WHERE {{data}}',
       "scalar", (3, 16, 8, 3), moeda("Taxa de serviço")),
    _p("Faturamento por dia", GERAL, "painel_vendas",
       'SELECT data AS "Dia", sum(valor_faturamento) AS "Faturamento", count(*) AS "Vendas" '
       "FROM painel_vendas WHERE {{data}} GROUP BY 1 ORDER BY 1",
       "line", (6, 0, 24, 6), {**eixos("Dia", "Faturamento"), **moeda("Faturamento")}),
    _p("Faturamento por hora do dia", GERAL, "painel_vendas",
       'SELECT hora AS "Hora", sum(valor_faturamento) AS "Faturamento" '
       "FROM painel_vendas WHERE {{data}} GROUP BY 1 ORDER BY 1",
       "bar", (12, 0, 12, 6), {**eixos("Hora", "Faturamento"), **moeda("Faturamento")}),
    _p("Faturamento por dia da semana", GERAL, "painel_vendas",
       'SELECT dia_semana AS "Dia da semana", sum(valor_faturamento) AS "Faturamento" '
       "FROM painel_vendas WHERE {{data}} GROUP BY dia_semana_numero, 1 ORDER BY dia_semana_numero",
       "bar", (12, 12, 12, 6), {**eixos("Dia da semana", "Faturamento"), **moeda("Faturamento")}),
    # -- Produtos -------------------------------------------------------------
    _p("Top 15 produtos por quantidade", PRODUTOS, "painel_produtos",
       'SELECT produto AS "Produto", sum(qtd_produtos) AS "Quantidade" '
       "FROM painel_produtos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC LIMIT 15",
       "row", (0, 0, 12, 8), eixos("Produto", "Quantidade")),
    _p("Top 15 produtos por valor", PRODUTOS, "painel_produtos",
       'SELECT produto AS "Produto", sum(valor_produto) AS "Valor" '
       "FROM painel_produtos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC LIMIT 15",
       "row", (0, 12, 12, 8), {**eixos("Produto", "Valor"), **moeda("Valor")}),
    _p("Vendas por grupo", PRODUTOS, "painel_produtos",
       'SELECT grupo AS "Grupo", sum(valor_produto) AS "Valor" '
       "FROM painel_produtos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "bar", (8, 0, 12, 7), {**eixos("Grupo", "Valor"), **moeda("Valor")}),
    _p("Vendas por subgrupo", PRODUTOS, "painel_produtos",
       'SELECT grupo AS "Grupo", sub_grupo AS "Subgrupo", sum(qtd_produtos) AS "Quantidade", '
       'sum(valor_produto) AS "Valor" FROM painel_produtos WHERE {{data}} GROUP BY 1, 2 ORDER BY 4 DESC',
       "table", (8, 12, 12, 7), moeda("Valor")),
    _p("Itens em promoção", PRODUTOS, "painel_produtos",
       'SELECT promocao AS "Promoção", produto AS "Produto", sum(qtd_produtos) AS "Quantidade", '
       'sum(valor_produto) AS "Valor" FROM painel_produtos WHERE {{data}} AND promocao IS NOT NULL '
       "GROUP BY 1, 2 ORDER BY 4 DESC",
       "table", (15, 0, 24, 6), moeda("Valor")),
    # -- Pagamentos e descontos -------------------------------------------------
    _p("Total de descontos", PAGAMENTOS, "painel_descontos",
       'SELECT coalesce(sum(valor_desconto), 0) AS "Descontos" FROM painel_descontos WHERE {{data}}',
       "scalar", (0, 0, 12, 3), moeda("Descontos")),
    _p("Total de acréscimos", PAGAMENTOS, "painel_acrescimos",
       'SELECT coalesce(sum(valor_taxa), 0) AS "Acréscimos" FROM painel_acrescimos WHERE {{data}}',
       "scalar", (0, 12, 12, 3), moeda("Acréscimos")),
    _p("Valor por forma de pagamento", PAGAMENTOS, "painel_pagamentos",
       'SELECT forma_pagamento AS "Forma de pagamento", sum(valor_pagamento) AS "Valor" '
       "FROM painel_pagamentos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "row", (3, 0, 12, 8), {**eixos("Forma de pagamento", "Valor"), **moeda("Valor")}),
    _p("Pagamentos por tipo", PAGAMENTOS, "painel_pagamentos",
       'SELECT tipo_pagamento AS "Tipo", sum(valor_pagamento) AS "Valor" '
       "FROM painel_pagamentos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "pie", (3, 12, 12, 8), {"pie.dimension": "Tipo", "pie.metric": "Valor", **moeda("Valor")}),
    _p("Descontos por motivo", PAGAMENTOS, "painel_descontos",
       'SELECT tipo_desconto AS "Tipo", desconto AS "Desconto", motivo AS "Motivo", '
       'count(*) AS "Quantidade", sum(valor_desconto) AS "Valor" '
       "FROM painel_descontos WHERE {{data}} GROUP BY 1, 2, 3 ORDER BY 5 DESC",
       "table", (11, 0, 12, 7), moeda("Valor")),
    _p("Acréscimos por tipo", PAGAMENTOS, "painel_acrescimos",
       'SELECT tipo_taxa AS "Tipo", sum(valor_taxa) AS "Valor" '
       "FROM painel_acrescimos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "bar", (11, 12, 12, 7), {**eixos("Tipo", "Valor"), **moeda("Valor")}),
    # -- Cancelamentos e equipe -------------------------------------------------
    _p("Valor cancelado", CANCELAMENTOS, "painel_cancelamentos",
       'SELECT coalesce(sum(valor_produto), 0) AS "Valor cancelado" FROM painel_cancelamentos WHERE {{data}}',
       "scalar", (0, 0, 12, 3), moeda("Valor cancelado")),
    _p("Itens cancelados", CANCELAMENTOS, "painel_cancelamentos",
       'SELECT coalesce(sum(qtd_produtos), 0) AS "Itens cancelados" FROM painel_cancelamentos WHERE {{data}}',
       "scalar", (0, 12, 12, 3)),
    _p("Cancelamentos por motivo", CANCELAMENTOS, "painel_cancelamentos",
       'SELECT motivo_cancelamento AS "Motivo", sum(valor_produto) AS "Valor" '
       "FROM painel_cancelamentos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "bar", (3, 0, 12, 7), {**eixos("Motivo", "Valor"), **moeda("Valor")}),
    _p("Cancelamentos por operador", CANCELAMENTOS, "painel_cancelamentos",
       'SELECT operador_cancelamento AS "Operador", sum(valor_produto) AS "Valor" '
       "FROM painel_cancelamentos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "bar", (3, 12, 12, 7), {**eixos("Operador", "Valor"), **moeda("Valor")}),
    _p("Últimos cancelamentos", CANCELAMENTOS, "painel_cancelamentos",
       'SELECT data_hora_cancelamento AS "Quando", produto AS "Produto", qtd_produtos AS "Qtd", '
       'valor_produto AS "Valor", motivo_cancelamento AS "Motivo", '
       'operador_cancelamento AS "Cancelado por", tipo_cancelamento AS "Tipo" '
       "FROM painel_cancelamentos WHERE {{data}} ORDER BY 1 DESC LIMIT 100",
       "table", (10, 0, 24, 6), moeda("Valor")),
    _p("Faturamento por operador de caixa", CANCELAMENTOS, "painel_vendas",
       'SELECT operador AS "Operador", sum(valor_faturamento) AS "Faturamento", count(*) AS "Vendas" '
       "FROM painel_vendas WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "bar", (16, 0, 12, 7), {**eixos("Operador", "Faturamento"), **moeda("Faturamento")}),
    _p("Vendas por atendente", CANCELAMENTOS, "painel_produtos",
       'SELECT coalesce(atendente, \'Sem atendente\') AS "Atendente", sum(valor_produto) AS "Valor" '
       "FROM painel_produtos WHERE {{data}} GROUP BY 1 ORDER BY 2 DESC",
       "row", (16, 12, 12, 7), {**eixos("Atendente", "Valor"), **moeda("Valor")}),
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


def corpo_da_pergunta(pergunta: Pergunta, banco_id: int, campo_data: int, colecao_id: int) -> dict[str, Any]:
    return {
        "name": pergunta.nome,
        "collection_id": colecao_id,
        "display": pergunta.display,
        "visualization_settings": pergunta.ajustes,
        "dataset_query": {
            "database": banco_id,
            "type": "native",
            "native": {
                "query": pergunta.sql,
                "template-tags": {
                    "data": {
                        # id estável: rodar de novo não troca a ligação com o filtro.
                        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"swfast/{pergunta.nome}")),
                        "name": "data",
                        "display-name": "Período",
                        "type": "dimension",
                        "dimension": ["field", campo_data, None],
                        "widget-type": "date/all-options",
                    }
                },
            },
        },
    }


def montar_dashboard(cliente: ClienteMetabase, banco_id: int, campos: dict[str, int], colecao_id: int) -> int:
    """Cria ou atualiza perguntas e dashboard. Devolve o id do dashboard."""
    existentes = itens_da_colecao(cliente, colecao_id, "card")
    ids: dict[str, int] = {}
    for pergunta in PERGUNTAS:
        corpo = corpo_da_pergunta(pergunta, banco_id, campos[pergunta.view], colecao_id)
        if pergunta.nome in existentes:
            ids[pergunta.nome] = cliente.put(f"/card/{existentes[pergunta.nome]}", corpo)["id"]
        else:
            ids[pergunta.nome] = cliente.post("/card", corpo)["id"]

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

    cartoes = [
        {
            "id": -indice,  # sempre novos: o dashboard é redesenhado inteiro a cada instalação
            "card_id": ids[pergunta.nome],
            "dashboard_tab_id": id_da_aba[pergunta.aba],
            "row": pergunta.linha,
            "col": pergunta.coluna,
            "size_x": pergunta.largura,
            "size_y": pergunta.altura,
            "parameter_mappings": [{
                "parameter_id": PARAMETRO_PERIODO["id"],
                "card_id": ids[pergunta.nome],
                "target": ["dimension", ["template-tag", "data"]],
            }],
            "visualization_settings": {},
        }
        for indice, pergunta in enumerate(PERGUNTAS, start=1)
    ]
    cliente.put(f"/dashboard/{dashboard_id}", {
        "name": NOME_DASHBOARD,
        "parameters": [PARAMETRO_PERIODO],
        "tabs": abas,
        "dashcards": cartoes,
    })
    logger.info("Dashboard com %d perguntas em %d abas.", len(PERGUNTAS), len(ABAS))
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
