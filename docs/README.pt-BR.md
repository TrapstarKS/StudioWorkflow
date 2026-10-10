# StudioWorkflow

**Busque e edite scripts do Roblox por arquivos locais, sem usar MCP para cada leitura ou alteração.**

[README em inglês](../README.md) · [Skill](../studioworkflow/skill/SKILL.md) · [Comandos e recuperação](workflow.md)

Um plugin local conversa com um serviço Python e sincroniza scripts entre Studio e arquivos `.luau`. O agente lê só os trechos necessários; a sincronização roda em segundo plano, sem chamadas de IA nem de MCP. O trabalho do agente ainda consome tokens, e a economia depende do tamanho e da quantidade de leituras.

O núcleo não tem dependências Python externas. Não precisa de API key do Roblox, Lune, Rojo ou MCP para sincronizar fontes. Inclui proteção contra conflitos, histórico, várias places, busca de alterações locais pendentes e leitura por intervalo de linhas.

## Pedir para o agente configurar

```text
Instale https://github.com/TrapstarKS/StudioWorkflow seguindo o README.
Use uma venv isolada. Pasta da ferramenta: <caminho fixo>.
Pasta do jogo: <caminho fixo>. Places: lobby=<PLACE_ID>, arena=<PLACE_ID>.
Instale a skill para <codex|claude|both> e configure início no login.
Preserve as instruções do projeto e rode doctor.
Me diga qual etapa de reiniciar o Studio/aceitar permissões falta.
Não publique o jogo.
```

## Instalação manual

Instale **Roblox Studio, Python 3.11+ e Git**. Escolha uma pasta fixa para a ferramenta e outra para os arquivos do jogo. Evite pastas sincronizadas por iCloud/OneDrive e não compartilhe uma pasta de trabalho ativa entre computadores.

No macOS:

```sh
git clone https://github.com/TrapstarKS/StudioWorkflow.git
cd StudioWorkflow
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m studioworkflow --root "$HOME/Roblox/MeuJogo" init --place lobby=123456 --agent both --startup
```

No Windows, pelo PowerShell:

```powershell
git clone https://github.com/TrapstarKS/StudioWorkflow.git
cd StudioWorkflow
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m studioworkflow --root "$env:USERPROFILE\Roblox\MeuJogo" init --place lobby=123456 --agent both --startup
```

Troque `123456` pelo **Place ID real**, não pelo Universe ID. Para várias places, repita `--place arena=987654`. Uma pasta pertence a um conjunto fixo de places; use outra pasta para outro jogo.

Para descobrir o ID, abra a place no Studio e rode `print(game.PlaceId)` na Command Bar. Se retornar `0`, abra uma place existente que já tenha ID; a configuração não suporta places sem ID.

`--agent both` instala somente a skill do StudioWorkflow e acrescenta um bloco pequeno às instruções locais. Pode escolher `codex`, `claude`, ou omitir para uso manual. Nenhuma skill de outro jogo é instalada.

`--startup` inicia o serviço e configura início no login. Sem essa opção, inicie com `run` no terminal. Sem `--place`, o setup interativo pergunta os nomes e IDs; agentes devem informar os IDs no comando.

Depois, salve seu trabalho e reabra o Studio. Abra uma place configurada em Edit mode e aceite as permissões do plugin, se aparecerem. Rode `doctor` usando o mesmo Python da instalação. A place aberta deve ficar `ready`; os scripts aparecem em `studio/lobby/Files/`.

Para os comandos curtos abaixo, ative a `.venv` ou substitua `studioworkflow` pelo mesmo Python seguido de `-m studioworkflow`. Rode dentro da pasta do jogo ou use `--root /caminho/MeuJogo` antes do subcomando. A skill instalada também encontra o comando pelo registro da instalação.

## Uso

```sh
studioworkflow search "Inventory" --place lobby --limit 5
studioworkflow read --place lobby --path ReplicatedStorage/Inventory.luau --start 20 --lines 40
```

A busca padrão usa `Files/`, incluindo alterações ainda não enviadas. `--observed` usa o índice da última fonte observada no Studio. A saída traz trechos curtos e o estado da conexão. `read` limita as linhas/caracteres e informa se cortou o resultado. `rg` também funciona para busca exata nos arquivos locais.

Leia a versão atual de `Files/`, faça a alteração e salve. Scripts existentes e sem ambiguidade são enviados após o debounce. Para trabalho longo, vários salvamentos ou edição concorrente:

```sh
studioworkflow checkout --place lobby --path ReplicatedStorage/Inventory.luau
# Agora leia e edite o arquivo em Files/.
studioworkflow apply --place lobby --path ReplicatedStorage/Inventory.luau
```

`checkout` deve vir **antes da leitura**: segura a base e pausa o envio. `apply` verifica concorrência e espera brevemente o resultado. Um save bem-sucedido não confirma entrega ao Studio.

Se houver divergência, `rebase` prepara um merge e mantém a alteração retida para revisão. Depois de revisar, use `apply`. Operações incertas devem ser investigadas; não rode retries cegos. `doctor`, `status`, `changes`, `operation` e `history` ajudam no diagnóstico.

Play, offline, inventário incompleto e conflito bloqueiam envios. Não edite `Source/`, índices ou histórico como código do jogo. Criar/apagar arquivo no disco não cria/apaga instância no Studio. Assets, hierarquia e testes de gameplay continuam sendo trabalho do Studio ou de outra ferramenta.

## Configurar, atualizar e remover

`studio-workflow.json` fica na pasta do jogo. `init --no-auto-sync` desliga envio automático; `--debounce 2` muda a espera; `--port 8647` fixa a porta; `--plugins-dir` indica a pasta real de plugins. Repetir `init` preserva identidade, chave, IDs e opções existentes. Alterar porta exige reiniciar serviço e Studio.

Para atualizar, rode `uninstall`, atualize o checkout com `git pull --ff-only`, reinstale com o mesmo Python e execute `init --agent both --startup` na mesma pasta do jogo. Reabra o Studio. A fonte, configuração, chave e histórico são preservados. A remoção também preserva a skill e os blocos de instrução; retire esses blocos marcados manualmente se não for usar mais.

O setup adiciona exclusões ao `.gitignore`. Não compartilhe `.studio-workflow/`, plugins gerados, configuração ou logs: podem conter chave privada e código do seu jogo. Os logs do supervisor/serviço ficam em `.studio-workflow/` e são rotacionados; o histórico cresce com as alterações.

Registros de scripts têm limite de 16 MB de JSON UTF-8. Nomes ambíguos não aceitam envio; colisões de caminhos interrompem o inventário. A sessão precisa receber uma mudança de Team Create para observá-la. Reconectar atualiza o inventário completo.

Os testes cobrem protocolo HTTP real em localhost e o plugin com serviços Roblox simulados. Permissões do Studio, início no login e Team Create precisam ser conferidos na máquina de destino. Veja o [README completo](../README.md) para detalhes e limites.
