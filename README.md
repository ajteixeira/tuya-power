# Tuya Power

Site web (FastAPI + HTMX + SQLite) para gerir os dispositivos Tuya de casa, com
histórico local completo, estado online, controlo, configuração e consulta de registos.

## Funcionalidades

1. **Histórico local:** descarga incremental e sem duplicados para SQLite, de hora a hora (cron) e
   1x por dia pelo próprio site (predefinição 03:00), mais o botão manual «Sincronizar agora».
2. **Estado online** de cada dispositivo (atualização a cada 5 s).
3. **Ligar, desligar e ajustar** (interruptores, números, listas, texto), com os controlos
   bloqueados até se carregar em «Editar».
4. **Configuração:** as funções configuráveis declaradas pela Tuya.
5. **Alarmes do disjuntor:** descodificação e edição dos limites (sobrecorrente, tensão,
   temperatura, potência).
6. **Histórico e registos:** gráfico por dia, mês ou ano (com navegação) e tabela paginada de registos/consumos, com
   filtros por data/hora, tipo, código (escolha múltipla) e valor mínimo/máximo.

## Interface

A página de cada dispositivo tem cinco separadores (guardados no endereço, por exemplo
`#alarmes`, e lembrados entre visitas):

| Separador | Conteúdo |
|---|---|
| **Estado** | Mosaicos com os valores principais (potência, tensão, corrente, energia, temperatura, humidade, bateria, fases A/B/C…), etiquetas Ligado/Desligado e a secção recolhida «Outros parâmetros» (mantém-se aberta ou fechada entre atualizações). |
| **Configuração** | Controlos de cada função do dispositivo. |
| **Alarmes** | Tabela editável dos limites do disjuntor (ver abaixo), com os valores brutos recolhidos. |
| **Histórico** | Gráfico de um código numérico à escolha (menu com nomes em português), com os separadores **Dia**, **Mês** e **Ano** e as setas `‹ ›` para navegar entre períodos. O eixo vertical mostra o nome e a unidade. Para a energia há barras por período e um cartão com o total (ver abaixo). |
| **Registos** | Tabela paginada de todos os registos gravados, com filtros. |

**Energia de hoje no Estado.** O separador *Estado* mostra o consumo (e, onde existe, a injeção) do dia
corrente, desde as 00:00 no fuso do navegador (enviado em cada atualização):

- **Contadores acumulados** (por exemplo o «Geral»): mosaicos «Energia consumida hoje» e «Energia
  injetada hoje» = valor atual do contador (lido da Tuya em cada atualização, sem atraso) menos o
  último valor gravado antes das 00:00. Se não houver nenhum valor anterior a hoje, parte-se da
  primeira leitura de hoje. A «Energia (saldo)» (`balance_energy`) passou para «Outros parâmetros».
- **Incrementos** (`add_ele`, por exemplo o «Termo acumulador Solar»): mosaico «Energia consumida
  hoje (até HH:MM)» = soma dos incrementos gravados hoje. Vem da base local, por isso pode estar até
  uma hora atrasado (a hora indicada é a do último registo).

**Histórico: Dia, Mês e Ano.** Os separadores escolhem o período mostrado e as setas `‹ ›` passam ao
anterior/seguinte (a seta seguinte fica desativada no futuro). O eixo mostra sempre o período
inteiro, no fuso do navegador:

| Separador | Período | Barras de energia |
|---|---|---|
| **Dia** | o dia escolhido (00:00–24:00) | por **hora** |
| **Mês** | o mês escolhido | por **dia** |
| **Ano** | o ano escolhido | por **mês** |

Os outros códigos (potência, tensão, temperatura…) usam os mesmos separadores, em linha.

**Energia por período.** Para os códigos de energia o menu oferece «Energia consumida por período» e
«Energia injetada por período», desenhadas em barras. Os valores brutos de energia (`total_forward_energy`,
`reverse_energy_total` e `add_ele`) não se oferecem como gráfico: aparecem só no separador *Estado*. Há dois tipos
de código, com cálculos diferentes:

- **Contadores acumulados** (`total_forward_energy`, `reverse_energy_total`): cada barra é o último
  valor do contador nesse período menos o do período anterior. O servidor devolve também o último
  ponto anterior ao intervalo, para o primeiro período ter o consumo certo.
