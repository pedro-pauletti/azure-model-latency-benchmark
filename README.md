# Azure model latency benchmark

Benchmark controlado de latencia no Azure, com painel HTML interativo e dois
cenarios independentes:

- **Geracao:** Azure OpenAI Global Standard PAYG, comparando
  `service_tier=default` (Standard) e `service_tier=priority` (Priority).
- **Selecao de tools:** function calling nativo de LLMs versus
  Microsoft-Decision-1, com um agente simples de roteamento.

Execute localmente ou em um **Azure Container Apps Job**. Ajuste tokens,
modelos, tiers, repeticoes, warmups e intervalos; acompanhe o progresso,
filtre e ordene resultados e exporte CSVs e imagens PNG.

> **Custo e limites:** provisionar infraestrutura e executar inferencias pode
> gerar cobranca. Calibracao, warmups, Priority, ACR, Storage e builds tambem
> podem gerar custo. Os scripts nao estimam precos. Resultados sao referencias
> de um ambiente controlado, nao SLA, previsao de producao ou ranking geral
> de qualidade. Global Standard nao garante inferencia na regiao do recurso.

## Requisitos

- Python 3.11+ e Azure CLI.
- Conta Foundry e projeto existentes na subscription configurada.
- Modelos/versoes, quota e suporte a Priority disponiveis nessa subscription
  e regiao. O catalogo de exemplo **nao garante disponibilidade**.
- Permissoes ARM para consultar/provisionar e atribuir as roles necessarias,
  quando for provisionar. Para inferencia OpenAI, use
  **Cognitive Services OpenAI User** no recurso.
- Microsoft-Decision-1 e sua permissao especifica sao necessarios para a
  segunda aba, nao para um benchmark somente de geracao.

O provisionador de modelos cria deployments ausentes; **nao cria** a conta
Foundry, o projeto nem o resource group. Ele recusa sobrescrever deployments
existentes com parametros divergentes.

## Inicio rapido (PowerShell)

Na raiz deste repositorio:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

Copy-Item .\benchmark.example.json .\benchmark.json
Copy-Item .\tool-benchmark.example.json .\tool-benchmark.json
```

Edite os dois arquivos locais antes de qualquer chamada Azure:

1. Em `benchmark.json`, preencha `subscription_id`, `resource_group`,
   `account`, `project`, `location` e `endpoint`.
2. Selecione apenas os modelos/versoes disponiveis para seu ambiente e
   ajuste `capacity` conforme a quota. Os deployments usam o nome do modelo.
3. Para tools, ajuste o endpoint e deployment em `tool-benchmark.json`.
   O alias padrao e `decision-1`; nao use `Microsoft` no nome do deployment.

Os endpoints dos exemplos contem `YOUR_FOUNDRY_ACCOUNT`: substitua pelo
endpoint correto do seu recurso. A subscription com zeros e apenas um
placeholder. **Nao use chaves de API**: a autenticacao local utiliza Entra
via Azure CLI; o worker Azure utiliza identidade gerenciada.

```powershell
az login
az account set --subscription "<SUA_SUBSCRIPTION_ID>"

# Consulta modelos, versoes, SKU, quota e capacidade; nao cria deployments.
python .\provision.py

# Opcional: cria somente os deployments ausentes.
python .\provision.py --apply

# Inicia o painel apenas em loopback; ainda nao envia inferencias.
python .\server.py
```

Abra <http://127.0.0.1:8765/>. A aba de geracao funciona independentemente
de o modelo Decision estar provisionado. Revise o volume estimado e confirme
a cobranca antes de iniciar uma execucao.

## Executar e personalizar a geracao

Sem `--execute`, o script mostra somente o plano, sem enviar inferencias:

```powershell
python .\benchmark.py --models gpt-4.1 `
  --input-tokens 512 --output-tokens 128 --repetitions 1 --warmups 1

# Exemplo faturavel: contexto longo e resposta curta, dois tiers.
python .\benchmark.py --models gpt-4.1 gpt-5.4-mini `
  --input-tokens 7500 --output-tokens 300 `
  --repetitions 30 --warmups 3 --interval 1 --timeout 180 --execute

# Um tier ou uma matriz de tamanhos:
python .\benchmark.py --models gpt-4.1 --tiers priority `
  --input-tokens 512 4096 --output-tokens 128 512 `
  --repetitions 10 --warmups 1 --interval 3 --execute
```

