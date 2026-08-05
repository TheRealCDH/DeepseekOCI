# DeepseekOCI

Flux-managed deployment of **DeepSeek-V4-Flash** served by
[llama.cpp](https://github.com/ggml-org/llama.cpp) on a single-node k3s cluster, CPU-only.

## What this deploys

| | |
|---|---|
| Model | DeepSeek-V4-Flash, `UD-IQ2_M` GGUF (~85 GiB, 284B MoE / 13B active) |
| Runtime | `ghcr.io/ggml-org/llama.cpp:server-b10257` |
| Interface | llama.cpp built-in WebUI + OpenAI-compatible API on `:8080` |
| Exposure | tailnet only, via the Tailscale Kubernetes operator |

## Notes

**The model weights are not in this repo.** An 85 GiB GGUF cannot live in git. The
manifests mount it from the node via `hostPath` at `/models`; download it separately
with a parallel-capable client (HuggingFace rate-limits per connection, so a single
stream throttles badly — `aria2c -x16` is roughly twenty times faster than `curl`).

**The image tag is pinned deliberately.** `deepseek4` architecture support landed in
llama.cpp [PR #24162](https://github.com/ggml-org/llama.cpp/pull/24162), merged
2026-06-29. Older builds fail with `unknown model architecture: 'deepseek4'`. Do not
float this to `:server`.

**CPU-only by design.** At ~85 GiB, a consumer GPU with 8–16 GB holds under 20% of the
weights, so offload yields little on token generation while competing with other
workloads for the card. Throughput here is bounded by memory bandwidth, and MoE
architectures win because only the active parameters are read per token.

## Layout

```
kustomization.yaml          namespace + resources
namespace.yaml
deepseek/
  deployment.yaml           llama-server, hostPath model mount
  service.yaml              ClusterIP :8080
  tailscale-service.yaml    tailnet exposure
```

## Usage

Once the Tailscale operator has published the proxy, the WebUI and API are reachable
from any device on the tailnet at `http://deepseek:8080` (use the fully-qualified
MagicDNS name if your client has no tailnet search domain).

```bash
curl http://deepseek:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"..."}],"max_tokens":700}'
```

**Give it a generous `max_tokens`.** V4-Flash is a *reasoning* model: with `--jinja`
applying the chat template, llama.cpp splits the reply and returns the thinking trace in
`message.reasoning_content`, leaving `message.content` for the final answer. A budget
that runs out mid-thought comes back as HTTP 200 with a perfectly **empty `content`**,
`finish_reason: "length"`, and all the tokens in `reasoning_content` — which looks like
a broken deployment but is not. Short factual answers routinely spend 400+ tokens
thinking first, so budget accordingly.

Use `/v1/chat/completions`, not the raw `/completion` endpoint — the latter bypasses the
chat template and returns incoherent continuation text.

## Measured on the reference host

Dual-socket-class desktop, 8 memory channels at DDR4-2933, no GPU:

| | |
|---|---|
| Decode | ~4.9 tok/s |
| Model load | ~30 s from NVMe |
| Context | 131072, `kv_unified`, 4 slots |

Answer quality at `IQ2` holds up — reasoning traces are coherent and on-topic. Tool and
structured-output reliability is the capability most likely to degrade at this
quantisation and is worth testing against your own workload before depending on it.
