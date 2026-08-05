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
