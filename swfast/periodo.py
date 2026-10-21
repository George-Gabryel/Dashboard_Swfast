"""Representação de um período de consulta e as regras que ele precisa respeitar.

A API aceita no máximo 7 dias entre dataInicio e dataFim. Em vez de espalhar
essa regra por todo o código, ela mora aqui: quem cria um Periodo já recebe
o objeto validado, ou um erro.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

LIMITE_DIAS_API = 7


class PeriodoInvalido(ValueError):
    """Período fora das regras aceitas pela API."""


@dataclass(frozen=True)
class Periodo:
    """Um intervalo fechado de datas [inicio, fim]."""

    inicio: date
    fim: date

    def __post_init__(self) -> None:
        if self.fim < self.inicio:
            raise PeriodoInvalido(
                f"A data final ({self.fim}) é anterior à inicial ({self.inicio})."
            )
        if self.dias > LIMITE_DIAS_API:
            raise PeriodoInvalido(
                f"O período tem {self.dias} dias. A API aceita no máximo {LIMITE_DIAS_API}. "
                "Use Periodo.fatiar() para quebrar o intervalo em janelas menores."
            )

    # ------------------------------------------------------------------
    # Construtores
    # ------------------------------------------------------------------
    @classmethod
    def ultimos_dias(cls, dias: int, referencia: date | None = None) -> "Periodo":
        """Janela que termina hoje (ou na data de referência) e cobre `dias` dias.

        `ultimos_dias(7)` em 21/10 devolve 15/10 a 21/10: 7 dias contando o de hoje.
        """
        if dias < 1:
            raise PeriodoInvalido("A janela precisa ter ao menos 1 dia.")
        fim = referencia or date.today()
        return cls(inicio=fim - timedelta(days=dias - 1), fim=fim)

    @classmethod
    def de_texto(cls, inicio: str, fim: str) -> "Periodo":
        """Constrói a partir de duas datas no formato YYYY-MM-DD."""
        try:
            return cls(
                inicio=datetime.strptime(inicio, "%Y-%m-%d").date(),
                fim=datetime.strptime(fim, "%Y-%m-%d").date(),
            )
        except ValueError as erro:
            raise PeriodoInvalido(
                "As datas precisam estar no formato YYYY-MM-DD (ex.: 2026-10-15)."
            ) from erro

    # ------------------------------------------------------------------
    # Propriedades e formatação
    # ------------------------------------------------------------------
    @property
    def dias(self) -> int:
        """Quantidade de dias cobertos, contando as duas pontas."""
        return (self.fim - self.inicio).days + 1

    @property
    def inicio_iso(self) -> str:
        """Data inicial como 2026-10-15 — formato dos endpoints de data."""
        return self.inicio.isoformat()

    @property
    def fim_iso(self) -> str:
        return self.fim.isoformat()

    @property
    def inicio_datahora(self) -> str:
        """Primeiro instante do período: 2026-10-15T00:00:00."""
        return datetime.combine(self.inicio, time.min).isoformat(timespec="seconds")

    @property
    def fim_datahora(self) -> str:
        """Último instante do período: 2026-10-21T23:59:59."""
        return datetime.combine(self.fim, time(23, 59, 59)).isoformat(timespec="seconds")

    @property
    def rotulo(self) -> str:
        """Identificador curto usado no nome dos arquivos: 20261015_20261021."""
        return f"{self.inicio:%Y%m%d}_{self.fim:%Y%m%d}"

    # ------------------------------------------------------------------
    # Fatiamento
    # ------------------------------------------------------------------
    @staticmethod
    def fatiar(inicio: date, fim: date, dias_por_janela: int = LIMITE_DIAS_API) -> list["Periodo"]:
        """Quebra um intervalo longo em janelas que a API aceita.

        Serve para a carga histórica: pedir 90 dias de uma vez é recusado,
        então o intervalo vira 13 janelas de 7 dias.
        """
        if dias_por_janela > LIMITE_DIAS_API:
            raise PeriodoInvalido(
                f"A janela não pode passar de {LIMITE_DIAS_API} dias."
            )
        if fim < inicio:
            raise PeriodoInvalido("A data final é anterior à inicial.")

        janelas: list[Periodo] = []
        cursor = inicio
        while cursor <= fim:
            fim_janela = min(cursor + timedelta(days=dias_por_janela - 1), fim)
            janelas.append(Periodo(inicio=cursor, fim=fim_janela))
            cursor = fim_janela + timedelta(days=1)
        return janelas

    def __str__(self) -> str:
        return f"{self.inicio_iso} a {self.fim_iso}"
