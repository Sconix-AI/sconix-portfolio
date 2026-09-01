# Sconix Portfolio

Flagship projects built as a stress test of the [Sconix
platform](https://github.com/Sconix-AI/sconix-systems). Each is real, deployed,
and measured; the platform only grows when a project makes a gap hurt.

**Positioning:** Agentic AI Engineer with an infrastructure edge.

| # | project | what it proves | status |
|---|---------|----------------|--------|
| **F1** | **[pilot](https://github.com/Sconix-AI/pilot)** | a production agent with real side effects — typed actions, a policy gate, a human-approval boundary, incident memory, verified recovery | **done** — 45 tests, [runbook](https://github.com/Sconix-AI/pilot/blob/main/RUNBOOK.md) |
| F2 | `posttrain` (tbd) | owns a model-quality metric — post-train a small model on an RTX 5090, publish base vs. tuned on a held-out eval + an HF model card | not started |
| F3 | `inference` (tbd) | quantified inference cost / latency reduction (quantization, batching, spec-decode) | not started |

F2 and F3 share a model and a serving stack, built on the [research
engine](https://github.com/Sconix-AI/sconix-research) and
[vllm-explore](https://github.com/Sconix-AI/vllm-explore).

Also shipped on the platform: three SaaS apps (auth, Stripe billing, an
in-product agent), each deployed with TLS + Postgres + Redis + migrations on one
small box behind a shared edge.
