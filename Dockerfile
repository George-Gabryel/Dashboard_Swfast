# Imagem do ETL Swfast: roda `python main.py --continuo` para sempre.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Recife

# tzdata: sem ele o TZ acima é ignorado e o agendamento roda em UTC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependências numa camada própria: mudar o código não reinstala tudo.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY swfast/ swfast/
COPY main.py .

# Usuário sem privilégios; data/ é onde ele grava manifestos e logs.
RUN useradd --create-home --uid 1000 etl \
    && mkdir -p /app/data \
    && chown etl:etl /app/data
USER etl

CMD ["python", "main.py", "--continuo"]