- **Incrementos** (`add_ele`, «electricity added»): cada valor reportado é a energia consumida desde
  o reporte anterior (não é um total), por isso cada barra é a **soma** dos valores do período.
  Confirmado com o «Termo acumulador Solar»: a soma diária coincide, dentro de ~1 %, com o integral
  da potência instantânea (`cur_power`). Por isso o `add_ele` aparece no Estado como
  «Energia (último incremento)» e só se oferece no gráfico «por período».

Um **cartão** no canto superior direito do painel mostra o **total do período** (soma das barras),
por exemplo «Energia consumida no dia (até agora): 16,12 kW·h»; fica escondido nas séries que não são
de energia. O primeiro período pode estar incompleto (a base local só tem histórico desde a primeira
sincronização) e, se o serviço estiver parado, o consumo desse intervalo fica todo na 1.ª barra
seguinte.

**Segurança dos comandos:** nos separadores *Configuração* e *Alarmes* há um botão vermelho
**Editar** que desbloqueia todos os controlos do grupo; «Cancelar» repõe os valores e volta a
bloquear. Depois de enviar um comando, o grupo volta a bloquear-se sozinho.

**Datas e horas:** são escritas pelo navegador, no formato e fuso do navegador (ordem do dia e do
mês conforme a língua), **sempre com relógio de 24 horas**. Os campos «Início» e «Fim» dos filtros
são controlos do próprio navegador, por isso o seu formato depende da língua do navegador.

## Estrutura

```
app/
  main.py        rotas web, autenticação, agendador
  tuya.py        cliente da Tuya Cloud OpenAPI (assinatura HMAC-SHA256)
  sync.py        descarga do histórico (INSERT OR IGNORE + UNIQUE)
  db.py          esquema SQLite
  config.py      definições lidas do .env
  templates/     páginas Jinja2/HTMX
scripts/sync_once.py   sincronização única (para depuração e cron)
scripts/cron_sync.sh   envoltório do cron (flock + registo em data/sync.log)
run.sh                 arranque do servidor (sem contentor)
Dockerfile             imagem do contentor (micromamba, sem pip)
docker-compose.yml     contentor: porta 8002 (127.0.0.1), dados em /mnt/storage1/data/tuya-power
deploy/94-tuya-power.conf  configuração do lighttpd (/power)
.vscode/               configurações de depuração
data/tuya.db           base de dados (criada automaticamente)
```

## Criar o projeto Cloud na plataforma Tuya

No menu *Cloud* da consola existem três opções: *Project Management* (criar e gerir projetos, credenciais e dispositivos), *Cloud Services* (subscrição das APIs/serviços) e *API Explorer* (testes). Os nomes podem variar ligeiramente consoante a versão da consola.

1. **Conta de developer:** entre em <https://platform.tuya.com> (ou registe-se) com a conta de developer.
2. **Criar o projeto:** *Cloud → Project Management → Create Cloud Project* e preencha:
   - *Project Name*: por exemplo `tuya-power`;
   - *Industry*: `Smart Home`;
   - *Development Method*: `Smart Home`;
   - *Data Center*: **Central Europe** ou **Western Europe**, consoante os dispositivos. O centro de
     dados tem de ser o mesmo da conta da app, caso contrário os dispositivos não aparecem.
     Endpoints: Central Europe `https://openapi.tuyaeu.com` (predefinição); Western Europe `https://openapi-weaz.tuya.com`. Vai para `TUYA_ENDPOINT`.
3. **Subscrever as APIs:** o assistente pede as APIs a autorizar. Se não o fizer, abra o projeto em
   *Cloud → Project Management*, separador *Service API*, ou use *Cloud → Cloud Services*. Subscreva (são gratuitas na versão de teste):
   - *Authorization Token Management*;
   - *IoT Core* (obrigatória: listagem, estado, especificações e comandos dos dispositivos);
   - *Device Log Query* (só se existir como API separada na sua consola; é usada para o histórico).
   *Device Status Notification* não é necessária: a aplicação consulta o estado por pedido (polling).
4. **Associar a conta da app Tuya Smart / Smart Life:**
   - *Devices → Link App Account → Add App Account*;
   - na app, abra *Eu → ícone de leitura de QR code* e leia o código apresentado;
   - escolha o acesso *Automatic Link* e confirme. Os dispositivos da conta passam a surgir em *All Devices*.
