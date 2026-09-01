<!-- replaces vllm-explore/README.md -->

# vllm-explore

**Question:** how do LLM inference engines actually behave under load — and what
does an RTX 5090 (WSL2) give you?

A reproducible benchmark project on the [Sconix research
engine](https://github.com/Sconix-AI/sconix-research): every run records its
resolved config, git SHA + diff, `pip freeze`, GPU info, and per-step metrics
into `results/<timestamp>__<name>__<sha>/`.

## Findings so far

**Qwen2.5-7B-Instruct, vLLM, RTX 5090:**

| metric | value |
|---|---|
| batched decode @ batch 64 | **≈ 5,560 output tok/s** (≈ 43 req/s), reproducible |
| cold load | 111 s (torch.compile + CUDA graphs + FlashInfer autotune) |
| warm load (caches primed) | 21 s |
| VRAM working set | ≈ 17 GB |

Getting vLLM running at all on the 5090 under WSL2 took fixing three environment
quirks (a stray system `nvcc` on PATH, a CUDA-13 subpackage skew, a pinned-memory
setting) — each written up in `log.md`.

## Run

```bash
task setup
task exp -- my-idea           # scaffold experiments/expNNN_my-idea/
task run -- expNNN_my-idea
task compare                  # runs side by side
task report                  # render report.html
```

## Layout

| path | what |
|---|---|
| `questions.md` | open questions |
| `log.md` | dated log, newest on top |
| `experiments/` | one folder per experiment (`_template/` is the seed) |
| `results/` | one folder per run (git-ignored) |
| `report.qmd` | Quarto report over `results/` |
