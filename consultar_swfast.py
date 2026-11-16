"""Consultas agregadas para usar com o aplicativo do Codex."""

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row

PROJETO = Path(__file__).resolve().parent

CONSULTAS = {
    'resumo': '''
        SELECT count(*) AS vendas,
               coalesce(sum(valor_liquido), 0) AS faturamento_liquido,
               coalesce(round(avg(valor_liquido)::numeric, 2), 0) AS ticket_medio,
               coalesce(sum(valor_desconto_total), 0) AS descontos
        FROM public.painel_vendas WHERE data BETWEEN %s AND %s
    ''',
    'por-dia': '''
        SELECT data, count(*) AS vendas, sum(valor_liquido) AS faturamento_liquido
        FROM public.painel_vendas WHERE data BETWEEN %s AND %s
        GROUP BY data ORDER BY data
    ''',
    'produtos': '''
        SELECT produto, sum(qtd_produtos) AS quantidade, sum(valor_produto) AS total_itens
        FROM public.painel_produtos WHERE data BETWEEN %s AND %s
        GROUP BY produto ORDER BY quantidade DESC, produto LIMIT 20
    ''',
    'pagamentos': '''
        SELECT tipo_pagamento, sum(valor_pagamento) AS total_pago
        FROM public.painel_pagamentos WHERE data BETWEEN %s AND %s
        GROUP BY tipo_pagamento ORDER BY total_pago DESC, tipo_pagamento
    ''',
    'cancelamentos': '''
        SELECT tipo_cancelamento, count(DISTINCT id_venda) AS vendas_com_cancelamento,
               sum(qtd_produtos) AS quantidade_cancelada, sum(valor_produto) AS valor_cancelado
        FROM public.painel_cancelamentos WHERE data BETWEEN %s AND %s
        GROUP BY tipo_cancelamento ORDER BY valor_cancelado DESC, tipo_cancelamento
    ''',
}


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('consulta', choices=CONSULTAS)
    cli.add_argument('--inicio', type=date.fromisoformat, help='Data inicial YYYY-MM-DD')
    cli.add_argument('--fim', type=date.fromisoformat, help='Data final YYYY-MM-DD')
    args = cli.parse_args()
    if bool(args.inicio) != bool(args.fim):
        cli.error('Informe --inicio e --fim juntos.')
    fim = args.fim or datetime.now(ZoneInfo('America/Fortaleza')).date()
    inicio = args.inicio or (fim - timedelta(days=6))
    if not 0 <= (fim - inicio).days < 366:
        cli.error('Informe um intervalo válido de até 366 dias.')

    banco = dotenv_values(PROJETO / '.env')
    meta = dotenv_values(PROJETO / '.env.meta')
    with psycopg.connect(
        host='127.0.0.1', port=int(banco.get('POSTGRES_PORT') or 5432),
        dbname=banco.get('POSTGRES_DB') or 'swfast',
        user=meta.get('METABASE_LEITOR_USUARIO') or 'metabase_leitor',
        password=meta['METABASE_LEITOR_SENHA'], row_factory=dict_row,
        options='-c default_transaction_read_only=on -c statement_timeout=15000',
        connect_timeout=5,
    ) as con:
        cobertura = con.execute('''
            SELECT min(data) AS primeira_venda, max(data) AS ultima_venda
            FROM public.painel_vendas
        ''').fetchone()
        linhas = con.execute(CONSULTAS[args.consulta], (inicio, fim)).fetchall()
    print(json.dumps({
        'consulta': args.consulta, 'inicio': inicio, 'fim': fim,
        'cobertura_das_vendas': cobertura, 'dados': linhas,
    }, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