5. **Obter as credenciais:** em *Cloud → Project Management →* (o seu projeto) *→ Overview*, secção *Authorization Key* copie o **Access ID/Client ID** e o
   **Access Secret/Client Secret** para o `.env` (`TUYA_ACCESS_ID` e `TUYA_ACCESS_SECRET`).
6. **Testar:** na consola, *Cloud → API Explorer* permite chamar, por exemplo,
   `GET /v1.0/devices/{device_id}/status` e confirmar que a conta responde antes de usar o site.

### Limitações e erros frequentes

- **Subscrição de teste (Trial):** as APIs gratuitas podem expirar após algum tempo (historicamente
  1 a 6 meses) e têm de ser renovadas manualmente na consola. Quando expiram, a aplicação passa a
  devolver erros como `No permissions` (código `1106`) ou `API subscription expired`.
- **`IoT Core service subscription has expired` (28841002):** a subscrição de teste do IoT Core expirou.
  Em *Cloud → Cloud Services → IoT Core* use a opção de renovar/prolongar o período de teste
  (*Extend Trial Period*) e confirme o formulário. Depois clique de novo em «Sincronizar agora».
- **`sign invalid` (1004) ou `clientId is invalid` (1005):** credenciais erradas ou centro de dados
  diferente do endpoint configurado.
- **`permission deny` (1106):** a API não está subscrita ou a conta da app não está associada ao projeto.
- **Lista de dispositivos vazia:** conta da app não associada, ou centro de dados errado.

## Instalação

Usa-se apenas o conda do repositório comum (sem pip nem venv):

```bash
/mnt/storage1/conda/bin/conda env create -p /mnt/storage1/conda/envs/tuya-power -f environment.yml
cp .env.example .env
```

Edite o `.env`:

| Variável | Descrição |
|---|---|
| `TUYA_ACCESS_ID` / `TUYA_ACCESS_SECRET` | Credenciais do projeto Tuya |
| `TUYA_ENDPOINT` | Predefinição `https://openapi.tuyaeu.com` |
| `WEB_USER` / `WEB_PASSWORD` | Autenticação HTTP Basic. **Com a palavra-passe vazia o site fica aberto.** |
| `DB_PATH` | Caminho da base de dados (predefinição `data/tuya.db`) |
| `SYNC_HOUR` | Hora local da descarga diária feita pelo próprio site (predefinição 3) |
| `SYNC_IN_APP` | `true` (predefinição) mantém a sincronização diária do site; `false` se usar só o cron |
| `SYNC_INTERVAL_MINUTES` | `>0`: sincroniza de N em N minutos (por exemplo `60`, de hora a hora, com a 1.ª execução ao arrancar) em vez de 1x por dia; o `docker-compose.yml` usa 60 |
| `SYNC_INITIAL_DAYS` | Dias a descarregar na 1.ª sincronização (predefinição 7) |

## Execução

```bash
./run.sh        # http://<IP-da-máquina>:8080
```

Na primeira vez, clicar em **Sincronizar agora** para listar os dispositivos e
descarregar o histórico. O estado da última sincronização aparece na página inicial.

## Contentor (Docker)

A aplicação corre num contentor e fica publicada em `http://<servidor>/power` através do lighttpd. A
sincronização de hora a hora corre **dentro do contentor** (agendador da própria aplicação,
`SYNC_INTERVAL_MINUTES=60`), por isso não é preciso cron no anfitrião.

| | |
|---|---|
| Imagem | `ghcr.io/ajteixeira/tuya-power:latest` (também se constrói localmente com `docker-compose build`) |
| Porta | `127.0.0.1:8002` no anfitrião → `8080` no contentor (só o lighttpd lhe acede; mude com `PORT`) |
| Dados | `/mnt/storage1/data/tuya-power` → `/app/data` (a base de dados `tuya.db`, fora do contentor) |
| Configuração | o `.env` do projeto (`env_file`): credenciais Tuya, `WEB_USER`/`WEB_PASSWORD` |
| Utilizador | `1000:1000` (variáveis `UID`/`GID`), para os ficheiros dos dados ficarem do utilizador |
| Fuso | `TZ=Europe/Lisbon` |

