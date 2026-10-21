"""Pipeline de extração de dados da API Swfast v5."""

from .cliente import ClienteApi, ErroDeApi, ErroDeAutenticacao, ErroDeRequisicao
from .config import Configuracao, ConfiguracaoInvalida
from .extratores import Extrator, ResultadoExtracao
from .gravador import GravadorBruto, ManifestoExecucao
from .periodo import Periodo, PeriodoInvalido
from .pipeline import PipelineExtracao

__all__ = [
    "ClienteApi",
    "Configuracao",
    "ConfiguracaoInvalida",
    "ErroDeApi",
    "ErroDeAutenticacao",
    "ErroDeRequisicao",
    "Extrator",
    "GravadorBruto",
    "ManifestoExecucao",
    "Periodo",
    "PeriodoInvalido",
    "PipelineExtracao",
    "ResultadoExtracao",
]
