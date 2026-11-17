# Azure model latency benchmark

A controlled, client-observed latency benchmark with a live HTML dashboard and
two independent workloads:

1. **Text generation:** compare Azure OpenAI models using the same input/output
   token targets, with Standard and Priority processing on Global Standard
   pay-as-you-go deployments.
2. **Tool selection:** compare native LLM function calling with
   Microsoft-Decision-1 using a small, single-step routing agent.

Run inference on your workstation or in an **Azure Container Apps Job**. Adjust
parameters in the dashboard, observe progress, keep Standard/Priority results
together, sort and filter completed runs, and export CSVs and PNGs.

**This is a measurement tool, not a published performance leaderboard.** No
customer results, original Azure configuration, or execution histories are
bundled. The dashboard and some runtime messages are currently in Portuguese;
this guide is in English.

> **Billing:** inference, calibration, warmups, Priority processing, ACR builds,
> container execution, and persistent Azure resources can incur charges.
> Benchmark CLIs require `--execute` to send inference; provisioning uses
> `--apply`. These are different switches. There is no pricing estimator or
> automatic resource cleanup. Review each plan before proceeding.

## Contents

- [What is being compared](#what-is-being-compared)
- [Requirements and Azure preparation](#requirements-and-azure-preparation)
- [Install and configure](#install-and-configure)
- [Verify and provision model deployments](#verify-and-provision-model-deployments)
- [Use the live dashboard](#use-the-live-dashboard)
- [Run generation benchmarks from the CLI](#run-generation-benchmarks-from-the-cli)
- [Run the tool-selection benchmark](#run-the-tool-selection-benchmark)
- [Run inference in Azure Container Apps](#run-inference-in-azure-container-apps)
- [Metrics and interpretation](#metrics-and-interpretation)
- [Results, exports, and reproducibility](#results-exports-and-reproducibility)
- [Troubleshooting](#troubleshooting)
- [Privacy and safe sharing](#privacy-and-safe-sharing)
- [Tests and repository map](#tests-and-repository-map)

## What is being compared

| Workload | Protocol | Comparison | What completes a measurement |
| --- | --- | --- | --- |
| Generation | Streaming Chat Completions | Same model and token targets, `service_tier=default` vs. `priority` | End of the HTTP streaming operation |
| Tool selection: LLM | Non-streaming Chat Completions with native function calling | LLM models, Standard and Priority | A validated selection, followed by one simulated local tool |
| Tool selection: Decision | Decision typed `choice` API, adapted to the same local tool call | One Decision profile, **without Priority** | A validated choice, followed by the same simulated local tool |

Standard and Priority use **the same deployment**, not separately provisioned
model copies. The generation provisioner enables Priority on a `GlobalStandard`
deployment; each inference request explicitly selects its processing tier. The
returned tier is checked, rather than assuming the requested tier was honored.

Global Standard routing is global. A resource and benchmark worker in Brazil
South do **not** establish that model inference happened in Brazil South.
Container Apps reduces workstation/network variability; it does not isolate the
model service or turn these measurements into a production SLA.

## Requirements and Azure preparation

- Python **3.11+**, Git, and Azure CLI. Commands below use **PowerShell** from the
  repository root. The container uses Python 3.13.
- An Azure subscription with an **existing resource group, Microsoft Foundry
  resource, and Foundry project**. A legacy standalone OpenAI resource without
  the required Foundry project is not the complete prerequisite.
- The exact model versions, `GlobalStandard` SKU, Priority support, and sufficient
  subscription quota/regional capacity. The example catalog is **not** an
  availability guarantee.
- Network access from the execution host to the configured endpoints. Existing
  Foundry firewall/private-endpoint restrictions are not removed by these scripts.
- For local OpenAI inference: **Cognitive Services OpenAI User** on the Foundry
  account, or an approved equivalent.
- For deployment: ARM permissions to inspect/create the relevant resources.
  Custom-role creation and role assignments require separate RBAC permissions;
  resource Contributor alone is not sufficient for every setup step.
- For Decision: its provider inference data action, described below.
- For the full browser test suite: Microsoft Edge and the development dependencies.
  Edge is not needed to run the ordinary dashboard in your preferred browser.

Create the base Foundry resource/project through your organization's approved
Azure process first. The model scripts **do not create** the resource group,
Foundry account, or project. The Container Apps helper creates supporting runner
infrastructure in the configured resource group.

Authenticate as the intended operator:

```powershell
az login
az account set --subscription "<YOUR_SUBSCRIPTION_ID>"
az account show --query "{subscription:id,name:name}" --output table
```

Authentication uses **Microsoft Entra ID**, not API keys. Local processes use
Azure CLI credentials; the Azure worker uses a user-assigned managed identity.
Do not paste tokens or credentials into either JSON configuration.

If needed, have an authorized administrator grant the local operator OpenAI
inference access. For a human operator, the assignment can be scoped as follows:

```powershell
$AccountId = az cognitiveservices account show `
  --resource-group "<YOUR_RESOURCE_GROUP>" --name "<YOUR_FOUNDRY_ACCOUNT>" `
  --query id --output tsv

az role assignment create `
  --assignee-object-id "<OPERATOR_OBJECT_ID>" --assignee-principal-type User `
  --role "Cognitive Services OpenAI User" --scope $AccountId
```

Use the correct principal type for a non-user identity. Allow time for RBAC
propagation. Management-plane access does not imply inference/data-plane access.

## Install and configure

### 1. Clone and install

```powershell
git clone https://github.com/pedro-pauletti/azure-model-latency-benchmark.git
Set-Location azure-model-latency-benchmark
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

Copy-Item .\benchmark.example.json .\benchmark.json
Copy-Item .\tool-benchmark.example.json .\tool-benchmark.json
```

If your execution policy prevents activation, use
`.\.venv\Scripts\python.exe` instead of `python` in subsequent commands; do not
weaken an organizational execution policy.

**Copy both configuration files**, even for a generation-only dashboard. The
dashboard loads the tool catalog at startup, but Decision does not need to be
deployed for **local generation**.

### 2. Configure `benchmark.json`

Replace every placeholder and trim `models` to the versions you actually have.
A smaller starting configuration is:

```json
{
  "subscription_id": "00000000-0000-0000-0000-000000000000",
  "resource_group": "YOUR_RESOURCE_GROUP",
  "account": "YOUR_FOUNDRY_ACCOUNT",
  "project": "YOUR_FOUNDRY_PROJECT",
  "location": "brazilsouth",
  "endpoint": "https://YOUR_FOUNDRY_ACCOUNT.openai.azure.com/openai/v1/",
  "capacity": 100,
  "models": [
    {"name": "gpt-4.1", "version": "2025-04-14", "reasoning_effort": null},
    {"name": "gpt-5.4-mini", "version": "2026-03-17", "reasoning_effort": "none", "verbosity": "low"}
  ]
}
```

| Field | How to set it |
| --- | --- |
| `subscription_id` | Your actual subscription GUID; all zeros above is deliberately invalid configuration. |
| `resource_group`, `account`, `project` | Existing resource names in that subscription. |
| `location` | The Foundry account region, such as `brazilsouth`; the preflight checks it. |
| `endpoint` | The **account OpenAI v1 endpoint**, not a project URL or a URL already ending in `chat/completions`. |
| `capacity` | Requested deployment capacity in the model SKU's units; it is **not** a universal TPM value or benchmark concurrency setting. |
| `models[].name` | Both the model catalog name and the deployment name used by this implementation. Arbitrary OpenAI deployment aliases are not supported by this provisioner. |
| `models[].version` | Exact version to verify/provision. Runtime requests address the deployment; its version must match the configuration. |
| `reasoning_effort`, `verbosity` | Included only when non-null. Use only values accepted by that model/API. |

The full example includes these **configured candidates**, not a promise that
all are deployable in your subscription:

| Model | Pinned example version |
| --- | --- |
| `gpt-6-sol` | `2026-09-22` |
| `gpt-5.6-terra` | `2026-07-09` |
| `gpt-5.6-sol` | `2026-07-09` |
| `gpt-5.5` | `2026-04-24` |
| `gpt-5.4-mini` | `2026-03-17` |
| `gpt-5.4` | `2026-03-05` |
| `gpt-5.2` | `2025-12-11` |
| `gpt-5.1` | `2025-11-13` |
| `gpt-4.1` | `2025-04-14` |

The GPT-5/6 example presets request `reasoning_effort=none` and `verbosity=low`;
GPT-4.1 omits those options. No unsupported temperature or sampling override is
silently added. A rejected parameter is an error to investigate, not an
instruction to silently change the benchmark.

### 3. Configure `tool-benchmark.json`

Replace `YOUR_FOUNDRY_ACCOUNT` in the Decision provider endpoint. Keep
`deployment=decision-1` for the supplied provisioner. This endpoint is distinct
from the OpenAI Chat Completions endpoint.

**Implementation constraint:** `provision_decision.py` currently pins model
`Microsoft-Decision-1`, version `1`, alias `decision-1`, and capacity `100` in its
constants. Editing the JSON alone does not change those provisioning constants.
If the catalog requires a different version or capacity, review/update the
provisioner and runtime JSON together before using it. Newer Decision service
versions may require API changes; they are not automatically compatible.

## Verify and provision model deployments

Run the read-only preflight **before** any billable benchmark:

```powershell
python .\provision.py
```

This makes authenticated ARM queries for the account/project, exact model
versions, Priority-capable SKU, quota, existing deployments, and regional
capacity. It does not send inference. An unavailable candidate stops the
preflight: remove it from your local configuration or obtain approved capacity;
there is no automatic region/version substitution.

To create missing OpenAI deployments:

```powershell
python .\provision.py --apply
```

The provisioner requests `GlobalStandard`, enables deployment-level Priority,
and disables automatic version upgrades. It refuses to overwrite an existing
deployment with a different version, SKU, capacity, tier setting, or upgrade
policy. It writes a local inventory under `results`.

You still need valid model parameters and inference permissions. ARM preflight
does not prove that a later data-plane request will succeed.

## Use the live dashboard

```powershell
python .\server.py
```

Open <http://127.0.0.1:8765/>. To use another local port:

```powershell
python .\server.py --port 8766
```

Choose the generation or tool-selection tab, then:

1. Select the execution target: local, or Container Apps after provisioning.
2. Select models/tiers and adjust tokens or routing cases, repetitions, warmups,
   interval, and timeout. Model request presets are shown with the configuration.
3. Review the request volume and explicitly confirm billing before starting.
4. Observe progress. Once results exist, filter/sort them without rerunning
   inference; Standard/Priority remain adjacent for each model.
5. Use the capture/export controls for PNGs or CSVs. The views include execution
   context and token usage; detailed information can be collapsed.

There is one shared active-job lock across both tabs. Moving sliders, opening
history, sorting, and exporting do not submit new inference requests.

Cancellation is cooperative: it stops subsequent calls after the in-flight call
finishes; that call may still be billed. Closing a browser tab does not cancel a
run. Keep the backend running for progress, cancellation, and Azure artifact
collection. A remote job can continue after your local backend stops.

The server binds to loopback. **Do not expose it through port forwarding, a
public ingress, or a reverse proxy.** Host/Origin checks and POST control tokens
are local safeguards, not a multi-user authentication system.

## Run generation benchmarks from the CLI

These commands run on the **current host**, not automatically in Container Apps.
Use the dashboard's Azure execution target for the supported remote workflow.

### Start with a non-billable plan

```powershell
python .\benchmark.py --models gpt-4.1 `
  --input-tokens 512 --output-tokens 128 --repetitions 1 --warmups 1
```

Without `--execute`, this CLI prints a request/token plan and exits without
acquiring an inference token or sending inference.

### Send a small smoke run

The following command **is billable**:

```powershell
python .\benchmark.py --models gpt-4.1 `
  --input-tokens 512 --output-tokens 128 `
  --repetitions 2 --warmups 1 --interval 3 --execute
```

Check tier confirmation, actual token counts, exclusions, and errors before
spending on a larger run.

### Reproduce a 7,500-input / 300-output-target scenario

First inspect the plan; append `--execute` only after review:

```powershell
python .\benchmark.py --models gpt-4.1 gpt-5.4-mini `
  --input-tokens 7500 --output-tokens 300 `
  --repetitions 30 --warmups 3 --interval 3 --timeout 180
```

This two-model plan has **120 measured requests**, **12 warmups**, and up to
**8 calibration requests**. If all nine configured candidates are selected,
the same settings yield 540 measured requests and 54 warmups, plus calibration.

These are synthetic prompts, not a replay of a customer's conversations.
`max_completion_tokens=300` is an **upper bound**, not a guarantee of 300 output
tokens. Early normal completion remains visible and is excluded from an
exact-length comparison; the harness does not selectively rerun short answers.

### Sweep sizes or select one tier

```powershell
python .\benchmark.py --models gpt-4.1 --tiers priority `
  --input-tokens 512 4096 --output-tokens 128 512 `
  --repetitions 10 --warmups 1 --interval 3
```

This is also a plan until `--execute` is added. **Even a Priority-only generation
run performs Standard calibration requests** to align actual input usage.

| Option | Default / behavior |
| --- | --- |
| `--models` | All configured models if omitted. |
| `--tiers` | `default priority`; explicit request processing tiers. |
| `--input-tokens` | `512 4096 16384`; one or more server input-token targets. |
| `--output-tokens` | `128 512`; one or more output caps/strict comparison targets. |
| `--repetitions` | `20` measured calls per model/input/output/tier cell. |
| `--warmups` | `1` per cell, recorded separately from measurement. |
| `--interval` | `3` seconds after each request. |
| `--timeout` | `180` seconds: per-I/O timeout plus a deadline checked between SSE lines, not a hard process kill timer. |
| `--seed` | `42`, controlling schedule randomization. |
| `--input-tolerance`, `--output-tolerance` | `0`; absolute token-count tolerances, not percentages. |
| `--encoding` | `o200k_base`; actual server usage, not local encoding alone, determines comparability. |
| `--config` | `benchmark.json` next to the script. |
| `--result-dir` | Optional **new** output directory; an existing directory is rejected. |
| `--cancel-file` | Stop between requests when this local control file exists. |

Measured requests are `models x input sizes x output sizes x tiers x repetitions`.
Warmups replace repetitions with the warmup count. Calibration adds up to four
Standard requests per model/input pair, each with a 16-token output cap.

Execution is sequential: one inference at a time, HTTP/1.1 connection reuse,
120-second keepalive expiry, and no automatic inference retries. Cells and
tiers are shuffled using the seed; prompts receive fresh nonces. Authentication
and serialization are outside HTTP timings; connection/network time is inside.

The strict generation cohort requires success, matching requested/returned
tier, matching actual input/visible-output lengths, and known zero cache and
reasoning usage. Missing counters do not become zero. Raw summaries retain
excluded cohorts: use `comparable=true` for a controlled ranking.

## Run the tool-selection benchmark

### Provision Decision and its inference permission

```powershell
# Authenticated, read-only preflight.
python .\provision_decision.py

# Creates the missing deployment and the scoped custom inference role/assignment.
python .\provision_decision.py --apply --grant-inference
```

The data action is
`Microsoft.CognitiveServices/accounts/AIServices/providers/action`; it is not
included in **Cognitive Services OpenAI User**. The helper creates a custom
role assignable within the configured resource group and assigns it to the
signed-in caller at the Foundry account. This requires RBAC administration
permissions and may take time to propagate.

### Use an explicit settings file

The no-argument plan uses Decision, `gpt-4.1`, and `gpt-5.4-mini`. If you trimmed
the catalog, supply only model names present in your configuration.

Save this as **`tool-settings.local.json`** in the repository root:

```json
{
  "models": ["Microsoft-Decision-1", "gpt-4.1"],
  "tiers": ["default", "priority"],
  "cases": 12,
  "repetitions": 3,
  "warmups": 3,
  "padding_tokens": 0,
  "interval": 3,
  "timeout": 180,
  "confirm_billing": true
}
```

```powershell
# Plan only. The settings confirmation does not replace --execute.
python .\tool_benchmark.py --settings .\tool-settings.local.json

# Billable execution of that exact plan.
python .\tool_benchmark.py --settings .\tool-settings.local.json --execute
```

This example has three profiles: Decision once, plus GPT-4.1 Standard/Priority.
With 12 cases and three repetitions, it makes **108 measurements and 9 warmups**.
Warmups are per profile, **not** per case. Valid case counts are 3, 6, 9, or 12,
balanced across the three routing classes; the full 12-case set is also balanced
between English and Portuguese.

The tool CLI also accepts `--config`, `--result-dir`, `--run-id`, and
`--cancel-file`. The Decision catalog still comes from root
`tool-benchmark.json`; `--config` does not relocate that file.

### Understand the agent

The same three deterministic, no-I/O functions are available to each selector:
`route_billing`, `route_technical`, and `route_account`. Each takes `{}` and
returns a simulated routing result. Names, arguments, and response structure
are validated before dispatch.

LLMs use strict native function schemas, `tool_choice=required`,
`parallel_tool_calls=false`, `stream=false`, and an output cap of 128.
Decision uses a typed `choice` request; an adapter translates its answer into
the same validated local function call. It does **not** pretend to implement
native Chat Completions tool calling or Priority.

Accuracy includes all measured attempts, including errors. Tool latency
aggregates include only correct, comparable selections. Known positive
cache/reasoning counters exclude a sample; absent counters, including Decision
counters the service does not provide, remain explicitly unknown.

Padding uses a local tokenizer and does not guarantee identical billed tokens
across different protocols. Use actual service usage when available.

This is **not** the generation 7,500/300 workload, a Foundry Agent Service
benchmark, real external tool execution, multi-step planning, or a final
LLM-written response. Simple-case accuracy is not a general quality or safety
evaluation.

## Run inference in Azure Container Apps

The local dashboard remains the control plane; only inference moves into the
Azure Job:

```text
Browser -> loopback Python server -> ARM: start manual Container Apps Job
                                -> authenticated Blob: settings / snapshots
Managed-identity worker         -> Foundry inference endpoints
                                -> authenticated Blob: progress / results
```

### 1. Complete prerequisites

Verify OpenAI deployments and complete
`provision_decision.py --apply --grant-inference` first.
**The current Container Apps helper assigns the Decision custom role to its
worker unconditionally**, even for a generation-only run. That role definition
must already exist. If Decision is unavailable, local generation still works;
the standard cloud setup needs an administrator-reviewed alternative for this
role dependency.

The operator needs resource deployment, ACR build, role-assignment, Job
read/start/execution-read, and Blob data permissions as applicable. The helper
assigns Blob access but does not grant every management-plane permission to
the operator.

These resource providers must already be registered; the script checks but does
not register them:
`Microsoft.App`, `Microsoft.ContainerRegistry`, `Microsoft.Storage`,
`Microsoft.ManagedIdentity`, and `Microsoft.Network`.

### 2. Validate the infrastructure

Use the **single public egress IP that actually reaches Azure Storage**:

```powershell
python .\deploy_containerapps.py --operator-ip "<YOUR_PUBLIC_EGRESS_IP>"
```

This writes `infra\containerapps.json` and performs authenticated ARM validation;
it does not provision infrastructure or submit inference. It is not an
offline-only command.

### 3. Provision and build

The following step creates resources and runs a potentially billable ACR build:

```powershell
python .\deploy_containerapps.py `
  --operator-ip "<YOUR_PUBLIC_EGRESS_IP>" --apply --build
```

No local Docker daemon is required. The helper creates:

| Resource | Purpose |
| --- | --- |
| ACR Basic registry | Authenticated runner image storage and remote build. |
| Storage account + `benchmark-runs` container | Entra-authenticated settings, snapshots, and artifacts. |
| Network Security Perimeter | Enforced Storage access policy with a narrow operator IP rule. |
| User-assigned managed identity | Worker inference, image pull, and scoped Blob access. |
| Container Apps Consumption environment | Regional execution host. |
| Manual Container Apps Job | One replica, 1 vCPU / 2 GiB, parallelism 1, replica retry limit 0. |

The image is pinned by digest, not a mutable tag. Startup/image pull are outside
per-request HTTP timings. Runner region, image, resources, and authentication
provenance are recorded with the run.

**Networking scope:** the Job has no public application ingress. Storage uses
`SecuredByPerimeter` with an **Enforced** NSP association, no anonymous Blob
access, and no shared-key authentication. The subscription identity network
rule does not itself grant data access; Blob RBAC is container-scoped.
This helper does **not** build end-to-end VNet/private-endpoint isolation or
configure the Foundry account's network rules. ACR remains an authenticated
registry endpoint. "No ingress" is not a claim that every service uses a private
IP address.

The Blob transport is tested before a ready runner is recorded. Local
`containerapps.json` contains runner resource identifiers; keep it private.

### 4. Start the dashboard and select Azure explicitly

```powershell
python .\server.py
```

Restart an existing server after provisioning so it reloads runner configuration.
In either benchmark tab, select the Azure Container Apps execution target,
choose settings, confirm billing, and start. Verify the resulting run's recorded
executor and region rather than relying only on a UI label.

There is no silent fallback to local inference if Azure fails. A lease prevents
simultaneous workers and a run-ID claim prevents replaying the same cloud run.
This does not make a manually submitted **new** run ID free or deduplicated.

### 5. Update or retire the runner

Runtime source and both local benchmark configurations are included in the
private ACR image. Re-run `--apply --build` after changing those files, then
restart the dashboard. Editing local presets without rebuilding does not update
the remote image.

For an approved egress IP change, `--apply` without `--build` can update
infrastructure/transport; it does not create an initial runnable Job image.

Resources persist between runs. There is no automatic teardown. Remove only
the dedicated benchmark resources through your approved governance process;
do not delete a shared Foundry account or resource group just to clean up
benchmark runs.

## Metrics and interpretation

All client latency values are milliseconds unless a field explicitly says
tokens/second. Timings use a monotonic performance clock.

| Generation metric | Meaning |
| --- | --- |
| `headers_ms` | Request start to response headers. |
| `first_sse_ms` | First SSE data event, which may contain only metadata. |
| `ttft_ms` | First **non-empty visible text chunk**, not a role/filter event. |
| `last_token_ms` | Arrival of the last visible text chunk, relative to request start. |
| `generation_ms` | Last visible text chunk minus first visible text chunk. |
| `total_ms` | Complete measured HTTP streaming operation. |
| `stream_tail_ms` | Measured total minus last visible text arrival. |
| `tpot_ms` | Generation window divided by `visible_tokens - 1`, when meaningful. |
| `generation_tokens_per_s` | `(visible_tokens - 1) / generation window`, in tokens/second. |
| `end_to_end_tokens_per_s` | Visible output tokens divided by total HTTP duration. |
| `chunk_gap_*` | Gaps between visible-content chunks; chunks are not individual tokens. |
| `server_*` | Service latency checkpoints **only when supplied**; not estimated from client timings. |

| Tool-selection metric | Meaning |
| --- | --- |
| `headers_ms` | HTTP start to response headers. |
| `http_total_ms` | HTTP request plus complete non-streamed response body. |
| `selection_ms` | HTTP start through parsing and validated complete selection. |
| `tool_ms` | Execution of the local simulated function after validation. |
| `agent_total_ms` | HTTP start through tool return; excludes a final LLM answer. |

Aggregates include count, mean, p50/p90/p95/p99, min/max, sample standard
deviation, and coefficient of variation where defined. Percentiles use linear
interpolation between ordered observations. Missing metrics stay missing;
single-chunk outputs do not acquire an invented decode rate.

**Priority gain** is `100 x (Standard p50 - Priority p50) / Standard p50`.
Positive means Priority was faster in that cohort; negative means it was slower.
Interleaving reduces temporal bias but does not establish a causal or
statistically significant gain. Routing, service load, network behavior, small
samples, and different accepted sample sets can change results.

Always inspect sample count, exclusion/error rate, actual tokens, actual tier,
and dispersion alongside p50. A 30-sample p99 is exploratory. Do not sum
component medians to obtain the median total, pool different token dimensions,
or compare streaming TTFT directly with non-streaming tool-selection duration.

## Results, exports, and reproducibility

Generation writes `results\<run-id>`; tools write `tool-results\<run-id>`.

| File | Contents |
| --- | --- |
| `run.json` | Manifest, settings, provenance, completion state, and workload metadata. |
| `requests.jsonl` | Per-attempt evidence, including warmup/calibration phases where applicable, usage, failures, exclusions, and timing data. |
| `requests.csv` | Tabular per-attempt export. |
| `summary.json`, `summary.csv` | Aggregates; generation retains separate comparable/excluded cohorts. |
| `report.md` | Generation-only text report and Standard/Priority comparison. |

Generation's raw manifest contains the configured Azure environment and local
runtime details. It is **not anonymized**. A completed run can still contain
errors/exclusions: completion means the schedule was attempted, not that every
measurement qualified.

Benchmark exit codes: `0` means all measured samples qualified; `2` means at
least one was excluded/failed; `3` means cooperative cancellation. Configuration,
authentication, calibration, or other fatal failures can instead terminate with
an exception/nonzero exit. Preserve partial artifacts before investigating.

### Create an offline HTML snapshot

After at least one generation run:

```powershell
python .\dashboard.py
Start-Process .\results\dashboard.html
```

This embeds saved generation and available tool histories; it sends no inference.
The current CLI needs a generation run directory, so use the live dashboard for
tool-only history if no generation run exists yet. An offline snapshot cannot
start new jobs.

Snapshots are self-contained with respect to the embedded dashboard data, but
they are **not a privacy scrubber**. PNG/CSV exports can also contain contextual
metadata. Review every deliverable before external sharing.

### Reproduce the methodology, not an identical latency number

Record the Git commit, resolved package versions, model/deployment versions,
request presets, token targets, seed, repetitions, warmups, interval, timeout,
execution region/image, test time window, usage coverage, and exclusion counts.
Keep local and Azure cohorts separate and repeat the run across representative
time windows before drawing production conclusions.

To capture local software provenance without adding it to Git:

```powershell
New-Item -ItemType Directory -Force .\exports | Out-Null
git rev-parse HEAD | Set-Content .\exports\source-commit.txt
python -m pip freeze | Set-Content .\exports\requirements.resolved.txt
```

Dependency ranges are not a lockfile; preserve resolved versions for your run.
Service-side routing and deployment state can change even with identical code.
Fresh nonces intentionally prevent byte-identical prompts. For production
validation, separately test realistic prompts, cache hit rates, concurrency,
rate limits, reasoning, RAG, external tools, and business task quality.

## Troubleshooting

| Symptom | Check / action |
| --- | --- |
| Model/version/SKU/capacity preflight fails | Revalidate your subscription catalog and quota; trim unavailable candidates. Do not silently substitute a region or version. |
| Existing deployment differs | Review version, SKU, capacity, Priority setting, upgrade policy, and state. The helper deliberately will not overwrite it. |
| HTTP 401/403 on inference | Verify tenant/subscription, Entra sign-in, data-plane role, propagation, endpoint, and network access. Decision needs its own provider data action. |
| Decision role assignment fails during cloud setup | Create the expected custom role through the Decision setup first; cloud provisioning currently depends on it. Check role-definition and assignment permissions. |
| Provider must be registered | Have an authorized administrator register the named Azure resource provider, then validate again. |
| HTTP 429 | Review captured rate-limit headers, model quota, and request spacing. Increase the interval or obtain approved capacity; failed calls are not automatically retried. |
| Normal HTTP 200 but output is short | Output cap is not a minimum. Inspect `finish_reason`, actual output, and exclusions. Compare another clearly labeled preset or exclude the model from an exact-output cohort; do not relabel short outputs as 300 tokens. |
| Requested Priority differs from actual tier | Inspect service support/response and exclusions. Do not count a downgraded or unconfirmed call as a Priority result. |
| Unknown cache/reasoning usage | Keep it unknown. Generation's strict cohort rejects it; Decision's missing counters are explicitly reported rather than asserted to be zero. |
| No useful TPOT/decode rate | A single visible chunk or insufficient timing/token evidence cannot support that metric. |
| Azure target unavailable / Blob 403 | Verify NSP association, operator's Storage egress IP, Blob RBAC, and propagation. Re-run approved transport setup; do not fall back silently. |
| VPN/proxy IP check disagrees with Storage behavior | Egress may be destination-specific. Use an approved stable path that actually reaches Storage, not a broad proxy IP allowlist or a weakened perimeter. |
| Tool calls fail only behind an environment proxy | The tool HTTP client deliberately uses `trust_env=False`; `HTTPS_PROXY` is not used for its inference path. Arrange approved direct/private connectivity or review the implementation; do not assume all clients share proxy behavior. |
| Browser/backend disconnects during Azure execution | Inspect the existing Azure execution and saved artifacts before starting a new billed run; the remote worker may still be running. |
| Changed presets do not affect Azure | Rebuild/redeploy the runner image and restart the local dashboard. |
| Offline report has no runs | Complete or retain a generation run with its manifest; tool-only history is supported by the live dashboard. |

## Privacy and safe sharing

The public source contains sanitized examples, synthetic routing cases, and
test fixtures. Example account names, the all-zero subscription, reserved test
domains, test IPs, and built-in Azure role GUIDs are not live credentials.

Do **not** commit real configuration, raw runs, customer prompts/results,
inventories, exports, identity metadata, access tokens, API keys, certificates,
or generated infrastructure. These may expose subscription/resource IDs,
endpoints, request IDs, operator IPs, local paths, or infrastructure topology
even without containing a password.

The repository ignores root `benchmark.json`, `tool-benchmark.json`,
`containerapps.json`, `tool-settings*.json`, `infra`, `results`, `tool-results`,
`exports`, environment files, Azure CLI state, and common private-key containers.
`.gitignore` is only a guardrail: renamed files, screenshots, Git history, and
`git add --force` can bypass it. Review staged content and all exported reports.

Azure authorization stays in the backend process, not in generated HTML.
However, saved run context and exported reports can still identify your
environment. Private ACR images contain runtime configuration and should not be
redistributed as sanitized source.

Before publishing a fork or a release:

```powershell
git status --short
git diff --cached --check
git diff --cached

# Optional: with an independently installed local Gitleaks scanner.
gitleaks git . --log-opts="--all --full-history" --redact
```

The exact historical false positive in `.gitleaksignore` is the
`chunk_gap_p50_ms` metric string, not an API key. It suppresses one original
commit/file/line fingerprint, not a file or a credential rule. Scanning and
review reduce disclosure risk; they are not an unconditional guarantee about
future commits, forks, or exports. Rotate/revoke any real credential immediately
if it is ever exposed; deleting its latest file is not enough.

## Tests and repository map

Copy both sanitized example configurations before running tests:

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest test_benchmark test_cloud test_dashboard test_server test_tools -v

# Full suite, including browser interactions and exports; Microsoft Edge required.
python -m unittest discover -p "test_*.py" -v
```

Tests use fixtures/mocks and should not provision resources or send billable
inference. The default CLI plan paths are also safe to inspect without Azure
credentials. Do not substitute `--execute` while performing an offline check.

| File | Responsibility |
| --- | --- |
| `benchmark.py` | Streaming generation runner, calibration, strict cohorts, and reports. |
| `tool_agent.py` | Routing cases, selector protocols, validation, and simulated tools. |
| `tool_benchmark.py` | Tool schedules, settings validation, accuracy, and latency aggregates. |
| `server.py` | Loopback control plane and live subprocess/cloud coordination. |
| `dashboard.py`, `dashboard.template.html` | Saved-run loading, embedded HTML, live controls, and exports. |
| `provision.py`, `provision_decision.py` | Explicit model preflights and opt-in deployment. |
| `deploy_containerapps.py` | ARM validation, scoped access, ACR build, and manual Job setup. |
| `cloud_runner.py`, `cloud_worker.py`, `azure_io.py` | Authenticated control/results transport and Azure execution. |
| `runtime_context.py` | Authentication selection and recorded runner provenance. |
| `Dockerfile`, `.dockerignore` | Non-root worker image and allowlisted build context. |
| `test_*.py` | Measurement, controls, transport, UI, and export regression tests. |

Further reading:
[Azure CLI installation](https://learn.microsoft.com/cli/azure/install-azure-cli),
[Microsoft Decision](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision),
and [Gitleaks](https://github.com/gitleaks/gitleaks).