```bash
docker-compose up -d --build     # constrói (ou, sem --build, usa a imagem) e arranca
docker-compose logs -f           # registos
docker-compose down              # pára (os dados ficam na pasta montada)
```

**Primeira instalação a partir de uma base existente:** copie a base para a pasta de dados com o
`backup` do SQLite (cópia consistente, mesmo com a base em uso):

```bash
python -c "import sqlite3;s=sqlite3.connect('data/tuya.db');d=sqlite3.connect('/mnt/storage1/data/tuya-power/tuya.db');s.backup(d)"
```

Se o contentor arrancar sem base de dados, cria uma vazia e só recupera os últimos 7 dias da Tuya.
Retire também o cron do anfitrião (`crontab -e`, apagar a linha do `cron_sync.sh`) para não haver
duas sincronizações.

### Publicar em /power (lighttpd)

Como as outras aplicações (`/ytd`, `/sc`), o lighttpd faz proxy para o contentor e envia o cabeçalho
`X-Forwarded-Prefix: /power`; a aplicação retira o prefixo do caminho e põe-no em todas as ligações
das páginas (também com a aplicação aberta diretamente, sem prefixo, tudo funciona).

```bash
sudo cp deploy/94-tuya-power.conf /etc/lighttpd/conf-available/
sudo lighty-enable-mod tuya-power
sudo systemctl reload lighttpd
```

### Imagem no GitHub Container Registry

```bash
docker login ghcr.io                      # utilizador do GitHub + token com o âmbito write:packages
docker build -t ghcr.io/ajteixeira/tuya-power:latest .
docker push ghcr.io/ajteixeira/tuya-power:latest
```

A imagem **não contém** o `.env` nem a base de dados (ver `.dockerignore`), só o código; as
credenciais e os dados entram em tempo de execução. Por predefinição um pacote novo no ghcr é privado.
Noutra máquina: `docker login ghcr.io` e `docker-compose up -d` (descarrega a imagem).

## Alarmes e limites do disjuntor (`alarm_set_*`)

Nos disjuntores (categoria Tuya `dlq`), os limites de proteção vêm em dois datapoints do tipo
**Raw** (`alarm_set_1` e `alarm_set_2`), enviados pela Tuya em base64. A Tuya não documenta o
formato; foi deduzido e validado com as capturas da app Tuya Smart.

Cada datapoint é uma sequência de registos de 4 bytes: `[tipo, corte, valor]`, com o valor em
16 bits big-endian. O byte «corte» corresponde ao interruptor *Breaker tripping* da app.

| Tipo | Alarme | Unidade | Estado |
|---|---|---|---|
| 1 | Sobrecorrente | A | confirmado |
| 3 | Sobretensão | V | confirmado |
| 4 | Subtensão | V | confirmado |
| 5 | Temperatura alta | °C | confirmado |
| 7 | Potência alta | kW | tipo confirmado; escala por confirmar |
| 2, 6 | desconhecidos | – | mostrados como «Tipo N» |

Exemplo: `AQEAEAMBAQkEAQBa` → `01 01 00 10 | 03 01 01 09 | 04 01 00 5a` →
sobrecorrente 16 A, sobretensão 265 V e subtensão 90 V, todas com corte ativo.

**Na interface (separador Alarmes):**
- Uma tabela com **Alarme**, **Limite** e **Corte do disjuntor**. Os campos ficam bloqueados até
  se carregar em «Editar»; um só botão **Aplicar alterações** envia o que mudou, depois de
  confirmação. Só são enviados os `alarm_set_N` cujo valor mudou (sem alterações não se envia nada).
- Valores fora de 0–65535 são recusados. Os tipos desconhecidos são reenviados tal como estão.
  Depois do envio, a tabela volta a ler o estado do dispositivo.
- A secção recolhida «Valores brutos» mostra o hexadecimal e o base64 de cada `alarm_set_N`.
- No **Estado** e nos **Registos** os alarmes aparecem descodificados (por exemplo
  `Sobretensão: 265 V · corte ativo`) e, nos registos, com os bytes originais entre parênteses retos.
  Qualquer outro datapoint Raw é mostrado em hexadecimal, com o base64 original.

