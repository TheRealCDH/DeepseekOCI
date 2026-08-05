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
kustomization.yaml          the three stacks below
deepseek/                   the LLM — CPU only
  namespace.yaml
  kustomization.yaml        pins namespace: deepseek
  deployment.yaml           llama-server, hostPath model mount
  service.yaml              ClusterIP :8080
  tailscale-service.yaml    tailnet exposure
speech/                     STT + TTS — two engines, two GPUs
  namespace.yaml
  kustomization.yaml        pins namespace: speech
  deployment.yaml           speaches: faster-whisper + Kokoro (NVIDIA)
  service.yaml              ClusterIP :8000
  tailscale-service.yaml    tailnet exposure
  qwentts-deployment.yaml   Qwen3-TTS, voice cloning (AMD, Vulkan)
  qwentts-service.yaml      ClusterIP :8080
voice/                      browser UI, HTTPS
  namespace.yaml
  kustomization.yaml        pins namespace: voice
  index.html                the app (vanilla JS, no build step)
  nginx.conf                serves the page, proxies /v1/* same-origin
  deployment.yaml           nginx
  service.yaml              NodePort 30080 (TLS terminated on the host)
```

Each stack pins its own namespace in its own `kustomization.yaml`. The root kustomization
deliberately sets no `namespace:` — it used to, which would have dragged every later stack
into the `deepseek` namespace.

## Speech stack

OpenAI-compatible `/v1/audio/transcriptions` and `/v1/audio/speech`, so it composes with
the llama.cpp endpoint above rather than introducing a second API style. It also exposes
`/v1/realtime`, pointed at the local LLM via `CHAT_COMPLETION_BASE_URL`.

**The two halves want opposite numeric formats, which is not obvious.** On Pascal:

| | format | why |
|---|---|---|
| Whisper (CTranslate2) | **int8** | uses the DP4A path; FP16 runs at 1/64 rate on sm_61 |
| Kokoro (ONNX Runtime) | **fp32** | ONNX Runtime's CUDA provider has thin int8 kernel coverage |

Choosing the int8 Kokoro build looks like the consistent decision and is a trap: ORT
silently falls back to **CPU**, and measured throughput drops from 7.7x realtime to 0.5x —
slower than speech itself, and competing with the LLM for the CPU it is trying to avoid.

Measured on a GTX 1070 Ti (8 GB, sm_61), 10s of audio:

| | speed |
|---|---|
| Kokoro fp32 TTS | 0.46s per 3.6s of audio — **7.7x realtime** |
| faster-whisper large-v3-turbo | 4.7s — 2.1x realtime |
| faster-whisper large-v3 | 8.7s — 1.16x realtime, not worth it here |

Models download from HuggingFace on first use into the `HF_HOME` cache on the mounted
hostPath, so they survive pod restarts.

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

## Voice UI

A push-to-talk page: tap the mic, speak, and the transcript goes to the LLM whose reply is
streamed back and spoken sentence-by-sentence as it generates. Vanilla JS, no build step.

**HTTPS is mandatory, not cosmetic.** `getUserMedia()` is refused outside a secure context
by every modern browser, so a plain-HTTP page cannot access the microphone at all. Chromium
normally offers `chrome://flags/#unsafely-treat-insecure-origin-as-secure` as an escape
hatch, but **GrapheneOS's Vanadium removes the `chrome://flags` UI entirely**, so there is
no override available there.

TLS is terminated by the **host's** tailscaled via `tailscale serve`, not by the Tailscale
Kubernetes operator's Ingress. That is a throughput decision, measured on an identical
252,972-byte audio response:

| | total | speed |
|---|---|---|
| operator Ingress (`TS_USERSPACE`, userspace TCP + TLS) | 13.87s | 18 KB/s |
| `tailscale serve` on the host (kernel path) | 2.14s | **118 KB/s** |

6.5x, and not CPU starvation — the Ingress proxy sat at 2m CPU with no limit. Audio is the
only thing this app moves in bulk, so that gap decides whether speech streams or stutters.
Both paths get a real Let's Encrypt cert, so no private CA has to be trusted on the phone.
The serve mapping is declared in `/etc/nixos/voice-serve.nix`.

Everything is served from one origin. The browser never talks to speaches or llama.cpp
directly; nginx proxies `/v1/audio/*` and `/v1/chat/*`. Neither backend sends CORS headers
(speaches ships `allow_origins=None`), so cross-origin calls would simply fail.

Two details that are easy to get wrong:

- **`proxy_buffering off`** on the LLM location. With buffering on, nginx accumulates the
  SSE stream and releases it in one lump at the end, which silently defeats sentence-chunked
  streaming — the reply would arrive complete, then be spoken all at once.
- **Playback goes through WebAudio, not `<audio>.play()`.** Android blocks programmatic
  playback outside a user gesture; the `AudioContext` is created and resumed inside the mic
  tap handler and stays usable afterwards.

The thinking toggle maps to `chat_template_kwargs.thinking` per request, so voice and text
sessions can differ without redeploying anything.
