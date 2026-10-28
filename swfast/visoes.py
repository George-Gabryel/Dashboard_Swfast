"""Views de análise: a camada que o Metabase lê.

As tabelas `dados_*` guardam o que a API manda, campo a campo. As views
`painel_*` juntam o que o dashboard precisa — data e hora da venda em cada
item, dia da semana em português, tipo de pagamento — para que cada
pergunta do Metabase seja um SELECT simples, e a regra de negócio fique
aqui, versionada, e não espalhada em dezenas de perguntas.

São recriadas a cada execução do ETL (`Banco.criar_tabelas`), dentro de uma
transação: mudar uma view aqui chega ao banco no próximo ciclo.
"""

from __future__ import annotations

# Nome para mostrar e número (1 = segunda) para ordenar os gráficos.
DIA_DA_SEMANA = """
    CASE extract(isodow FROM {coluna})::int
        WHEN 1 THEN 'Segunda' WHEN 2 THEN 'Terça' WHEN 3 THEN 'Quarta'
        WHEN 4 THEN 'Quinta'  WHEN 5 THEN 'Sexta' WHEN 6 THEN 'Sábado'
        ELSE 'Domingo'
    END"""
NUMERO_DO_DIA = "extract(isodow FROM {coluna})::int"

# Nome -> SELECT. A ordem importa só para leitura; nenhuma view depende de outra.
VISOES: dict[str, str] = {
    "painel_vendas": f"""
        SELECT
            v.id,
            v.data,
            v.data_hora_transacao,
            extract(hour FROM v.data_hora_transacao)::int AS hora,
            {DIA_DA_SEMANA.format(coluna="v.data")} AS dia_semana,
            {NUMERO_DO_DIA.format(coluna="v.data")} AS dia_semana_numero,
            v.loja,
            v.setor,
            v.operador,
            v.nr_mesa,
            coalesce(v.qtd_pessoas, 0) AS qtd_pessoas,
            v.qtd_produtos,
            coalesce(v.valor_produtos, 0) AS valor_produtos,
            coalesce(v.valor_taxa_servicos, 0) AS valor_taxa_servicos,
            coalesce(v.valor_taxa_entregas, 0) AS valor_taxa_entregas,
            coalesce(v.valor_repiques, 0) AS valor_repiques,
            coalesce(v.valor_descontos, 0) AS valor_descontos,
            coalesce(v.valor_combos, 0) AS valor_combos,
            coalesce(v.valor_cortesias, 0) AS valor_cortesias,
            coalesce(v.valor_faturamento, 0) AS valor_faturamento
        FROM dados_venda v
    """,
    "painel_produtos": f"""
        SELECT
            p.id,
            p.id_venda,
            v.data,
            extract(hour FROM v.data_hora_transacao)::int AS hora,
            {DIA_DA_SEMANA.format(coluna="v.data")} AS dia_semana,
            {NUMERO_DO_DIA.format(coluna="v.data")} AS dia_semana_numero,
            v.operador,
            p.atendente,
            p.codigo_produto,
            p.produto,
            p.grupo,
            p.sub_grupo,
            p.promocao,
            p.produto_origem_etapa,
            coalesce(p.qtd_produtos, 0) AS qtd_produtos,
            coalesce(p.valor_produto, 0) AS valor_produto
        FROM dados_produtos p
        JOIN dados_venda v ON v.id = p.id_venda
    """,
    "painel_pagamentos": """
        SELECT
            g.id,
            g.id_venda,
            v.data,
            g.forma_pagamento,
            -- A API não preenche `modalidade`; o tipo sai do nome da forma.
            CASE
                WHEN upper(g.forma_pagamento) LIKE '%CREDITO%' OR upper(g.forma_pagamento) LIKE '%CRÉDITO%'
                    OR upper(g.forma_pagamento) LIKE '%AMEX%' THEN 'Crédito'
                WHEN upper(g.forma_pagamento) LIKE '%DEBITO%' OR upper(g.forma_pagamento) LIKE '%DÉBITO%' THEN 'Débito'
                WHEN upper(g.forma_pagamento) LIKE '%PIX%' THEN 'PIX'
                WHEN upper(g.forma_pagamento) LIKE '%DINHEIRO%' THEN 'Dinheiro'
                WHEN upper(g.forma_pagamento) ~ '(VOUCHER|ALELO|VR |TICKET|SODEXO|PLUXEE)' THEN 'Voucher'
                ELSE 'Outros'
            END AS tipo_pagamento,
            g.bandeira,
            coalesce(g.valor_pagamento, 0) AS valor_pagamento
        FROM dados_pagamento g
        JOIN dados_venda v ON v.id = g.id_venda
    """,
    "painel_descontos": """
        SELECT
            d.id,
            d.id_venda,
            v.data,
            d.tipo_desconto,
            d.desconto,
            d.motivo,
            d.operador_desconto,
            coalesce(d.qtd_descontos, 0) AS qtd_descontos,
            coalesce(d.valor_desconto, 0) AS valor_desconto
        FROM dados_desconto d
        JOIN dados_venda v ON v.id = d.id_venda
    """,
    "painel_acrescimos": """
        SELECT
            a.id,
            a.id_venda,
            v.data,
            a.tipo_taxa,
            a.operador_taxa,
            coalesce(a.valor_taxa, 0) AS valor_taxa
        FROM dados_acrescimo a
        JOIN dados_venda v ON v.id = a.id_venda
    """,
    "painel_cancelamentos": """
        SELECT
            p.id,
            p.id_venda,
            coalesce(p.data_hora_cancelamento, c.data_hora_cancelamento)::date AS data,
            coalesce(p.data_hora_cancelamento, c.data_hora_cancelamento) AS data_hora_cancelamento,
            coalesce(p.tipo_cancelamento, c.tipo_cancelamento) AS tipo_cancelamento,
            coalesce(p.motivo_cancelamento, c.motivo_cancelamento, 'Sem motivo') AS motivo_cancelamento,
            coalesce(p.operador_cancelamento, c.operador_cancelamento) AS operador_cancelamento,
            c.operador AS operador_caixa,
            p.atendente,
            p.produto,
            p.grupo,
            coalesce(p.qtd_produtos, 0) AS qtd_produtos,
            coalesce(p.valor_produto, 0) AS valor_produto
        FROM dados_produto_cancelamento p
        JOIN dados_venda_cancelamento c ON c.id = p.id_venda
    """,
}
