"""As tabelas de destino, transcritas das planilhas de especificação.

common_vendas.xlsx               -> dados_venda, dados_produtos, dados_pagamento,
                                    dados_desconto, dados_acrescimo, dados_fiscal
common_vendas_cancelamentos.xlsx -> dados_venda_cancelamento, dados_produto_cancelamento

Cada aba da planilha virou um `EsquemaTabela`, e cada linha da aba virou uma
`Coluna`: o nome do campo no JSON, o nome da coluna no banco e o tipo de
destino. Mudou a planilha, muda aqui — e só aqui. O `Transformador` não
conhece nenhum campo pelo nome; ele lê estas listas.

Os nomes das colunas estão em snake_case (`redeLoja` vira `rede_loja`). O
PostgreSQL converte para minúsculas todo nome que não estiver entre aspas,
então `redeLoja` viraria `redeloja` no banco e no Metabase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Tipo(Enum):
    """Os tipos de destino que aparecem na coluna "Tratamento dos Dados"."""

    TEXTO = "texto"
    INTEIRO = "inteiro"
    DECIMAL = "decimal"      # "float" na planilha
    DATA = "data"            # YYYY-MM-DD
    DATA_HORA = "data_hora"  # YYYY-MM-DD HH:MM:SS


@dataclass(frozen=True)
class Coluna:
    """Uma linha da planilha.

    `origem` é a chave no JSON. Quando é None, a coluna não vem do JSON: é o
    `id_venda`, copiado do registro pai para as tabelas filhas.
    `substituir` troca valores inteiros depois da limpeza (ex.: NF-e -> BNF).
    """

    origem: str | None
    destino: str
    tipo: Tipo = Tipo.TEXTO
    substituir: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EsquemaTabela:
    """Uma aba da planilha: o que entra na tabela e de onde vem.

    `lista` diz de onde saem as linhas. None = uma linha por registro do
    JSON (a venda). "produtos" = uma linha por item da lista `produtos` de
    cada venda, com o `id` da venda copiado para `id_venda`.

    `descartados` são campos que o JSON traz e que ficam de fora de
    propósito. Qualquer campo que não esteja nem nas colunas nem aqui gera
    um aviso no log — é assim que se percebe a API mudando.
    """

    nome: str
    colunas: tuple[Coluna, ...]
    chave_primaria: tuple[str, ...]
    lista: str | None = None
    descartados: frozenset[str] = frozenset()

    @property
    def origens(self) -> set[str]:
        return {coluna.origem for coluna in self.colunas if coluna.origem}

    @property
    def destinos(self) -> list[str]:
        return [coluna.destino for coluna in self.colunas]


T, I, D = Tipo.TEXTO, Tipo.INTEIRO, Tipo.DECIMAL
ID_VENDA = Coluna(None, "id_venda", T)

# Dados de cliente: a planilha manda não armazenar (LGPD).
DADOS_DE_CLIENTE = frozenset({"cliente", "clienteFone", "clienteDocumento"})

# ---------------------------------------------------------------------------
# Blocos que aparecem iguais nos dois endpoints
# ---------------------------------------------------------------------------
CABECALHO_DA_VENDA: tuple[Coluna, ...] = (
    Coluna("redeLoja", "rede_loja"),
    Coluna("loja", "loja"),
    Coluna("cidade", "cidade"),
    Coluna("uf", "uf"),
    Coluna("royaltyImpostoEstimado", "royalty_imposto_estimado"),
    Coluna("crt", "crt"),
    Coluna("codigoLoja_Rede", "codigo_loja_rede"),
    Coluna("codigoLoja", "codigo_loja"),
    Coluna("venda", "venda"),
    Coluna("id", "id"),
    Coluna("nrAbertura", "nr_abertura"),
    Coluna("pdv", "pdv", I),
    Coluna("pdvNome", "pdv_nome"),
    Coluna("emissor", "emissor", substituir={"NF-e": "BNF"}),
    Coluna("emissor_Serie", "emissor_serie", I),
    Coluna("emissor_Numero", "emissor_numero"),
    Coluna("emissor_Chave", "emissor_chave"),
    Coluna("emissor_Status", "emissor_status"),
    Coluna("setor", "setor"),
    Coluna("aplicativo", "aplicativo"),
    Coluna("tipoEntrega", "tipo_entrega"),
    Coluna("tipoConsumo", "tipo_consumo"),
    Coluna("operador", "operador"),
    Coluna("nrCupom", "nr_cupom", I),
    Coluna("nrMesa", "nr_mesa", I),
    Coluna("idPedidoExterno", "id_pedido_externo"),
    Coluna("qtdPessoas", "qtd_pessoas", I),
    Coluna("data", "data", Tipo.DATA),
    Coluna("dataHoraTransacao", "data_hora_transacao", Tipo.DATA_HORA),
    Coluna("entregador", "entregador"),
    Coluna("idiomaAplicativo", "idioma_aplicativo"),
)

DADOS_DO_PRODUTO: tuple[Coluna, ...] = (
    Coluna("nrItem", "nr_item", I),
    Coluna("atendente", "atendente"),
    Coluna("codigoProduto", "codigo_produto"),
    Coluna("codigoProduto_OrigemEtapa", "codigo_produto_origem_etapa"),
    Coluna("promocao", "promocao"),
    Coluna("regraPreco", "regra_preco"),
    Coluna("produto", "produto"),
    Coluna("produto_OrigemEtapa", "produto_origem_etapa"),
    Coluna("grupo", "grupo"),
    Coluna("subGrupo", "sub_grupo"),
    Coluna("unidadeMedida", "unidade_medida"),
    Coluna("fatorCompra", "fator_compra", I),
    Coluna("unidadeMedidaCompra", "unidade_medida_compra"),
    Coluna("classificacaoRoyalty", "classificacao_royalty"),
    Coluna("codigoClassificacaoRoyalty", "codigo_classificacao_royalty"),
)

DADOS_DO_CANCELAMENTO: tuple[Coluna, ...] = (
    Coluna("tipoCancelamento", "tipo_cancelamento"),
    Coluna("operadorCancelamento", "operador_cancelamento"),
    Coluna("dataHoraCancelamento", "data_hora_cancelamento", Tipo.DATA_HORA),
    Coluna("motivoCancelamento", "motivo_cancelamento"),
)

QUANTIDADE_E_VALOR_DO_PRODUTO: tuple[Coluna, ...] = (
    Coluna("qtdProdutos", "qtd_produtos", D),
    Coluna("valorProduto", "valor_produto", D),
)

# ---------------------------------------------------------------------------
# common_vendas.xlsx
# ---------------------------------------------------------------------------
DADOS_VENDA = EsquemaTabela(
    nome="dados_venda",
    chave_primaria=("id",),
    colunas=CABECALHO_DA_VENDA + (
        Coluna("qtdProdutos", "qtd_produtos", D),
        Coluna("qtdPagamentos", "qtd_pagamentos", D),
        Coluna("qtdCombos", "qtd_combos", D),
        Coluna("valorProdutos", "valor_produtos", D),
        Coluna("valorRepiques", "valor_repiques", D),
        Coluna("valorTaxaServicos", "valor_taxa_servicos", D),
        Coluna("valorTaxaEntregas", "valor_taxa_entregas", D),
        Coluna("valorCreditos", "valor_creditos", D),
        Coluna("valorDescontos", "valor_descontos", D),
        Coluna("valorCombos", "valor_combos", D),
        Coluna("valorEspeciais", "valor_especiais", D),
        Coluna("valorAssinadas", "valor_assinadas", D),
        Coluna("valorCortesias", "valor_cortesias", D),
        Coluna("valorFaturamento", "valor_faturamento", D),
    ),
    # As listas viram tabelas próprias, logo abaixo.
    descartados=DADOS_DE_CLIENTE
    | {"produtos", "pagamentos", "descontos", "acrescimos", "fiscais"},
)

DADOS_PRODUTOS = EsquemaTabela(
    nome="dados_produtos",
    lista="produtos",
    chave_primaria=("id",),
    colunas=(Coluna("id", "id"), ID_VENDA) + DADOS_DO_PRODUTO + QUANTIDADE_E_VALOR_DO_PRODUTO,
)

DADOS_PAGAMENTO = EsquemaTabela(
    nome="dados_pagamento",
    lista="pagamentos",
    chave_primaria=("id",),
    colunas=(
        Coluna("id", "id"),
        ID_VENDA,
        Coluna("codigoFormaPagamento", "codigo_forma_pagamento"),
        Coluna("formaPagamento", "forma_pagamento"),
        Coluna("valorPagamento", "valor_pagamento", D),
        Coluna("nsu", "nsu"),
        Coluna("codigoAutorizacao", "codigo_autorizacao"),
        Coluna("bandeira", "bandeira"),
        Coluna("modalidade", "modalidade"),
        Coluna("tef", "tef"),
    ),
)

DADOS_DESCONTO = EsquemaTabela(
    nome="dados_desconto",
    lista="descontos",
    chave_primaria=("id",),
    colunas=(
        Coluna("id", "id"),
        ID_VENDA,
        Coluna("operadorDesconto", "operador_desconto"),
        Coluna("tipoDesconto", "tipo_desconto"),
        Coluna("desconto", "desconto"),
        Coluna("motivo", "motivo"),
        Coluna("codigoConvenio", "codigo_convenio"),
        Coluna("codigoCombo", "codigo_combo"),
        Coluna("valorDesconto", "valor_desconto", D),
        Coluna("qtdDescontos", "qtd_descontos", I),
    ),
)

DADOS_ACRESCIMO = EsquemaTabela(
    nome="dados_acrescimo",
    lista="acrescimos",
    chave_primaria=("id",),
    colunas=(
        Coluna("id", "id"),
        ID_VENDA,
        Coluna("operadorTaxa", "operador_taxa"),
        Coluna("tipoTaxa", "tipo_taxa"),
        Coluna("valorTaxa", "valor_taxa", D),
    ),
)

DADOS_FISCAL = EsquemaTabela(
    nome="dados_fiscal",
    lista="fiscais",
    # A planilha indica id_venda como chave primária, mas uma venda tem uma
    # linha por item E por imposto (ICMS, PIS, COFINS). Nos dados reais de
    # 17 a 23/10, 6 vendas geraram 138 linhas fiscais. A combinação abaixo
    # é a menor que identifica uma linha sozinha.
    chave_primaria=("id_venda", "emissor_numero_item", "imposto"),
    colunas=(
        Coluna("emissorNumero_Item", "emissor_numero_item", I),
        ID_VENDA,
        Coluna("produto", "produto"),
        Coluna("codigoProduto", "codigo_produto"),
        Coluna("cest", "cest"),
        Coluna("ncm", "ncm"),
        Coluna("gtim", "gtim"),
        Coluna("cfop", "cfop"),
        Coluna("valorVendaItem", "valor_venda_item", D),
        # Vem no JSON mas não está na planilha; mantido por decisão do time.
        Coluna("valorDescontoItem", "valor_desconto_item", D),
        Coluna("imposto", "imposto"),
        Coluna("cst", "cst"),
        Coluna("csosn", "csosn"),
        Coluna("baseCalculo", "base_calculo", D),
        Coluna("aliquota", "aliquota", D),
        Coluna("valorImposto", "valor_imposto", D),
    ),
)

# ---------------------------------------------------------------------------
# common_vendas_cancelamentos.xlsx
# ---------------------------------------------------------------------------
DADOS_VENDA_CANCELAMENTO = EsquemaTabela(
    nome="dados_venda_cancelamento",
    chave_primaria=("id",),
    colunas=CABECALHO_DA_VENDA + DADOS_DO_CANCELAMENTO,
    descartados=DADOS_DE_CLIENTE | {"produtos"},
)

DADOS_PRODUTO_CANCELAMENTO = EsquemaTabela(
    nome="dados_produto_cancelamento",
    lista="produtos",
    chave_primaria=("id",),
    colunas=(Coluna("id", "id"), ID_VENDA)
    + DADOS_DO_PRODUTO
    + DADOS_DO_CANCELAMENTO
    + QUANTIDADE_E_VALOR_DO_PRODUTO,
)

# ---------------------------------------------------------------------------
# Fonte da extração -> tabelas que ela alimenta
# ---------------------------------------------------------------------------
ESQUEMAS_POR_FONTE: dict[str, tuple[EsquemaTabela, ...]] = {
    "vendas": (
        DADOS_VENDA,
        DADOS_PRODUTOS,
        DADOS_PAGAMENTO,
        DADOS_DESCONTO,
        DADOS_ACRESCIMO,
        DADOS_FISCAL,
    ),
    "cancelamentos": (
        DADOS_VENDA_CANCELAMENTO,
        DADOS_PRODUTO_CANCELAMENTO,
    ),
}
