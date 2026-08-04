# Azure Agents Learning SDK End-to-End Test Results

**Test Date:** January 31, 2026  
**Test Script:** `tests/test_agent_learning.py --direct`  
**Environment:** AKS via LoadBalancer (Direct Mode)

---

## Test Environment

| Component | Value |
|-----------|-------|
| AKS Cluster | `aks-<unique-suffix>` |
| Resource Group | `rg-apim-mcp-aks-<env>` |
| Location | `eastus2` |
| LoadBalancer IP | `<loadbalancer-ip>` |
| MCP Endpoint | `http://<loadbalancer-ip>/runtime/webhooks/mcp` |
| Cosmos DB | `cosmos-<unique-suffix>-eastus2.documents.azure.com` |
| Learning Database | `agent_learning` |
| Store Backend | `cosmos` (`AGENT_LEARNING_STORE_BACKEND`) |
| Kubernetes Version | `1.32.x` |
| Pod Replicas | 2 |
| Image | `<registry>.azurecr.io/mcp-agents:latest` |

---

## Test Results Summary

### Core MCP Tools

| Test Category | Status | Details |
|---------------|--------|----------|
| SSE Connection | ✅ PASSED | Session established successfully |
| MCP Tools Discovery | ✅ PASSED | 27 total tools (12 Learning + 15 Core) |
| Episode Creation | ✅ PASSED | 5/5 episodes created via ask_foundry |
| Next Best Action | ✅ PASSED | Task processed with 9 plan steps |
| Memory Store | ✅ PASSED | 3/3 memories stored |
| Memory Recall | ✅ PASSED | 2/2 queries successful |

### Learning MCP Tools (12 Tools)

| Test | Status | Details |
|------|--------|----------|
| `learning_get_stats` | ✅ PASSED | 30 episodes, 2 rewards, 0 training runs |
| `learning_list_episodes` | ✅ PASSED | 10 episodes retrieved |
| `learning_get_episode` | ✅ PASSED | Episode details retrieved |
| `learning_assign_reward` | ✅ PASSED | Reward 0.8 assigned (human_approval) |
| `learning_list_rewards` | ✅ PASSED | 3 rewards found |
| `learning_score_episode` | ✅ PASSED | Judges scored episode → 4 rewards written |
| `learning_get_metrics` | ✅ PASSED | 3 metric results retrieved |
| `learning_init_policy` | ✅ PASSED | Policy v0 created (2 actions) |
| `learning_get_policy` | ✅ PASSED | Action probabilities retrieved |
| `learning_run_training` | ✅ PASSED | REINFORCE batch completed, policy → v1 |
| `learning_get_training_status` | ✅ PASSED | Run succeeded |
| `learning_list_training_runs` | ✅ PASSED | 1 training run listed |

**Overall Result: 🎉 ALL 12 LEARNING TESTS PASSED (12/12)**

---

## Performance Comparison

### Learning Capture Overhead

| Metric | Without Capture | With Capture | Overhead |
|--------|-----------------|--------------|----------|
| Mean Latency | 2976 ms | 2989 ms | +13 ms (+0.4%) |
| Median Latency | 2435 ms | 2488 ms | - |
| Min Latency | 1665 ms | 1629 ms | -36 ms |
| Max Latency | 4822 ms | 4725 ms | -97 ms |
| Std Dev | 1447 ms | 1389 ms | -58 ms |

**Conclusion:** ✅ **Capture overhead is negligible (<1%)**

Enabling `AGENT_LEARNING_ENABLE_CAPTURE=true` adds minimal overhead to request processing:
- Mean latency increased by only **13 ms** (0.4%)
- The overhead is within measurement noise/variance
- Episode capture appends tool calls in-memory and persists a single record at the end of the turn

### Benchmark Methodology

- **Endpoint:** LoadBalancer direct connection (port 80)
- **Iterations:** 5 requests per configuration
- **Tool tested:** `ask_foundry` (most I/O intensive)
- **Questions:** Mix of simple and complex prompts

### Technical Details

Learning capture overhead consists of:
1. **Start capture:** ~0.1 ms (UUID generation, timestamp, policy decision metadata)
2. **Record tool calls:** ~0.2 ms per call (in-memory append + secret redaction)
3. **End capture:** Store write (~50-100 ms) — in-memory / local file / Cosmos depending on backend

