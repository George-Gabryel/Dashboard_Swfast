-- Camadas do pipeline ELT

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS marts;

COMMENT ON SCHEMA raw IS  'Dados brutos da API Swfast, gravados como vieream (JSON)';
COMMENT ON SCHEMA staging IS 'Dados limpos, tipados e deduplicados';
COMMENT ON SCHEMA marts IS 'Tabelas e views prontas para o Metabase';