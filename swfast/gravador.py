"""O manifesto de execução: o log secundário do pipeline.

Cada ciclo grava um JSON legível em `data/_manifesto/`, com o que aconteceu
em cada etapa de cada fonte — quantos registros a extração trouxe, quantas
linhas cada tabela teve na transformação, quantas foram gravadas no banco,
quanto tempo levou e, se falhou, em qual etapa e por quê. Abre no Bloco de
Notas ou no navegador, sem ferramenta nenhuma.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .extratores import ResultadoExtracao
from .periodo import Periodo
from .transformacao import RelatorioTabela

logger = logging.getLogger(__name__)

FORMATO_DATA_HORA = "%Y-%m-%d %H:%M:%S"


class ManifestoExecucao:
    """Acumula o resultado de cada fonte no ciclo e grava um JSON no fim."""

    def __init__(self, diretorio_manifesto: Path, periodo: Periodo | None = None) -> None:
        self.diretorio_manifesto = Path(diretorio_manifesto)
        self.periodo = periodo
        self.iniciado_em = datetime.now()
        self._inicio = time.monotonic()
        #: Uma entrada por fonte, já no formato do JSON.
        self.linhas: list[dict[str, Any]] = []

    def registrar(
        self,
        resultado: ResultadoExtracao,
        *,
        relatorios: list[RelatorioTabela] | None = None,
        carregadas: dict[str, int] | None = None,
        duracao_transformacao: float | None = None,
        duracao_carga: float | None = None,
        erro: str | None = None,
    ) -> None:
        """Registra uma fonte. `erro` já vem com a etapa: "carga: ...".

        Sem `erro` explícito, vale o erro da extração, se houver.
        """
        if erro is None and resultado.erro is not None:
            erro = f"extracao: {resultado.erro}"

        carregadas = carregadas or {}
        tabelas = {
            relatorio.tabela: {
                "transformadas": relatorio.linhas,
                "carregadas": carregadas.get(relatorio.tabela),
                "itens_vazios_ignorados": relatorio.itens_vazios_ignorados,
                "duplicadas_descartadas": relatorio.duplicadas_descartadas,
                "sem_chave_descartadas": relatorio.sem_chave_descartadas,
                "falhas_de_conversao": relatorio.falhas_de_conversao,
                "campos_ausentes": relatorio.campos_ausentes,
                "campos_desconhecidos": relatorio.campos_desconhecidos,
                "tem_alertas": relatorio.tem_alertas,
            }
            for relatorio in relatorios or []
        }

        self.linhas.append({
            "fonte": resultado.fonte,
            "sucesso": erro is None,
            "erro": erro,
            "extracao": {
                "caminho": resultado.caminho,
                "periodo_inicio": resultado.periodo.inicio_iso,
                "periodo_fim": resultado.periodo.fim_iso,
                "registros": resultado.quantidade,
                "duracao_segundos": round(resultado.duracao_segundos, 2),
                "executado_em": resultado.executado_em.strftime(FORMATO_DATA_HORA),
            },
            "transformacao": _etapa(duracao_transformacao),
            "carga": _etapa(duracao_carga),
            "tabelas": tabelas,
        })

    # -- leitura --------------------------------------------------------------
    @property
    def falhas(self) -> list[dict[str, Any]]:
        return [linha for linha in self.linhas if not linha["sucesso"]]

    @property
    def total_registros(self) -> int:
        return sum(linha["extracao"]["registros"] for linha in self.linhas)

    @property
    def tabelas_com_alerta(self) -> int:
        return sum(
            1 for linha in self.linhas for tabela in linha["tabelas"].values() if tabela["tem_alertas"]
        )

    def para_dicionario(self) -> dict[str, Any]:
        return {
            "executado_em": self.iniciado_em.strftime(FORMATO_DATA_HORA),
            "periodo": (
                {"inicio": self.periodo.inicio_iso, "fim": self.periodo.fim_iso} if self.periodo else None
            ),
            "duracao_segundos": round(time.monotonic() - self._inicio, 2),
            "sucesso": not self.falhas,
            "tabelas_com_alerta": self.tabelas_com_alerta,
            "fontes": self.linhas,
        }

    # -- gravação -------------------------------------------------------------
    def gravar(self) -> Path | None:
        if not self.linhas:
            return None

        self.diretorio_manifesto.mkdir(parents=True, exist_ok=True)
        # Microssegundos no nome: na carga histórica duas janelas podem
        # terminar no mesmo segundo, e uma sobrescreveria a outra.
        destino = self.diretorio_manifesto / f"execucao_{datetime.now():%Y%m%d_%H%M%S_%f}.json"
        temporario = destino.with_name(destino.name + ".tmp")
        with temporario.open("w", encoding="utf-8") as arquivo:
            json.dump(self.para_dicionario(), arquivo, ensure_ascii=False, indent=2, default=str)
        temporario.replace(destino)

        logger.info("Manifesto gravado em %s", destino)
        return destino

    def resumo_em_texto(self) -> str:
        """Resumo curto do ciclo, para o log e para o retorno do main."""
        total = len(self.linhas)
        falhas = self.falhas
        texto = f"{total - len(falhas)}/{total} fontes processadas, {self.total_registros} registros."
        if falhas:
            nomes = ", ".join(linha["fonte"] for linha in falhas)
            texto += f" Falhas: {nomes}."
        if self.tabelas_com_alerta:
            texto += f" {self.tabelas_com_alerta} tabelas com alerta."
        return texto


def _etapa(duracao: float | None) -> dict[str, float] | None:
    """None quando a etapa não chegou a rodar (a anterior falhou)."""
    return None if duracao is None else {"duracao_segundos": round(duracao, 2)}
