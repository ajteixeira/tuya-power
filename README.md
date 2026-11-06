# Tuya Power

Site web (FastAPI + HTMX + SQLite) para gerir os dispositivos Tuya de casa, com
histórico local completo, estado online, controlo, configuração e consulta de registos.

## Funcionalidades

1. **Histórico local:** descarga incremental e sem duplicados para SQLite, automática
   1x por dia (predefinição: 03:00) e manual através do botão «Sincronizar agora».
2. **Estado online** de cada dispositivo (atualização a cada 5 s).
3. **Ligar, desligar e ajustar** (interruptores, números, listas, texto).
4. **Configuração:** as funções configuráveis declaradas pela Tuya surgem na mesma lista de controlos.
5. **Histórico:** gráfico (24 h a 1 ano) e tabela paginada de registos/consumos, com filtros por
   data/hora, tipo, código (escolha múltipla) e valor mínimo/máximo.

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
run.sh                 arranque do servidor
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
| `SYNC_INITIAL_DAYS` | Dias a descarregar na 1.ª sincronização (predefinição 7) |

## Execução

```bash
./run.sh        # http://<IP-da-máquina>:8080
```

Na primeira vez, clicar em **Sincronizar agora** para listar os dispositivos e
descarregar o histórico. O estado da última sincronização aparece na página inicial.

## Sincronização automática (cron)

A descarga pode correr independentemente do site, todas as horas, através do cron:

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
- Os valores de potência, tensão, corrente e energia são mostrados com a escala e a unidade
  declaradas na especificação do dispositivo. Nos campos de configuração numéricos, o valor
  introduzido é enviado em bruto (sem escala).
- O HTMX e o Chart.js são carregados por CDN, por isso a interface precisa de acesso à internet.
- Se a sincronização falhar, a mensagem de erro fica registada na tabela `sync_runs` e aparece
  na página inicial.
