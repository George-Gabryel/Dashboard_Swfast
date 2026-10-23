"""Pipeline ETL da API Swfast v5: extração (Etapa 1) e transformação (Etapa 2)."""

from .cliente import ClienteApi, ErroDeApi, ErroDeAutenticacao, ErroDeRequisicao
from .config import Configuracao, ConfiguracaoInvalida
from .extratores import Extrator, ResultadoExtracao
from .gravador import GravadorBruto, ManifestoExecucao
from .periodo import Periodo, PeriodoInvalido
from .pipeline import PipelineExtracao
from .transformacao import EtapaTransformacao, ErroDeTransformacao, Transformador

__all__ = [
    "ClienteApi",
    "Configuracao",
    "ConfiguracaoInvalida",
    "ErroDeApi",
    "ErroDeAutenticacao",
    "ErroDeRequisicao",
    "ErroDeTransformacao",
    "EtapaTransformacao",
    "Extrator",
    "GravadorBruto",
    "ManifestoExecucao",
    "Periodo",
    "PeriodoInvalido",
    "PipelineExtracao",
    "ResultadoExtracao",
    "Transformador",
]
