"""Gravação da camada bruta (raw) e do manifesto de execução.

Dois arquivos saem de cada ciclo:

1. O JSON bruto de cada fonte, fiel ao que a API devolveu. É o insumo do
   Transform e a garantia de que dá para reprocessar sem chamar a API.
2. O manifesto — uma linha por fonte, com contagem, duração e erro. É o
   registro de que o ciclo rodou e o que aconteceu nele.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import polars as pl

from .extratores import ResultadoExtracao

logger = logging.getLogger(__name__)

# Uma coluna por chave de ResultadoExtracao.resumo(), mais o caminho do arquivo.
ESQUEMA_MANIFESTO = {
    "fonte": pl.String,
    "caminho": pl.String,
    "periodo_inicio": pl.String,
    "periodo_fim": pl.String,
    "registros": pl.Int64,
    "duracao_segundos": pl.Float64,
    "executado_em": pl.String,
    "sucesso": pl.Boolean,
    "erro": pl.String,
    "arquivo": pl.String,
}


class GravadorBruto:
    """Escreve o resultado da extração em disco, particionado por fonte e dia."""

    def __init__(self, diretorio_raw: Path) -> None:
        self.diretorio_raw = Path(diretorio_raw)

    def caminho_para(self, resultado: ResultadoExtracao) -> Path:
        """Monta o caminho do arquivo — determinístico, e é isso que dá idempotência.

        data/raw/vendas/dt=2026-10-21/vendas_20261015_20261021.json

        Rodar o mesmo período duas vezes grava o mesmo nome e sobrescreve.
        Nunca gera duplicata.
        """
        particao = f"dt={datetime.now():%Y-%m-%d}"
        pasta = self.diretorio_raw / resultado.fonte / particao
        return pasta / f"{resultado.fonte}_{resultado.periodo.rotulo}.json"

    def gravar(self, resultado: ResultadoExtracao) -> Path | None:
        """Grava o JSON bruto. Devolve None se não houve o que gravar."""
        if not resultado.sucesso:
            logger.warning("[%s] nada gravado: a extração falhou.", resultado.fonte)
            return None
        if resultado.quantidade == 0:
            logger.info("[%s] período sem registros, nenhum arquivo gerado.", resultado.fonte)
            return None

        destino = self.caminho_para(resultado)
        destino.parent.mkdir(parents=True, exist_ok=True)

        # Grava num .tmp e só no fim troca pelo definitivo: se o processo cair
        # no meio, a versão anterior continua intacta em vez de virar um JSON
        # pela metade. O replace é atômico no mesmo disco.
        temporario = destino.with_name(destino.name + ".tmp")

        # ensure_ascii=False preserva acentos nas descrições de produto;
        # o JSON fica legível na hora de conferir um caso específico.
        with temporario.open("w", encoding="utf-8") as arquivo:
            json.dump(resultado.registros, arquivo, ensure_ascii=False, indent=2, default=str)
        temporario.replace(destino)

        logger.info("[%s] %d registros em %s", resultado.fonte, resultado.quantidade, destino)
        return destino


class ManifestoExecucao:
    """Acumula os resumos do ciclo e grava um Parquet com o histórico.

    É aqui que o Polars entra na etapa de extração: não para transformar
    dados de venda, mas para manter o log das execuções em formato
    consultável — dá para abrir no Metabase e ver se algum ciclo falhou.
    """

    def __init__(self, diretorio_manifesto: Path) -> None:
        self.diretorio_manifesto = Path(diretorio_manifesto)
        self.linhas: list[dict] = []

    def registrar(self, resultado: ResultadoExtracao, arquivo: Path | None) -> None:
        linha = resultado.resumo()
        linha["arquivo"] = str(arquivo) if arquivo else None
        self.linhas.append(linha)

    @property
    def falhas(self) -> list[dict]:
        return [linha for linha in self.linhas if not linha["sucesso"]]

    @property
    def total_registros(self) -> int:
        return sum(linha["registros"] for linha in self.linhas)

    def para_dataframe(self) -> pl.DataFrame:
        # Esquema fixo: sem ele, um ciclo sem erros grava `erro` como Null e um
        # com erros como String, e os Parquets deixam de ser lidos juntos.
        return pl.DataFrame(self.linhas, schema=ESQUEMA_MANIFESTO)

    def gravar(self) -> Path | None:
        if not self.linhas:
            return None

        self.diretorio_manifesto.mkdir(parents=True, exist_ok=True)
        # Microssegundos no nome: na carga histórica duas janelas podem
        # terminar no mesmo segundo, e uma sobrescreveria a outra.
        destino = self.diretorio_manifesto / f"execucao_{datetime.now():%Y%m%d_%H%M%S_%f}.parquet"
        self.para_dataframe().write_parquet(destino)
        logger.info("Manifesto gravado em %s", destino)
        return destino

    def resumo_em_texto(self) -> str:
        """Resumo curto do ciclo, para o log e para o retorno do main."""
        total = len(self.linhas)
        falhas = self.falhas
        texto = f"{total - len(falhas)}/{total} fontes coletadas, {self.total_registros} registros."
        if falhas:
            nomes = ", ".join(linha["fonte"] for linha in falhas)
            texto += f" Falhas: {nomes}."
        return texto