**Fases A, B e C** (`phase_a`/`phase_b`/`phase_c`, por exemplo no «Geral»): formato padrão da Tuya de
8 bytes (tensão em décimas de volt, 2 B · corrente em mA, 3 B · potência em W, 3 B), mostrado como
`248.2 V · 5.935 A · 1345 W`. Não foi confirmado com a app; fases vazias aparecem como «—».

**Cuidados:**
- Isto altera proteções reais do disjuntor. Experimente primeiro com uma alteração segura e
  confirme na app Tuya Smart.
- A unidade/escala da potência alta ainda não está confirmada.
- Defina `WEB_PASSWORD`: sem ela, qualquer equipamento da rede pode alterar os limites.
- Para outro modelo de disjuntor, os tipos podem ser diferentes; ajuste `ALARM_TYPES` em
  `app/main.py`.

## Sincronização automática (cron, sem contentor)

Com o contentor, a sincronização horária é feita pela própria aplicação (ver acima) e este cron **não é
necessário**. Sem contentor, a descarga pode correr independentemente do site, todas as horas, através do cron:

```bash
crontab -e
# ao minuto 5 de cada hora
5 * * * * /home/l00ks/src/tuya-power/scripts/cron_sync.sh
```

- `scripts/cron_sync.sh` corre `scripts/sync_once.py` com `flock` (nunca há duas execuções
  sobrepostas) e acrescenta o resultado a `data/sync.log` (`tail data/sync.log`).
- O `sync_once.py` termina com código 1 se houver erro, e cada execução fica registada na tabela
  `sync_runs` (visível na página inicial).
- A descarga é incremental e sem duplicados, por isso correr com mais frequência é seguro.
  A Tuya limita a frequência de pedidos (erro `40000309`); a aplicação espera e repete sozinha.
- O site tem também uma sincronização diária própria (`SYNC_HOUR`). Se usar o cron, pode
  desligá-la com `SYNC_IN_APP=false` no `.env` (reiniciar o servidor); manter as duas é inofensivo.
- A 1.ª descarga completa é demorada (dezenas de minutos, consoante o número de registos dos
  últimos 7 dias); as seguintes demoram segundos.

## Depuração no VS Code

Abra a pasta `tuya-power` como raiz do espaço de trabalho (o interpretador é
`/mnt/storage1/conda/envs/tuya-power/bin/python`) e use o painel *Run and Debug*:

- **Tuya Power (uvicorn):** servidor com `--reload` em `0.0.0.0:8080`, lê o `.env`.
- **Tuya Power (sem reload):** igual, sem reload e a depurar também o código das bibliotecas;
  mais fiável para pontos de paragem em tarefas agendadas.
- **Sincronização única:** corre `scripts/sync_once.py` (depura `app/sync.py` e `app/tuya.py` sem servidor).

As configurações escutam em `0.0.0.0`, logo o site fica acessível na rede local.
Defina `WEB_PASSWORD` antes de depurar assim.

## Notas

- A Tuya guarda apenas cerca de 7 dias de registos. Se o serviço estiver parado mais do que isso,
  perde-se o intervalo em falta; a base local só fica completa daqui para a frente.
- **Dispositivos com modelo de objetos** (por exemplo sensores de temperatura e humidade, como o
  «Sótão»): a API padrão da Tuya responde `function not support (2003)` / `not support this device
  (2009)`. Nesses casos a aplicação usa a API do modelo de objetos (`/v2.0/cloud/thing/...`) para o
  estado, as especificações e os comandos. A API de registos só lhes devolve eventos online/offline;
  para terem histórico de valores, cada sincronização grava a **leitura atual** (hora do último
  reporte), pelo que o histórico tem uma leitura por sincronização (uma por hora com o cron).
- O gráfico só oferece códigos com valores numéricos (códigos Raw ou booleanos não se desenham).
- A Tuya não oferece, nas APIs a que o projeto tem acesso, consumos agregados por dia ou mês além
  dos 7 dias de registos; o histórico longo é o que a base local for acumulando.
- Os valores de potência, tensão, corrente e energia são mostrados com a escala e a unidade
  declaradas na especificação do dispositivo. Nos campos de configuração numéricos, o valor
  introduzido é enviado em bruto (sem escala).
- O HTMX e o Chart.js são carregados por CDN, por isso a interface precisa de acesso à internet.
- Se a sincronização falhar, a mensagem de erro fica registada na tabela `sync_runs` e aparece
  na página inicial.
