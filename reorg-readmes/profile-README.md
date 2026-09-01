<!-- paste into YusufRM/YusufRM/README.md -->

# Yusuf — Agentic AI Engineer

I build production AI agents that take real actions — with permissions,
guardrails, human-approval boundaries, and verified recovery — and I keep the
GPUs under them fast and cheap.

### Sconix — an agent-native platform, and the flagships that prove it

| | |
|---|---|
| **[pilot](https://github.com/Sconix-AI/pilot)** | an autonomous ops agent that watches a live fleet, diagnoses failures, restarts under policy, proposes a rollback when a restart can't fix it, and executes it only after a human approves — nothing mutating happens off the leash. 45 tests, an operator runbook, full-chain integration tests. |
| **[sconix-systems](https://github.com/Sconix-AI/sconix-systems)** | the platform: a versioned project manifest, typed actions with risk/approval/verification, immutable releases, plan→approve→deploy→canary→promote→rollback→teardown, many apps on one €6.50/mo box behind a shared TLS edge. |
| **[sconix-research](https://github.com/Sconix-AI/sconix-research)** · **[vllm-explore](https://github.com/Sconix-AI/vllm-explore)** | the research engine + RTX 5090 inference benchmarks — batched-decode throughput, batch-size knees, cold vs. warm start, working-set memory. |

Three real SaaS apps shipped on it (auth, Stripe billing, an in-product agent),
each deployed with TLS, Postgres, Redis, and migrations.

### Also

Python · async · FastAPI · PyTorch / vLLM · the Anthropic API · Docker · Next.js ·
Hetzner. B.S. Computer Science.

📫 &lt;your public email&gt; &nbsp;·&nbsp; &lt;LinkedIn, if you want it here&gt;
