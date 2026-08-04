# Azure Agents Learning SDK - Reinforcement Learning and Behavior Optimization

The [Azure Agents Learning SDK](https://github.com/microsoft/azure-agents-learning-sdk) is the reinforcement-learning and behavior-optimization system for the MCP agents. It improves agents **without fine-tuning model weights**: there are no GPU fine-tune jobs and no opaque update cycles. Instead, an in-process learner optimizes a small, interpretable **policy** over discrete agent-configuration choices (prompt variants, retrieval-k, tool-selection strategies), using **Azure AI Evaluation judges** as the reward signal. Cosmos DB (or the local file / in-memory store) is the authoritative system of record for every episode, reward, policy snapshot, and learning run.

- **Package**: `azure-agents-learning-sdk` (PyPI) — imported as `agent_learning`
- **CLI**: `agent-learn`

## 🎯 Overview

The SDK implements a native reinforcement-learning feedback loop that continuously improves agent responses:

```
┌──────────────────────────────────────────────────────────────────────────┐
│                   AZURE AGENTS LEARNING FEEDBACK LOOP                       │
│                                                                            │
│  1. CAPTURE EPISODES              2. SCORE WITH JUDGES                      │
│  ┌─────────────────────┐          ┌─────────────────────┐                  │
│  │ EpisodeCapture      │  ──────► │ Intent / Adherence  │                  │
│  │ (input→tools→output)│          │ / Completion judges │                  │
│  │ + policy decision   │          │ → shaped reward     │                  │
│  └─────────────────────┘          └─────────────────────┘                  │
│           ▲                                    │                            │
│           │                                    ▼                            │
│  ┌─────────────────────┐          ┌─────────────────────┐                  │
│  │ policy.choose()     │ ◄─────── │ ReinforceLearner    │                  │
│  │ selects one of N    │  Policy  │ updates policy      │                  │
│  │ discrete actions    │  update  │ logits (REINFORCE)  │                  │
│  └─────────────────────┘          └─────────────────────┘                  │
│           3. ACT ON POLICY               4. LEARN                           │
└──────────────────────────────────────────────────────────────────────────┘
```

| Step | Component | What it does |
| --- | --- | --- |
| **1. Capture** | `EpisodeCapture` | Every tool call (ask_foundry, next_best_action, …) is recorded with input, output, tool calls, and the policy decision (action id + log-prob) |
| **2. Score** | `MetricEvaluator` judges | Three Azure AI Evaluation judges — `IntentResolutionEvaluator`, `TaskAdherenceEvaluator`, `TaskCompletionEvaluator` — score the episode |
| **3. Shape** | `RewardShaper` / `RewardWriter` | Judge scores are combined into a single scalar reward in `[-1, 1]` and persisted (per-metric + aggregate) |
| **4. Learn** | `ReinforceLearner` | A REINFORCE-with-baseline update nudges the policy logits from logged episodes + rewards |
| **5. Act** | `SoftmaxPolicy` | `policy.choose()` samples one of `N` discrete actions; `get_model_deployment()` always returns the base Foundry model |

Because the policy is a softmax distribution over a handful of discrete actions, updates are tiny CPU gradient steps that run in milliseconds inside the existing Python process — no separate training infrastructure required.

## 🧩 Core Components

| Component | Import | Responsibility |
| --- | --- | --- |
| Episode capture | `agent_learning.EpisodeCapture` / `get_capture` | Wrap a turn, record tool calls, persist an `Episode` |
| Judges / metrics | `agent_learning.MetricEvaluator`, `default_metrics`, `evaluate_all` | Intent Resolution, Task Adherence, Task Completion |
| Reward shaping | `agent_learning.RewardShaper`, `RewardWriter`, `shape_episode_reward` | Collapse metrics into a scalar reward and store rewards |
| Policy | `agent_learning.SoftmaxPolicy` / `ContextualSoftmaxPolicy` | Softmax distribution over discrete `Action`s |
| Learner | `agent_learning.ReinforceLearner` | REINFORCE-with-baseline policy update |
| Runner | `agent_learning.LearningRunner` | Ties evaluate → shape → learn into one offline batch |
| Storage | `agent_learning.LearningStore`, `get_default_store`, `Cosmos/Local/InMemory` | Durable persistence of all record types |

## 🔧 How Learning Works

```
  1. CAPTURE                      2. SCORE                    3. LEARN
  ┌─────────────────┐            ┌─────────────────┐         ┌───────────────────┐
  │ ask_foundry runs│  ───────►  │ learning_score_ │ ──────► │ learning_run_     │
  │ EpisodeCapture  │            │ episode (judges)│         │ training          │
  │ stores Episode  │            │ writes rewards  │         │ REINFORCE update  │
  └─────────────────┘            └─────────────────┘         └───────────────────┘
       │                              │                            │
       ▼                              ▼                            ▼
   learning_episodes             learning_rewards /           learning_policies /
   (Cosmos)                      learning_metrics             learning_runs
```

Unlike a fine-tuning pipeline, there is **no dataset build, no Azure OpenAI fine-tune job, and no tuned-model deployment**. The "trained artifact" is a versioned `PolicySnapshot` (action logits + baseline) stored in `learning_policies`.

### Reward signal (the judges)

Each episode is scored by three tiered judges whose normalized scores are combined by the `RewardShaper` using configurable weights:

| Weight | Env var | Default |
| --- | --- | --- |
| Intent resolution | `AGENT_LEARNING_W_INTENT` | 0.10 |
| Task adherence | `AGENT_LEARNING_W_ADHERENCE` | 0.20 |
| Task completion | `AGENT_LEARNING_W_COMPLETION` | 0.50 |

When the judge configuration (`AGENT_LEARNING_JUDGE_ENDPOINT` / `AGENT_LEARNING_JUDGE_DEPLOYMENT`) is missing, the SDK skips evaluation so unit tests still pass. You can also assign rewards manually with `learning_assign_reward`.

### The learner

`ReinforceLearner` applies a REINFORCE-with-baseline update over recent episodes and their aggregate rewards:

| Hyperparameter | Env var | Default |
| --- | --- | --- |
| Learning rate | `AGENT_LEARNING_LR` | 0.05 |
| Baseline (EMA) decay | `AGENT_LEARNING_BASELINE_DECAY` | 0.9 |
| Entropy bonus | `AGENT_LEARNING_ENTROPY_BONUS` | 0.01 |

## 🛠️ Accessing the Learning Loop

The workflow is exposed via the `agent-learn` CLI and via MCP tools on the agent server.

### CLI

```bash
# 1. Create the initial policy from a JSON list of actions
agent-learn init-policy --agent-id mcp-agents --actions ./actions.json

# 2. Run one offline learning batch over recent episodes
agent-learn train --agent-id mcp-agents --limit 500

# 3. Inspect the current policy snapshot
agent-learn policy --agent-id mcp-agents

# (optional) Score episodes without updating the policy
agent-learn score --agent-id mcp-agents --limit 100
```

### MCP Tools (12)

| # | Tool | Category | Description |
| --- | --- | --- | --- |
| 1 | `learning_list_episodes` | Episodes | List captured episodes |
| 2 | `learning_get_episode` | Episodes | Get episode details |
| 3 | `learning_assign_reward` | Rewards | Assign a manual reward/label |
| 4 | `learning_list_rewards` | Rewards | List assigned rewards |
| 5 | `learning_score_episode` | Judges | Score an episode with the judges → write rewards |
| 6 | `learning_get_metrics` | Judges | List stored judge metric results |
| 7 | `learning_init_policy` | Policy | Create/replace the softmax policy from actions |
| 8 | `learning_get_policy` | Policy | Get the latest policy snapshot + action probabilities |
| 9 | `learning_run_training` | Training | Run one offline REINFORCE learning batch |
| 10 | `learning_get_training_status` | Training | Get a learning run's status |
| 11 | `learning_list_training_runs` | Training | List learning runs |
| 12 | `learning_get_stats` | Statistics | Comprehensive statistics |

### Programmatic use

```python
from agent_learning import Action, EpisodeCapture, LearningRunner, SoftmaxPolicy

# Define the discrete action space and initialize the policy
actions = [Action(id="concise"), Action(id="detailed")]
policy = SoftmaxPolicy.from_actions(actions, agent_id="mcp-agents")

# At inference time
decision = policy.choose()
capture = EpisodeCapture()
ctx = capture.start(
    user_input="Summarise Q3 sales",
    policy_id=policy.snapshot().id,
    policy_version=policy.snapshot().version,
    action_id=decision.action.id,
    action_logprob=decision.logprob,
)
# … run the agent, record tool calls, produce output …
capture.end(ctx, assistant_output="…")

# Periodically (cron, manual, event-driven): one training step
runner = LearningRunner(policy=policy)
run = runner.run_offline_batch("mcp-agents", episode_limit=500)
```

## 🗄️ Cosmos DB Schema

The SDK persists to a dedicated Cosmos DB database (`agent_learning`) with five containers, all partitioned by `agent_id`:

| Container | Contents | Partition key |
| --- | --- | --- |
| `learning_episodes` | Agent interactions (input, tool calls, output, policy decision) | `agent_id` |
| `learning_rewards` | Per-metric + aggregate rewards attached to episodes | `agent_id` |
| `learning_metrics` | Raw judge metric results per episode | `agent_id` |
| `learning_policies` | Versioned softmax policy snapshots (logits + baseline) | `agent_id` |
| `learning_runs` | Offline REINFORCE learning-run records | `agent_id` |

The containers are provisioned by [infra/app/learning-cosmos.bicep](../infra/app/learning-cosmos.bicep). Storage is pluggable — set `AGENT_LEARNING_STORE_BACKEND` to `memory` (default), `local`, or `cosmos`.

## ⚙️ Configuration

All settings are read from environment variables (every variable is optional):

| Variable | Description | Default |
| --- | --- | --- |
| `AGENT_LEARNING_STORE_BACKEND` | `memory`, `local`, or `cosmos` | `memory` |
| `AGENT_LEARNING_AGENT_ID` | Default agent id for capture and tools | `default` (`mcp-agents` on AKS) |
| `AGENT_LEARNING_ENABLE_CAPTURE` | Capture agent interactions as episodes | `false` |
| `AGENT_LEARNING_COSMOS_ENDPOINT` | Cosmos account URL (when backend is `cosmos`) | unset |
| `AGENT_LEARNING_COSMOS_DATABASE` | Cosmos database name | `agent_learning` |
| `AGENT_LEARNING_JUDGE_ENDPOINT` | Azure OpenAI endpoint for the judges | unset |
| `AGENT_LEARNING_JUDGE_DEPLOYMENT` | Judge deployment name | unset |
| `AGENT_LEARNING_W_INTENT` / `_W_ADHERENCE` / `_W_COMPLETION` | Reward weights | 0.10 / 0.20 / 0.50 |
| `AGENT_LEARNING_LR` / `_BASELINE_DECAY` | Learner hyperparameters | 0.05 / 0.9 |

## 🔐 Security & Governance

- **Redaction**: `EpisodeCapture` redacts well-known secret patterns (bearer tokens, API keys, connection strings) from tool arguments and outputs before persistence.
- **Least privilege**: The learning store authenticates to Cosmos DB via managed identity (`AGENT_LEARNING_COSMOS_AUTH_MODE=aad`) — no keys in code.
- **Lineage**: Every episode, reward, metric result, policy snapshot, and learning run is captured for a complete audit trail of how the policy evolved.
- **Interpretability**: The policy is a small softmax over named actions, so its behavior is fully inspectable via `learning_get_policy`.
