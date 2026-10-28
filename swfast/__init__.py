"""Pipeline ETL da API Swfast v5: extração (Etapa 1), transformação (Etapa 2) e carga (Etapa 3)."""

from .banco import Banco, ErroDeBanco
from .cliente import ClienteApi, ErroDeApi, ErroDeAutenticacao, ErroDeRequisicao
from .config import Configuracao, ConfiguracaoInvalida
from .extratores import Extrator, ResultadoExtracao
from .gravador import ManifestoExecucao
from .periodo import Periodo, PeriodoInvalido
from .pipeline import PipelineExtracao
from .transformacao import ErroDeTransformacao, Transformador

__all__ = [
    "Banco",
    "ClienteApi",
    "Configuracao",
    "ConfiguracaoInvalida",
    "ErroDeApi",
    "ErroDeBanco",
    "ErroDeAutenticacao",
    "ErroDeRequisicao",
    "ErroDeTransformacao",
    "Extrator",
    "ManifestoExecucao",
    "Periodo",
    "PeriodoInvalido",
    "PipelineExtracao",
    "ResultadoExtracao",
    "Transformador",
]