No painel, sliders e campos numericos ajustam input/output, modelos, tiers,
repeticoes, warmups, intervalo, timeout e executor. Mudar controles, filtros
ou ordenacao nao envia inferencias. Standard e Priority ficam juntos por
modelo; os dados de cada execucao preservam sua configuracao historica.

**Cancelar apos chamada atual** impede as proximas chamadas, sem desfazer
a cobranca da chamada em andamento. Fechar apenas o navegador nao cancela:
mantenha o backend aberto e use o controle de cancelamento.

### Controles e metricas

- Uma requisicao por vez; modelos e tiers intercalados em ordem
  pseudoaleatoria, seed 42, com nonce novo por chamada e sem retries.
- Calibracao do input pelo `usage` real, warmups separados das medicoes e
  validacao do tier efetivamente retornado.
- Somente respostas bem-sucedidas com tokens exatos, cache zero,
  reasoning zero e tier correspondente entram nas latencias comparaveis.
  Erros, contadores ausentes e saidas curtas sao preservados, nao viram zero
  nem sao repostos seletivamente.
- GPT-5/6 usam `reasoning_effort=none` e `verbosity=low` no exemplo; GPT-4.1
  omite esses parametros. Cada modelo deve aceitar os parametros configurados.
- `max_completion_tokens` e **um teto, nao um minimo**. O GPT-5.5, por exemplo,
  apresentou encerramento antecipado no prompt sintetico em uma coleta e
  reteste; isso nao significa indisponibilidade ou incapacidade geral do modelo.
  Inspecione exclusoes e tamanho da amostra antes de comparar.

Metricas incluem headers, primeiro SSE, TTFT, TTLT, janela de geracao,
tempo total HTTP, cauda, TPOT, tokens/s e gaps entre chunks. Os agregados
incluem media, p50/p90/p95/p99, minimo/maximo, desvio padrao e CV, com
contagem por metrica. Checkpoints internos so aparecem se retornados pelo
servico; nao sao estimados nem misturados com outra modalidade de chamada.

Ganho positivo favorece Priority; negativo indica Priority mais lento
naquela coleta. Nao representa vantagem fixa, causalidade ou significancia
estatistica. Percentis de cauda com poucas amostras sao exploratorios.
Nao some medianas de componentes como se fossem a mediana do total.

## Agente e selecao de tools

```powershell
# Preflight sem criar o deployment Decision.
python .\provision_decision.py

# Opcional: cria o deployment e concede a data action especifica ao operador.
python .\provision_decision.py --apply --grant-inference

# Plano sem inferencia; --execute torna a execucao faturavel.
python .\tool_benchmark.py
```

Prefira o painel para selecionar modelos, tiers e 3/6/9/12 casos balanceados,
repeticoes, warmups, padding, intervalo e timeout. A API Decision exige a
data action `Microsoft.CognitiveServices/accounts/AIServices/providers/action`,
que nao integra a role OpenAI User. O provisionador usa uma role customizada
restrita ao recurso. Propagacao de RBAC pode demorar; revalide o catalogo
e as condicoes de uso/preview do modelo antes de provisionar.

O agente escolhe uma das tres tools locais: `route_billing`,
`route_technical` ou `route_account`. Sao funcoes deterministicas, sem
efeitos externos e com argumentos `{}`. Os LLMs usam function calling nativo,
schemas estritos, `tool_choice=required`, `parallel_tool_calls=false`,
`stream=false` e cap de saida 128.

Decision usa a API `choice`, nao Chat Completions ou function calling nativo.
Um adaptador transforma a escolha na mesma chamada local validada.
Nao existe tier Priority nesse caminho; ele nao e executado duas vezes
nem recebe uma confirmacao de Priority inventada.

| Metrica | Significado |
| --- | --- |
| `headers_ms` | Inicio HTTP ate headers. |
| `http_total_ms` | Requisicao e corpo completos, sem streaming. |
| `selection_ms` | Inicio HTTP ate selecao completa e chamada validada. |
| `tool_ms` | Dispatch e execucao da tool simulada local. |
| `agent_total_ms` | Inicio HTTP ate retorno da tool, sem resposta final LLM. |

Acuracia considera todas as tentativas, inclusive erros. Latencias usam
somente escolhas corretas/comparaveis. Cache/reasoning conhecidos e positivos
excluem a amostra; contadores ausentes no Decision permanecem desconhecidos,
nao zero. Mesmo padding nao significa igualdade de tokens faturados entre
protocolos/modelos: os contadores reais sao reportados por chamada.