The REINFORCE policy update runs **offline** (via `learning_run_training` or `agent-learn train`), so it never sits on the request hot path.

---

## Detailed Test Results

### 1. SSE Connection Test

```
📡 Establishing SSE session to: http://<loadbalancer-ip>/runtime/webhooks/mcp/sse
   SSE Response Status: 200
✅ Got session URL: http://<loadbalancer-ip>/runtime/webhooks/mcp/message?sessionId=<session-id>
```

### 2. MCP Server Health

```json
{
  "status": "healthy",
  "timestamp": "<timestamp>"
}
```

### 3. MCP Tools Available

#### Learning MCP Tools (12)

| Tool | Category | Status |
|------|----------|--------|
| `learning_list_episodes` | Episodes | ✅ Available |
| `learning_get_episode` | Episodes | ✅ Available |
| `learning_assign_reward` | Rewards | ✅ Available |
| `learning_list_rewards` | Rewards | ✅ Available |
| `learning_score_episode` | Judges | ✅ Available |
| `learning_get_metrics` | Judges | ✅ Available |
| `learning_init_policy` | Policy | ✅ Available |
| `learning_get_policy` | Policy | ✅ Available |
| `learning_run_training` | Training | ✅ Available |
| `learning_get_training_status` | Training | ✅ Available |
| `learning_list_training_runs` | Training | ✅ Available |
| `learning_get_stats` | Statistics | ✅ Available |

#### Core MCP Tools (4)

| Tool | Status |
|------|--------|
| `ask_foundry` | ✅ Available |
| `next_best_action` | ✅ Available |
| `store_memory` | ✅ Available |
| `recall_memory` | ✅ Available |

### 4. Episode Creation via `ask_foundry`

| Episode | Question | Response Status |
|---------|----------|-----------------|
| 1 | "What is the capital of France?" | ✅ "The capital of France is **Paris**." |
| 2 | "Calculate 2+2" | ✅ "2 + 2 = **4**" |
| 3 | "Explain what machine learning is in one sentence." | ✅ Success |
| 4 | "What programming language is Python named after?" | ✅ Success |
| 5 | "What is REST API?" | ✅ Success |

### 5. Judge Scoring (`learning_score_episode`)

```json
{
  "success": true,
  "episode_id": "<episode-id>",
  "rewards_written": 4,
  "aggregate_reward": 0.62,
  "rewards": [
    {"source": "metric", "metric": "intent_resolution", "value": 0.5},
    {"source": "metric", "metric": "task_adherence", "value": 0.6},
    {"source": "metric", "metric": "task_completion", "value": 0.8},
    {"source": "aggregate", "metric": null, "value": 0.62}
  ]
}
```

### 6. Policy Learning (`learning_init_policy` → `learning_run_training` → `learning_get_policy`)

```json
// learning_init_policy
{"success": true, "policy_id": "<id>", "version": 0, "actions": ["concise", "detailed"]}

// learning_run_training
{"success": true, "training_run_id": "<run-id>", "algorithm": "ReinforceLearner",
 "status": "succeeded", "episodes_used": 30,
 "metrics": {"episodes_used": 30, "mean_reward": 0.41, "baseline_after": 0.39}}

// learning_get_policy
{"has_policy": true, "version": 1, "actions": ["concise", "detailed"],
 "action_probabilities": {"concise": 0.44, "detailed": 0.56}, "updates_applied": 1}
```

---

## Notes

- The Learning SDK optimizes agent **behavior** via a softmax policy over discrete actions; it does **not** fine-tune model weights or deploy tuned models. `get_model_deployment()` always returns the base Azure AI Foundry model.
- Rewards can be produced automatically by the Azure AI Evaluation judges (`learning_score_episode`) or assigned manually (`learning_assign_reward`).
- Policy learning is idempotent per batch: each `learning_run_training` call applies one REINFORCE update and increments the policy version.
- See [AGENTS_AGENT_LEARNING_DESIGN.md](AGENTS_AGENT_LEARNING_DESIGN.md) for the full design.
