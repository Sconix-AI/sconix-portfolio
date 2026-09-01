# GitHub reorganization — plan + batch

Goal: a recruiter opens `github.com/YusufRM`, reads the profile in 10 seconds,
sees 6 strong pinned repos, and thinks "this person ships." Today the public
face is 10 repos, all SNHU coursework.

Decided: a **Sconix GitHub org**, **`pilot` as its own repo**, audit-first.

---

## 1. Secret scan — PASSED

`sconix-systems`, `sconix-research-os`, `vllm-explore`, `sconix-portfolio`:
no API keys, `.env`, SOPS age key, or private keys in tracked files or history.
`os/.age/`, `apps/`, `secrets.env` are untracked / gitignored. Safe to make public.

---

## 2. Repo audit → buckets

### PIN (public, in the Sconix org) — the storefront, exactly 6

| repo | one-line it proves | prep needed |
|---|---|---|
| **pilot** (extract from sconix-portfolio) | ships a production agent — permissions, guardrails, incident memory, verified recovery, human-approval boundary | already polished; extract with history |
| **sconix-systems** | built the platform: manifests, typed actions, deploy-safety, one-box multi-app hosting | README is one line ("Sconix project") → write a real one |
| **sconix-research** (rename `sconix-research-os`) | the research engine — reproducible experiments, the 5090 vLLM benchmarks | README + LICENSE |
| **vllm-explore** | quantified inference numbers on an RTX 5090 (batched decode tok/s, cost) | README with the numbers table + LICENSE |
| **sconix-portfolio** | index tying the flagships together | rewrite as a thin index (pilot link + F2/F3 placeholders) |
| **F2** (doesn't exist yet) | owns a model-quality metric — base vs tuned on a held-out eval | build after the reorg |

### Supporting (public, in the org) — referenced from sconix-systems, not pinned

| repo | why | prep |
|---|---|---|
| relnotes | shipped SaaS: auth + Stripe billing loop + the agent | scan again, add README, then public |
| sconix-template-web / sconix-template | the templates the factory generates from | public as part of the org story |

### Leave public, DO NOT pin

- `cs499-eportfolio` — the CS capstone ePortfolio. One "here's my degree" link.

### Archive (public → archived; stays visible as history, stops competing)

`cs360-inventory-app`, `cs465-fullstack`, `CS340-Project-Two`,
`CS330-Computational-Graphics`, `cs370-pirate-agent`,
`cs370-pirate-intelligent-agent`, `cs210-portfolio`, `cs305-portfolio`

### Leave private (or archive-private — no one sees them either way)

`skillforge`, `trim`, `mazajj-command-center`, `learn-in-public-lab`,
`airflow-dags`, `institute`, `yemunity`, `documents-simple-next-js-*`,
`serializer`, `diagrams_generator`, `class-craft-82`, `truck_vision`,
`realty-boost-pro-44`, `concepts_cards`, `sci-nation-genesis-core`,
`yusufrm.ai`, `personal-portfolio`, `Brain-OS-1.0`, `mcc-website`,
`Personal-Life-OS`, `sconix` (old TS)

### ⚠️ Handle explicitly

- **`annotated_deep_learning_paper_implementations`** — this is **labml.ai's**
  well-known repo, not yours. Keep private + archived, or delete. **Never public
  under your name** — a recruiter who thinks you're claiming it is a disqualifier.

---

## 3. The batch (you run these)

### 3a. Pick + create the org

GitHub org names are global. Check a candidate is free, then create it in the
browser (`github.com/organizations/plan` → Free) — `gh` can't create orgs.
Candidates: `sconix`, `sconix-dev`, `getsconix`, `sconixhq`, `usesconix`.

```bash
gh api orgs/sconix --silent 2>/dev/null && echo "taken" || echo "sconix is free"
```

Set `ORG` once you've made it:

```bash
ORG=sconix-dev   # <- your org
```

### 3b. Move the Sconix repos into the org

```bash
for r in sconix-systems sconix-research-os vllm-explore sconix-portfolio \
         relnotes sconix-template-web sconix-template; do
  gh repo transfer YusufRM/$r $ORG   # or: gh api -X POST repos/YusufRM/$r/transfer -f new_owner=$ORG
done
gh repo rename sconix-research --repo $ORG/sconix-research-os
```

### 3c. Extract `pilot` to its own repo (history preserved — done locally, see §4)

```bash
gh repo create $ORG/pilot --private --source ~/pilot-export --remote origin --push
```

### 3d. Flip the flagships public (AFTER their READMEs land — §5)

```bash
for r in pilot sconix-systems sconix-research vllm-explore sconix-portfolio; do
  gh repo edit $ORG/$r --visibility public --accept-visibility-change-consequences
done
```

### 3e. Archive the coursework (stays visible, de-emphasized)

```bash
for r in cs360-inventory-app cs465-fullstack CS340-Project-Two \
         CS330-Computational-Graphics cs370-pirate-agent \
         cs370-pirate-intelligent-agent cs210-portfolio cs305-portfolio; do
  gh repo archive YusufRM/$r -y
done
```

### 3f. Neutralize the labml repo

```bash
gh repo archive YusufRM/annotated_deep_learning_paper_implementations -y
# or: gh repo delete YusufRM/annotated_deep_learning_paper_implementations --yes
```

### 3g. Pin the 6 (browser: profile → "Customize your pins")

`pilot`, `sconix-systems`, `sconix-research`, `vllm-explore`,
`sconix-portfolio`, (F2 when it exists — until then a 6th: `relnotes`).

---

## 4. `pilot` extraction — DONE

`~/pilot-export/` is a standalone git repo: 16 commits (pilot's full
slice history + a `standalone:` commit adding `LICENSE`, `.gitignore`, a build
note). Push it with:

```bash
gh repo create $ORG/pilot --private --source ~/pilot-export --remote origin --push
```

---

## 5. READMEs — DRAFTED in `reorg-readmes/`

| file | goes to |
|---|---|
| `reorg-readmes/profile-README.md` | `YusufRM/YusufRM/README.md` — fill in email/LinkedIn; sed `sconix-dev` → your org |
| `README.md` (this repo, already rewritten) | `sconix-portfolio` index |
| `reorg-readmes/sconix-systems-README.md` | `sconix-systems/README.md` (has none today) |
| `reorg-readmes/vllm-explore-README.md` | `vllm-explore/README.md` (replace) |
| `~/research/README.md` | keep as-is |

Add an MIT `LICENSE` to `sconix-systems`, `sconix-research`, `vllm-explore` —
copy `~/pilot-export/LICENSE`.

---

## 6. Order of operations

1. Create the org, `ORG=<name>`.
2. Commit the READMEs + LICENSEs into `sconix-systems` / `sconix-research-os` /
   `vllm-explore` (while still private) and push.
3. Push `$ORG/pilot` from `~/pilot-export`.
4. Transfer the Sconix repos into the org (§3b); rename `sconix-research-os` →
   `sconix-research`.
5. Flip the 5 flagships public (§3d).
6. Archive coursework (§3e) + the labml repo (§3f).
7. Paste the profile README into `YusufRM/YusufRM`.
8. Pin the 6 (§3g).
