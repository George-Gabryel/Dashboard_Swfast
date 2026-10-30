# Dashboard Swfast

Pipeline ETL que coleta as vendas e os cancelamentos da **API Swfast v5**, grava tudo num **PostgreSQL** e publica um **dashboard no Metabase**. O cliente abre esse dashboard por um atalho na área de trabalho do Windows.

Tudo roda em containers Docker na própria máquina do cliente:

| Container         | Origem                                   | Papel                                                         |
|-------------------|------------------------------------------|---------------------------------------------------------------|
| `swfast-postgres` | `docker_postgre/docker-compose.yml`      | Banco `swfast` (dados) e banco `metabase` (config. do Metabase) |
| `swfast-etl`      | `docker-compose.yml` (este repositório)  | Roda o ETL ao subir e depois a cada 3 h                         |
| `swfast-metabase` | `metabase/docker-compose.yml`            | Serve o dashboard em `http://127.0.0.1:3000`                    |

Os três compartilham a rede Docker `dashboard-swfast_default`, que é criada pelo compose do banco. Por isso **o banco sempre sobe primeiro**.

---

## Sumário

1. [Como o código funciona](#como-o-código-funciona)
2. [Instalação numa máquina Windows nova](#instalação-numa-máquina-windows-nova)
3. [Ligar sozinho quando a máquina inicia](#ligar-sozinho-quando-a-máquina-inicia)
4. [Operação do dia a dia](#operação-do-dia-a-dia)
5. [Desenvolvimento local e testes](#desenvolvimento-local-e-testes)
6. [Referência de configuração](#referência-de-configuração)
7. [Solução de problemas](#solução-de-problemas)

---

## Como o código funciona

### Visão geral do fluxo

```
                    a cada 3 h (00h, 03h, 06h ... 21h)
                                   │
┌──────────────┐   GET    ┌────────▼────────┐  tabelas   ┌──────────────┐  SQL   ┌──────────┐
│ API Swfast v5│ ───────► │  swfast-etl     │ ─────────► │  PostgreSQL  │ ◄───── │ Metabase │
│ /common/...  │   JSON   │ extrai,         │  dados_*   │  banco swfast│ views  │ dashboard│
└──────────────┘          │ transforma,     │  painel_*  └──────────────┘painel_*└────▲─────┘
                          │ carrega         │                                         │
                          └────────┬────────┘                              atalho no Windows
                                   │                                    (Edge em modo aplicativo)
                     data/_manifesto/*.json  data/_logs/*.log
```

Cada ciclo processa as **fontes** do catálogo, uma por vez, com 5 s de pausa entre elas. O processamento é sequencial de propósito, para não disputar CPU com o sistema do restaurante. Cada fonte passa por três etapas:

1. **Extração (E)**: chama a API com `dataInicio`/`dataFim` cobrindo os **últimos 7 dias**, que é o máximo que a API aceita.
2. **Transformação (T)**: converte o JSON em tabelas tipadas (Polars), em memória.
3. **Carga (L)**: grava no Postgres numa única transação por fonte.

Uma falha em qualquer etapa de uma fonte é registrada no manifesto e **não impede a fonte seguinte**.

Como cada ciclo recoleta os últimos 7 dias, a mesma venda chega várias vezes. A carga é idempotente: a venda é atualizada, nunca duplicada. Assim, alterações feitas no PDV dentro dessa janela (um cancelamento tardio, por exemplo) chegam ao banco.

### Fontes coletadas

| Fonte           | Endpoint                         | Tabelas geradas                                                                                   |
|-----------------|----------------------------------|---------------------------------------------------------------------------------------------------|
| `vendas`        | `/common/vendas`                 | `dados_venda` (pai), `dados_produtos`, `dados_pagamento`, `dados_desconto`, `dados_acrescimo`, `dados_fiscal` |
| `cancelamentos` | `/common/vendas/cancelamentos`   | `dados_venda_cancelamento` (pai), `dados_produto_cancelamento`                                     |

### Estrutura do código

```
Dashboard_Swfast/
├── main.py                  Ponto de entrada (CLI) e modo contínuo/agendamento
├── swfast/
│   ├── config.py            Lê o .env e o .env.meta; único lugar que toca variáveis de ambiente
│   ├── catalogo.py          Lista de fontes (o que coletar e em que ordem)
│   ├── cliente.py           Cliente HTTP: autenticação, retentativas, tratamento de erros
│   ├── periodo.py           Janela de datas e a regra dos 7 dias; fatiamento da carga histórica
│   ├── extratores.py        Chama um endpoint e normaliza o retorno para lista de registros
│   ├── esquemas.py          As tabelas de destino (colunas, tipos, chaves), transcritas das planilhas
│   ├── transformacao.py     JSON → DataFrames tipados, com relatório de qualidade
│   ├── banco.py             CREATE TABLE/VIEW e carga (COPY + upsert) no Postgres
│   ├── visoes.py            Views painel_* que o Metabase consulta (regras de negócio)
│   ├── gravador.py          Manifesto JSON de cada ciclo
│   ├── pipeline.py          Orquestra E → T → L por fonte
│   └── metabase.py          Instalador do dashboard no Metabase (via API, sem cliques)
├── docker_postgre/
│   ├── docker-compose.yml   Postgres 18, projeto "dashboard-swfast", porta 127.0.0.1:5432
│   ├── .env.postgre.example Modelo de configuração do banco (nome, usuário e senha)
│   ├── init/01-schemas.sql  Executado só na primeira criação do volume
│   └── backups/             Montada em /backups no container (fora do git)
├── metabase/
│   ├── docker-compose.yml   Container do Metabase
│   └── abrir_dashboard.ps1  Atalho do cliente no Windows (liga os containers e abre o Edge)
├── tests/                   Testes (pytest)
├── Dockerfile               Imagem do ETL (python:3.14-slim)
├── docker-compose.yml       Serviços etl e instalar-dashboard
├── .env.example             Modelo de configuração do ETL
└── .env.meta.example        Modelo de configuração do Metabase
```

Cada parte tem seu próprio compose e seu próprio arquivo de variáveis, para que nenhum container receba senhas de que não precisa:

| Compose                             | Arquivo de variáveis        | Quem lê                          |
|-------------------------------------|-----------------------------|----------------------------------|
| `docker_postgre/docker-compose.yml` | `docker_postgre/.env.postgre` | Postgres                        |
| `docker-compose.yml`                | `.env` (+ `.env.meta` no `instalar-dashboard`) | ETL e instalador do dashboard |
| `metabase/docker-compose.yml`       | `.env.meta`                 | Metabase                         |

### Detalhes de cada etapa

**Cliente HTTP (`cliente.py`)**
- Envia a chave no header `API-Key`.
- Para `429` e `5xx` tenta de novo com espera progressiva (2 s, 4 s, 8 s...). No `429` respeita o `Retry-After`, com teto de 60 s.
- Para `401`/`403` (chave errada), `400` (parâmetro errado) e `404` desiste na hora, porque tentar de novo não resolveria.

**Transformação (`transformacao.py` + `esquemas.py`)**
- `esquemas.py` é a tradução das planilhas de especificação (`common_vendas.xlsx` e `common_vendas_cancelamentos.xlsx`). Cada aba virou um `EsquemaTabela` e cada linha virou uma `Coluna`, com campo do JSON, nome no banco e tipo. Se a planilha mudar, só esse arquivo muda.
- Tabelas filhas (produtos, pagamentos etc.) têm uma linha por item da lista correspondente da venda e recebem o `id_venda` do pai.
- Regras de limpeza:
  - texto vazio vira `NULL`;
  - item de lista sem nenhum dado é ignorado;
  - valor que não cabe no tipo vira `NULL` e é contado;
  - linhas sem chave primária ou duplicadas são descartadas (das duplicadas, fica a última).
- Campos de cliente (`cliente`, `clienteFone`, `clienteDocumento`) **não são armazenados**, por causa da LGPD.
- O relatório de cada tabela avisa no log quando o JSON da API deixa de bater com a planilha: campo esperado ausente ou campo novo desconhecido. Isso conta como **alerta**.

**Carga (`banco.py`)**
- As tabelas são criadas automaticamente a partir dos esquemas (`CREATE TABLE IF NOT EXISTS`), no schema `public`. Não existe arquivo `.sql` para manter.
- As views `painel_*` são apagadas e recriadas a cada execução, então uma alteração em `visoes.py` chega ao banco no ciclo seguinte.
- Por fonte, numa transação única:
  1. a tabela pai recebe **upsert** pela chave primária;
  2. nas filhas, as linhas das vendas do lote são **apagadas e regravadas**, de modo que um item removido na API também some do banco;
  3. qualquer erro desfaz a fonte inteira.
- A gravação usa `COPY` para uma tabela temporária seguido de `INSERT ... ON CONFLICT`, que é o caminho rápido do Postgres para muitas linhas.

**Views do dashboard (`visoes.py`)**

O Metabase lê só as views, nunca as tabelas `dados_*`:

| View                   | Conteúdo                                                                                         |
|------------------------|--------------------------------------------------------------------------------------------------|
| `painel_vendas`        | Venda com hora, dia da semana em português, valor bruto, desconto total e líquido                 |
| `painel_produtos`      | Itens vendidos com data/hora da venda                                                             |
| `painel_pagamentos`    | Pagamentos com `tipo_pagamento` (Crédito, Débito, PIX, Dinheiro, Voucher, Outros) deduzido do nome da forma |
| `painel_descontos`     | Descontos por venda                                                                               |
| `painel_acrescimos`    | Taxas/acréscimos por venda                                                                        |
| `painel_cancelamentos` | Itens cancelados com o momento correto do cancelamento (Total ou Item)                           |
| `painel_calendario`    | Um dia por linha, usado para comparar com o período anterior                                      |

**Manifesto e logs (`gravador.py`)**
- `data/_logs/pipeline_AAAAMMDD.log`: um arquivo por dia, com tudo que aparece na tela.
- `data/_manifesto/execucao_*.json`: um por ciclo, com registros extraídos, linhas transformadas e gravadas por tabela, tempos de cada etapa, alertas e, em caso de falha, a etapa e o motivo. Abre no Bloco de Notas.

**Instalador do dashboard (`metabase.py`)**

Rodado com `docker compose run --rm instalar-dashboard`, faz tudo pela API do Metabase:
1. cria no Postgres o banco `metabase` (dono `metabase_app`) e o usuário somente leitura `metabase_leitor`;
2. espera o Metabase responder;
3. cria o administrador (ou entra com ele, se já existe);
4. conecta o banco `swfast` usando o usuário de leitura;
5. monta o dashboard **"Swfast — Vendas"**, com 4 abas (Faturamento, Produtos, Pagamentos e descontos, Cancelamentos) e filtro de período;
6. liga o link público e grava em `data/dashboard_url.txt`.

Pode ser executado várias vezes: atualiza as perguntas no lugar, sem duplicar nada, e o link público continua o mesmo.

**Atalho do cliente (`metabase/abrir_dashboard.ps1`)**

O atalho "Dashboard Swfast" na área de trabalho roda esse script escondido. O script:
1. verifica se o Metabase responde;
2. se não responder, liga os containers `swfast-postgres` e `swfast-metabase` (com Docker Desktop ou com Docker dentro do WSL) e espera até 3 min;
3. abre o link público no Edge em modo aplicativo (janela própria, sem barra de endereço), com atualização a cada 10 min.

Se o dashboard já estiver aberto, o script só traz a janela para a frente. O log fica em `%USERPROFILE%\SwfastDashboard\abrir_dashboard.log`.

---

## Instalação numa máquina Windows nova

Há duas formas de rodar o Docker no Windows. Escolha uma:

| Opção                                   | Quando usar                                       | Liga sozinho no boot?                                |
|-----------------------------------------|---------------------------------------------------|------------------------------------------------------|
| **A. Docker Desktop**                   | Instalação mais simples                           | Só depois que alguém faz login no Windows            |
| **B. WSL2 + Docker Engine (Ubuntu)**    | Produção, máquina que fica ligada sem login       | Sim, com a tarefa agendada descrita mais abaixo      |

Os comandos `docker compose` são os mesmos nas duas. Na opção A eles rodam no PowerShell; na opção B, no terminal do Ubuntu.

### 1. Pré-requisitos

1. **Windows 10 22H2 ou Windows 11**, com virtualização habilitada na BIOS.
2. **WSL2**. Num PowerShell **como administrador**:
   ```powershell
   wsl --install
   ```
   Reinicie quando o comando pedir.
3. **Docker**, conforme a opção escolhida:
   - **A. Docker Desktop**: baixe em <https://www.docker.com/products/docker-desktop/>, instale com o backend WSL2 e abra uma vez para concluir a configuração.
   - **B. Docker Engine no Ubuntu do WSL**: no terminal do Ubuntu, siga <https://docs.docker.com/engine/install/ubuntu/> e depois rode `sudo usermod -aG docker $USER`. Feche e abra o terminal.
4. **Git**: <https://git-scm.com/download/win> (ou `sudo apt install git` no Ubuntu, na opção B).
5. **Microsoft Edge**: já vem no Windows e é usado pelo atalho.
6. **Chave da API Swfast v5** do cliente.

Para conferir:
```powershell
docker --version
docker compose version
```

### 2. Copiar os arquivos

Tudo está neste repositório, inclusive o compose do banco (`docker_postgre/`):

```powershell
mkdir C:\swfast; cd C:\swfast
git clone <url-do-repositorio> Dashboard_Swfast
```

Os arquivos `.env`, `.env.meta`, `docker_postgre\.env.postgre` e a pasta `data\` **não estão no git**. São criados nos passos seguintes.

> **Opção B:** clone **dentro do Linux** (ex.: `~/swfast`), e não em `/mnt/c/...`. Fica mais rápido e evita problemas de permissão nas pastas montadas.

### 3. Subir o banco

```powershell
cd C:\swfast\Dashboard_Swfast\docker_postgre
copy .env.postgre.example .env.postgre
notepad .env.postgre
```

Preencha o `.env.postgre`. **Não deixe espaços em volta do `=`**:

```ini
POSTGRES_DB=swfast
POSTGRES_USER=swfast
POSTGRES_PASSWORD=<uma senha forte>
```

```powershell
docker compose up -d
docker ps      # swfast-postgres deve aparecer como "healthy"
```

Esse comando também cria a rede `dashboard-swfast_default`, que os outros containers usam.

O arquivo se chama `.env.postgre`, e não `.env`, para não ser confundido com o `.env` do ETL. Por isso o compose o carrega com `env_file:`, que passa as variáveis direto para o container, em vez de usar `${...}`. A porta publicada é fixa (`127.0.0.1:5432`) no `docker-compose.yml`. Para trocá-la, altere ali e no `POSTGRES_PORT` do `.env` do ETL.

> ⚠️ O usuário e a senha só são aplicados **na primeira vez**, quando o volume `pgdata` é criado. Se mudar o `.env.postgre` depois, o banco continua com a senha antiga. Para recomeçar do zero, rode `docker compose down -v`, o que **apaga todos os dados**.

> ⚠️ Não mude o `name: dashboard-swfast` do compose do banco. Dele saem o nome do volume com os dados (`dashboard-swfast_pgdata`) e o nome da rede que o ETL e o Metabase usam.

### 4. Configurar e subir o ETL

```powershell
cd C:\swfast\Dashboard_Swfast
copy .env.example .env
notepad .env
```

Itens obrigatórios:
- `SWFAST_API_KEY`: a chave do cliente.
- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`: **idênticos** aos do `docker_postgre\.env.postgre`.
- `POSTGRES_HOST=127.0.0.1` e `POSTGRES_PORT=5432` podem ficar assim. Dentro do container, o compose troca esses valores por `postgres:5432`.

Se a conta for de **loja central** e for preciso filtrar lojas, preencha `LISTA_DE_LOJAS` com os códigos separados por vírgula.

```powershell
docker compose up -d --build
docker logs -f swfast-etl
```

O primeiro ciclo roda logo ao subir. Ele cria as tabelas e views e coleta os últimos 7 dias. Espere ver no log `Ciclo concluído — 2/2 fontes processadas` e saia com `Ctrl+C`; o container continua rodando.

### 5. (Opcional) Carga histórica

Para trazer meses anteriores, rode uma vez dentro do container que já está de pé. O intervalo é quebrado sozinho em janelas de 7 dias:

```powershell
docker exec swfast-etl python main.py --historico 2026-01-01 2026-10-29
```

### 6. Configurar e subir o Metabase

```powershell
copy .env.meta.example .env.meta
notepad .env.meta
```

Preencha com senhas novas:
- `METABASE_ADMIN_EMAIL` / `METABASE_ADMIN_SENHA`: login de administrador, usado para editar o dashboard.
- `MB_DB_PASS`: senha do dono do banco interno `metabase`.
- `METABASE_LEITOR_SENHA`: senha do usuário somente leitura que o Metabase usa para consultar os dados.

```powershell
docker compose -f metabase/docker-compose.yml up -d
docker compose run --rm instalar-dashboard
```

Enquanto o banco `metabase` não existe, o container do Metabase reinicia sozinho. Isso é esperado: o `instalar-dashboard` cria o banco, espera o Metabase ficar de pé (a primeira subida leva de 1 a 3 min) e monta o dashboard. No fim aparece:

```
Dashboard pronto: http://127.0.0.1:3000/public/dashboard/<uuid>
```

e o link fica gravado em `data\dashboard_url.txt`.

### 7. Criar o atalho na área de trabalho do cliente

No **Windows**, logado com o **usuário do cliente**, num PowerShell comum (não precisa ser administrador):

```powershell
cd C:\swfast\Dashboard_Swfast
powershell -ExecutionPolicy Bypass -File metabase\abrir_dashboard.ps1 -Instalar
```

> **Opção B:** o script e o link estão dentro do Linux. Aponte para eles pelo caminho de rede do WSL:
> ```powershell
> powershell -ExecutionPolicy Bypass -File \\wsl$\Ubuntu\home\<usuario>\swfast\Dashboard_Swfast\metabase\abrir_dashboard.ps1 -Instalar `
>   -ArquivoUrl \\wsl$\Ubuntu\home\<usuario>\swfast\Dashboard_Swfast\data\dashboard_url.txt
> ```

O comando copia o script e o link para `%USERPROFILE%\SwfastDashboard` e cria o atalho **"Dashboard Swfast"** na área de trabalho. Dê dois cliques para testar.

### Checklist final

- [ ] `docker ps` mostra `swfast-postgres`, `swfast-etl` e `swfast-metabase` rodando.
- [ ] `data\_manifesto\` tem um `execucao_*.json` com `"sucesso": true`.
- [ ] `http://127.0.0.1:3000` abre o Metabase e o login de administrador funciona.
- [ ] O atalho abre o dashboard com dados.
- [ ] Depois de reiniciar a máquina, tudo volta sozinho (próxima seção).

---

## Ligar sozinho quando a máquina inicia

Os três containers usam `restart: unless-stopped`, ou seja, voltam sozinhos **sempre que o Docker sobe**. O que falta é fazer o Docker subir.

**Opção A (Docker Desktop)**
- Em *Settings → General*, marque **"Start Docker Desktop when you sign in to your computer"**.
- Limitação: o Docker só sobe depois do login no Windows. Se o cliente abrir o atalho antes de o Docker estar pronto, o script espera até 3 min.

**Opção B (WSL2 + Docker Engine): sobe no boot, sem login**
1. No Ubuntu, edite `/etc/wsl.conf` (`sudo nano /etc/wsl.conf`):
   ```ini
   [boot]
   systemd=true
   ```
   Depois rode `sudo systemctl enable docker` e, no PowerShell, `wsl --shutdown`.
2. No Windows, abra o **Agendador de Tarefas → Criar Tarefa**:
   - **Geral**: marque "Executar estando o usuário conectado ou não".
   - **Disparadores**: "Ao iniciar o sistema".
   - **Ações**: programa `wsl.exe`, argumentos `-d Ubuntu --exec sleep infinity`.

   A tarefa liga o WSL no boot e o mantém de pé. Sem ela, o WSL só sobe no login.

---

## Operação do dia a dia

| Tarefa                         | Comando (na pasta `Dashboard_Swfast`)                                    |
|--------------------------------|--------------------------------------------------------------------------|
| Acompanhar o ETL               | `docker logs -f swfast-etl`                                              |
| Ver o último ciclo             | abrir o `execucao_*.json` mais recente em `data\_manifesto\`             |
| Forçar uma coleta agora        | `docker exec swfast-etl python main.py`                                  |
| Coletar um período específico  | `docker exec swfast-etl python main.py --inicio 2026-10-01 --fim 2026-10-07` |
| Coletar só uma fonte           | `docker exec swfast-etl python main.py --fontes vendas`                  |
| Atualizar o código             | `git pull` e depois `docker compose up -d --build`                       |
| Atualizar o dashboard          | `docker compose run --rm instalar-dashboard` (depois de mudar `visoes.py` ou `metabase.py`) |
| Editar o dashboard à mão       | `http://127.0.0.1:3000` com o login de `.env.meta`                       |
| Parar o ETL                    | `docker compose stop` (o dashboard continua funcionando)                 |

### Opções do `main.py`

```
python main.py                                   # últimos 7 dias (DIAS_POR_JANELA)
python main.py --inicio 2026-10-01 --fim 2026-10-07
python main.py --historico 2026-01-01 2026-10-21 # fatia em janelas de até 7 dias
python main.py --fontes vendas                   # só as fontes listadas
python main.py --continuo                        # modo do container: roda para sempre
```

O modo `--continuo` roda um ciclo ao iniciar e depois em horários fixos a cada `INTERVALO_HORAS`, contados a partir de 00:00. Antes de cada ciclo, espera até 5 min o banco responder. Se a máquina hibernar, o ciclo atrasado roda logo ao acordar.

**Códigos de saída:**

| Código | Significado                                                                       |
|--------|-----------------------------------------------------------------------------------|
| `0`    | Tudo coletado e gravado, sem alertas                                              |
| `1`    | Alguma fonte falhou, houve alerta de dados, banco inacessível ou erro inesperado  |
| `2`    | Erro de configuração ou de uso dos parâmetros                                     |
| `130`  | Interrompido (Ctrl+C ou `docker stop`)                                            |

### Backup do banco

A pasta `docker_postgre\backups` está montada em `/backups` dentro do container:

```powershell
docker exec swfast-postgres pg_dump -U swfast -d swfast   -Fc -f /backups/swfast.dump
docker exec swfast-postgres pg_dump -U swfast -d metabase -Fc -f /backups/metabase.dump
```

O backup do banco `metabase` guarda o dashboard e as edições manuais feitas nele.

---

## Desenvolvimento local e testes

Requer **Python 3.14**, a mesma versão da imagem Docker. O banco continua rodando no container (passo 3).

```powershell
cd C:\swfast\Dashboard_Swfast
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

python main.py          # usa o .env: POSTGRES_HOST=127.0.0.1
pytest -q
```

Use `127.0.0.1` e não `localhost` no `.env`: o container publica a porta só em IPv4, e `localhost` tenta primeiro o IPv6 (`::1`), o que atrasa a conexão em 10 s.

Os testes de integração de `tests/test_banco.py` sobem um Postgres temporário com **testcontainers** e são pulados automaticamente se o Docker não estiver rodando.

### Onde mexer para cada tipo de mudança

| Quero...                                       | Arquivo                       |
|------------------------------------------------|-------------------------------|
| Adicionar/desligar uma fonte da API            | `swfast/catalogo.py`          |
| Mudar colunas, tipos ou chaves de uma tabela   | `swfast/esquemas.py`          |
| Mudar uma regra de negócio do dashboard        | `swfast/visoes.py`            |
| Mudar perguntas, gráficos ou abas do dashboard | `swfast/metabase.py`          |
| Mudar retentativas/timeout                     | `.env` (ou `swfast/cliente.py`) |

> Mudar uma coluna que **já existe** em `esquemas.py` não altera a tabela no banco, porque o código usa `CREATE TABLE IF NOT EXISTS`. Nesse caso é preciso rodar um `ALTER TABLE` à mão ou apagar a tabela e recarregar o histórico.

---

## Referência de configuração

### `.env` (ETL)

| Variável                     | Padrão                                    | Descrição                                                      |
|------------------------------|-------------------------------------------|----------------------------------------------------------------|
| `SWFAST_API_KEY`             | **obrigatória**                           | Chave da API Swfast v5                                         |
| `SWFAST_URL`                 | `https://analytics-api.swfast.com.br/v5`  | URL base da API                                                |
| `DIAS_POR_JANELA`            | `7`                                       | Tamanho da janela de coleta (1 a 7)                            |
| `INTERVALO_HORAS`            | `3`                                       | Intervalo do modo contínuo; precisa dividir 24                 |
| `INTERVALO_ENTRE_CHAMADAS`   | `5`                                       | Pausa, em segundos, entre uma fonte e outra                    |
| `TIMEOUT_SEGUNDOS`           | `120`                                     | Timeout de cada requisição                                     |
| `TENTATIVAS`                 | `3`                                       | Tentativas em erros temporários (429/5xx/rede)                 |
| `ESPERA_INICIAL_RETENTATIVA` | `2`                                       | Primeira espera entre tentativas (dobra a cada nova tentativa) |
| `POSTGRES_HOST`              | `localhost`                               | Use `127.0.0.1` no Windows; o container sobrescreve            |
| `POSTGRES_PORT`              | `5432`                                    | Porta publicada do Postgres                                    |
| `POSTGRES_DB`                | **obrigatória**                           | Igual ao `docker_postgre\.env.postgre`                         |
| `POSTGRES_USER`              | **obrigatória**                           | Igual ao `docker_postgre\.env.postgre`                         |
| `POSTGRES_PASSWORD`          | **obrigatória**                           | Igual ao `docker_postgre\.env.postgre`                         |
| `DIRETORIO_DADOS`            | `data`                                    | Onde ficam `_manifesto/` e `_logs/`                            |
| `LISTA_DE_LOJAS`             | vazio                                     | Filtro de lojas (só para lojas centrais, só em `vendas`)       |
| `USAR_LOJA_ID_EXTERNO`       | `true`                                    | Se os códigos de `LISTA_DE_LOJAS` são IDs externos             |

### `.env.meta` (Metabase)

| Variável                  | Padrão                   | Descrição                                          |
|---------------------------|--------------------------|----------------------------------------------------|
| `METABASE_ADMIN_EMAIL`    | **obrigatória**          | Login do administrador do Metabase                 |
| `METABASE_ADMIN_SENHA`    | **obrigatória**          | Senha do administrador                             |
| `MB_DB_USER`              | `metabase_app`           | Dono do banco interno `metabase`                   |
| `MB_DB_PASS`              | **obrigatória**          | Senha desse usuário                                |
| `METABASE_LEITOR_USUARIO` | `metabase_leitor`        | Usuário somente leitura do banco `swfast`          |
| `METABASE_LEITOR_SENHA`   | **obrigatória**          | Senha desse usuário                                |
| `METABASE_URL`            | `http://metabase:3000`   | Endereço do Metabase visto pelo container          |
| `METABASE_URL_PUBLICA`    | `http://127.0.0.1:3000`  | Endereço que o navegador do cliente usa            |

### `docker_postgre/.env.postgre` (banco)

| Variável            | Descrição                                                            |
|---------------------|----------------------------------------------------------------------|
| `POSTGRES_DB`       | Nome do banco de dados criado na primeira subida                     |
| `POSTGRES_USER`     | Usuário dono do banco (superusuário do Postgres)                     |
| `POSTGRES_PASSWORD` | Senha desse usuário                                                  |

As três só valem na primeira subida, quando o volume ainda está vazio.

`.env`, `.env.meta` e `docker_postgre/.env.postgre` contêm senhas e estão no `.gitignore`. **Nunca faça commit desses arquivos.**

---

## Solução de problemas

| Sintoma                                                                 | Causa provável / solução                                                                                       |
|-------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------|
| `network dashboard-swfast_default declared as external, but could not be found` | O banco não foi iniciado. Rode `docker compose up -d` em `docker_postgre` primeiro.                     |
| `env file ...\.env.postgre not found`                                   | Falta criar o `docker_postgre\.env.postgre` a partir do `.env.postgre.example` (passo 3).                      |
| Log: `usuário ou senha recusados`                                       | O `.env` do ETL não bate com o do banco, ou a senha foi trocada depois de criar o volume (ver passo 3).        |
| Log: `Postgres ... inacessível`                                         | O container `swfast-postgres` está parado: `docker start swfast-postgres`.                                     |
| Log: `401`/`403 ... Confira a API-Key`                                  | Chave errada, expirada ou sem acesso ao endpoint.                                                              |
| Log: `campos novos no JSON` ou `campos da planilha ausentes`            | A API mudou o formato. Atualize `swfast/esquemas.py`. O ciclo sai com código 1 (alerta).                       |
| `swfast-metabase` reiniciando sem parar                                 | Normal antes do `instalar-dashboard`, que cria o banco `metabase`. Rode o passo 6.                             |
| Atalho diz "O dashboard ainda não foi instalado"                        | Falta o `-Instalar` do passo 7, ou `data\dashboard_url.txt` não existe.                                        |
| Atalho diz "O dashboard não respondeu"                                  | O Docker não subiu. Abra o Docker Desktop (A) ou confira a tarefa agendada (B). Veja `%USERPROFILE%\SwfastDashboard\abrir_dashboard.log`. |
| Horários do agendamento errados                                         | Fuso: todos os containers usam `TZ=America/Recife`. Se o cliente estiver em outro fuso, ajuste nos três composes. |

### Observações

- O `init/01-schemas.sql` do banco cria os schemas `raw`, `staging` e `marts`, mas o pipeline atual grava tudo no schema `public`. Esses schemas ficam vazios.
- As pastas `data/raw/` e `data/staging/`, quando existem, são de uma versão anterior do pipeline que gravava arquivos intermediários. O código atual não as gera nem as lê.