Este cenario nao equivale ao workload 7.500/300, nao mede ferramentas reais,
RAG, argumentos complexos, Foundry Agent Service ou planejamento multi-etapa.
Acerto nos casos sinteticos nao comprova qualidade geral ou seguranca.

## Executar no Azure Container Apps

O dashboard continua local, sem ingress publico para iniciar benchmarks.
O provisionador cria, no RG configurado, ACR, Storage, Network Security
Perimeter, identidade gerenciada, ambiente Consumption e Job manual.
Exige permissoes de criacao de recursos e atribuicao das roles.

```powershell
# Validacao ARM; sem provisionar recursos ou enviar inferencias.
python .\deploy_containerapps.py --operator-ip "<SEU_IP_PUBLICO_DE_SAIDA>"

# Provisionamento e build remoto no ACR; nao exige Docker local.
python .\deploy_containerapps.py `
  --operator-ip "<SEU_IP_PUBLICO_DE_SAIDA>" --apply --build

# Reinicie o dashboard para carregar containerapps.json gerado localmente.
python .\server.py
```

Cada execucao usa 1 vCPU / 2 GiB, uma replica, paralelismo 1 e zero retries.
O startup/pull da imagem fica fora dos tempos HTTP. A imagem e fixada por
digest; o manifesto registra regiao, imagem, recursos e identidade da
execucao. Uma escolha Azure nunca faz fallback silencioso para a maquina local.

O Storage usa NSP **Enforced**, `SecuredByPerimeter`, sem chaves compartilhadas,
SAS ou acesso anonimo. A regra do operador admite somente seu IP publico
de saida; a regra de identidades da subscription e de rede, nao concede
acesso aos dados. RBAC Blob fica restrito ao container. Nao sao criadas
excecoes de policy. Um lease impede workers simultaneos e uma marca de run ID
impede repetir a mesma execucao faturavel.

**VPN/proxy:** confirme o IP efetivamente usado para acessar o Storage.
Um servico generico de consulta de IP pode usar outra rota. Se o caminho
de saida for rotativo ou bloqueado, use uma conexao aprovada e estavel;
nao abra faixas amplas nem enfraqueca o perimetro para contornar o bloqueio.
O provisionador verifica o transporte Blob antes de habilitar o executor.

ACR/Storage permanecem provisionados entre coletas. Remova os recursos
dedicados pelo seu processo de governanca quando nao forem mais necessarios.

## Resultados, exportacao e privacidade

`results\<run-id>` armazena geracao; `tool-results\<run-id>` armazena tools.
Os arquivos incluem `run.json`, `requests.jsonl`, CSVs e resumos.
Nao misture execucoes, dimensoes ou origens local/Azure.

O painel oferece filtros/ordenacao apos a coleta, Standard/Priority
adjacentes, contexto integrado, detalhes tecnicos recolhidos, modo captura,
CSV e PNG. Tokens usam `usage` real; cache/reasoning ja integram input/output
e nao sao somados duas vezes. Totais parciais exibem cobertura do usage.

```powershell
# Snapshot offline autocontido, sem novas inferencias.
python .\dashboard.py
Start-Process .\results\dashboard.html
```

Snapshots e exportacoes podem conter metadados do seu ambiente. Revise-os
antes de compartilhar. Este repositorio nao inclui credenciais, configuracao
Azure pessoal, historicos brutos, resultados de clientes ou dependencias
instaladas. Os arquivos locais de configuracao/resultados estao no `.gitignore`.

**Nao publique o servidor loopback nem use port forwarding.** Ele valida
Host/Origin e token de controle nos POSTs, sem liberar CORS. A autenticacao
Azure fica no processo Python, nunca no HTML.

## Testes

Depois de copiar os exemplos de configuracao:

```powershell
python -m pip install -r requirements-dev.txt
# Testes unitarios com fixtures/mocks.
python -m unittest test_benchmark test_cloud test_dashboard test_server test_tools -v

# Suite completa, incluindo interface: requer Microsoft Edge instalado.
python -m unittest discover -p "test_*.py" -v
```

Os testes usam fixtures/mocks; nao devem provisionar Azure nem enviar
inferencias faturaveis.

## Referencias

- [Microsoft-Decision-1](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision)
