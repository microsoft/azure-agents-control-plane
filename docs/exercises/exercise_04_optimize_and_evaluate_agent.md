# Exercise 4: Optimize and Evaluate Agent

**Duration:** 1 hour

## Overview

In this exercise, you will establish a **baseline evaluation** of your agent, use the **Azure Agents Learning SDK** to capture episodes and learn an action-selection **policy** (in-process reinforcement learning — no model-weight fine-tuning), and then **re-evaluate to measure improvement**. This closed-loop approach ensures that reinforcement learning produces measurable, validated gains.

---

## Why Evaluate Before and After Learning?

Optimizing without measurement is guesswork. The evaluation framework provides three complementary dimensions that together capture whether the agent actually improved:

| Evaluator | Scale | What it Measures | Why It Matters for RL |
|-----------|-------|------------------|----------------------|
| **Intent Resolution** | 1-5 | Does the agent correctly understand user intent? | Learning should sharpen intent classification |
| **Tool Call Accuracy** | 1-5 | Does the agent select the right tools with correct parameters? | This is the **primary behavior** the policy optimizes |
| **Task Adherence** | flagged true/false | Does the agent complete the assigned task correctly? | End-to-end quality gate on response quality |

Without before/after evaluation, you cannot distinguish a policy that improved from one that regressed or stayed flat.

---

## Azure Agents Learning SDK + Evaluation Loop

The [Azure Agents Learning SDK](https://github.com/microsoft/azure-agents-learning-sdk) runs an in-process REINFORCE learner that optimizes a small softmax **policy** over discrete agent-configuration choices (prompt variants, retrieval strategies, …), using the Azure AI Evaluation judges as the reward signal. There is **no dataset build, no Azure OpenAI fine-tune job, and no tuned-model deployment** — the learned artifact is a versioned policy snapshot.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    LEARNING & EVALUATION CLOSED LOOP                      │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  1. BASELINE EVAL                 2. CAPTURE EPISODES                    │
│  ┌─────────────────────┐          ┌─────────────────────┐                │
│  │ Run evals on the    │          │ User asks question  │                │
│  │ agent: intent,      │ ───────► │ Agent responds      │                │
│  │ tool, task scores   │          │ Episode + policy    │                │
│  │ Record baseline     │          │ decision stored     │                │
│  └─────────────────────┘          └──────────┬──────────┘                │
│                                              │                           │
│  6. POST-LEARNING EVAL            3. SCORE   ▼   4. INIT & LEARN         │
│  ┌─────────────────────┐          ┌─────────────────────┐                │
│  │ Re-run SAME evals   │ ◄─────── │ Judges score episodes│               │
│  │ Compare to baseline │  Policy  │ Init softmax policy  │               │
│  │ Gate: must improve! │  update  │ Run REINFORCE batch  │               │
│  └─────────────────────┘          └─────────────────────┘                │
│         │                                                                │
│         ▼                                                                │
│  ┌──────────────────────┐                                                │
│  │ 7. DECISION GATE     │                                                │
│  │ Improved? → Keep     │                                                │
│  │ Regressed? → Re-run  │                                                │
│  │ Flat? → More data    │                                                │
│  └──────────────────────┘                                                │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Step 4.1: Prepare Evaluation Dataset with GitHub Copilot

Before any learning, create a consistent evaluation dataset that will be used for both baseline and post-learning measurement. Use GitHub Copilot in Agent Mode to generate evaluation data grounded in your agent's specification, task instructions, and ontology facts.

### Generate Evaluation Data with Copilot

**Prompt Copilot (Agent Mode) with:**
> "Generate an evaluation dataset for my autonomous agent. Review the SpecKit specification file at `.speckit/specifications/` that corresponds to my agent's use case. Also review the domain-specific task instruction documents in `task_instructions/` and the ontology fact files in `facts/ontology/` to understand the agent's domain, intents, tools, and expected behaviors. Additionally, review the existing evaluation data in `evals/next_best_action_eval_data.jsonl` as a reference for the expected JSONL format and structure. Using all of this context, generate a JSONL evaluation file at `evals/autonomous_agent_eval.jsonl`. Each line should be a JSON object with: query, expected_intent (derived from the spec's workflow intents), expected_tools (derived from the spec's MCP Tool Catalog), expected_response_contains (keywords from the task instructions and ontology), and context (user roles from the spec's security requirements). Generate at least 8 diverse test cases covering the full range of intents, tools, and edge cases defined in the specification."

### What Copilot Will Do

Copilot will:
1. Read your agent's SpecKit specification to extract defined intents, tools, and workflows
2. Read the task instruction files in `task_instructions/` to extract domain-specific queries and expected outcomes
3. Read the ontology facts in `facts/ontology/` to extract entity types, properties, and domain terminology
4. Read the existing `evals/next_best_action_eval_data.jsonl` to match the expected JSONL format
5. Synthesize all of this into a coherent evaluation dataset that covers your agent's capabilities

### Verify the Generated Dataset

**Prompt Copilot (Agent Mode) with:**
> "Read the generated `evals/autonomous_agent_eval.jsonl` and verify that: (1) each line is valid JSON, (2) the expected_intent values match intents from the specification, (3) the expected_tools reference tools from the spec's MCP Tool Catalog, and (4) there are at least 8 test cases covering different intents. Report a summary."

> **Important:** Use the **same evaluation dataset** for both baseline and post-learning evaluation. This is the only way to produce a valid comparison.

---

## Step 4.2: Run Baseline Evaluation (Before Learning) with Copilot

Establish baseline scores **before** any policy learning. These scores are your control group.

**Prompt Copilot (Agent Mode) with:**
> "Set up a kubectl port-forward from the autonomous-agent service in the mcp-agents namespace on port 8080:80. Then check if a .venv virtual environment exists in the project root — if it doesn't, create one. Activate it, install the dependencies from src/requirements.txt, and run the baseline evaluation using: `python -m evals.evaluate_next_best_action --data evals/next_best_action_eval_data.jsonl --out evals/eval_results --direct --strict`. After the evaluation completes, read the generated `evals/eval_results/eval_summary_*.json` file and display the baseline scores for Intent Resolution, Tool Call Accuracy, and Task Adherence."

### Review Baseline Results

Copilot will display your baseline scores. Record them here — you will compare against these after learning:

| Evaluator | Baseline Score | Threshold | Status |
|-----------|---------------|-----------|--------|
| Intent Resolution | ___ / 5 | ≥ 3 | |
| Tool Call Accuracy | ___ / 5 | ≥ 3 | |
| Task Adherence | pass / fail | not flagged | |
| **Overall** | ___ | all pass | |

> **Checkpoint:** If baseline scores are already very high (e.g., all 5/5), policy learning may yield diminishing returns. Focus on the dimensions where the agent scores lowest.

---

## Step 4.3: Enable Episode Capture with Copilot

Now enable episode capture to collect training data for the learner.

**Prompt Copilot (Agent Mode) with:**
> "Enable Azure Agents Learning SDK episode capture for the autonomous agent and approval agent deployments on AKS. Set `AGENT_LEARNING_ENABLE_CAPTURE=true` (and `AGENT_LEARNING_STORE_BACKEND=cosmos`) on both the `autonomous-agent` and `approval-agent` deployments in the `mcp-agents` namespace using kubectl set env. Then restart both deployments with kubectl rollout restart and verify they are running with the new environment variables."

---

## Step 4.4: Generate Agent Interactions with Copilot

Make several requests to generate episodes for training data.

**Prompt Copilot (Agent Mode) with:**
> "Set up a kubectl port-forward from the autonomous-agent service in the mcp-agents namespace on port 8080:80. Then activate the `.venv` virtual environment and run `python scripts/generate_episodes.py` to send each evaluation query from `evals/autonomous_agent_eval.jsonl` to the agent. The script connects to the MCP SSE endpoint at `http://localhost:8080/runtime/webhooks/mcp/sse`, establishes a session, and sends JSON-RPC tool calls for each query with a 2-second pause between requests. Display the output showing how many episodes were generated successfully."

> **Tip:** You can also run `python scripts/generate_episodes.py --list-tools` to see all available tools on the agent before generating episodes.

---

## Step 4.5: Review Captured Episodes with Copilot

Query captured episodes via the MCP API.

**Prompt Copilot (Agent Mode) with:**
> "Set up a kubectl port-forward from the mcp-agents service in the mcp-agents namespace on port 8000:80. Then activate the `.venv` virtual environment and run `python scripts/list_episodes.py --port 8000 --agent-id mcp-agents --limit 10` to list the most recent 10 captured episodes. The script connects to the MCP SSE endpoint and calls the `learning_list_episodes` tool. Display the results and summarize how many episodes were captured."

### Episode Structure

Each episode records the interaction **and** the policy decision (which action the policy chose), so the learner can replay the decision:

```json
{
  "id": "episode-abc123",
  "agent_id": "autonomous-agent",
  "user_input": "Analyze customer churn for Q4 2025",
  "assistant_output": "Churn rate is 12%; 450 customers at risk...",
  "tool_calls": [{ "name": "analyze_churn", "arguments": { "period": "Q4 2025" } }],
  "policy_id": "policy-xyz",
  "policy_version": 0,
  "action_id": "detailed",
  "action_logprob": -0.69,
  "created_at": "2026-02-07T10:30:00Z"
}
```

---

## Step 4.6: Produce Rewards with Copilot

Rewards can come from the **Azure AI Evaluation judges** (recommended) or from manual/heuristic labels.

### Score with the Judges

**Prompt Copilot (Agent Mode) with:**
> "For each captured episode, run the Azure AI Evaluation judges to produce a reward. Ensure `AGENT_LEARNING_JUDGE_ENDPOINT` and `AGENT_LEARNING_JUDGE_DEPLOYMENT` are configured on the deployment. Then, with the kubectl port-forward active on port 8000, call the `learning_score_episode` MCP tool for each episode id returned by `learning_list_episodes`. This writes per-metric (intent resolution, task adherence, task completion) and aggregate rewards. Display the aggregate reward for each scored episode."

### Automated Heuristic Labeling (alternative)

**Prompt Copilot (Agent Mode) with:**
> "Set up a kubectl port-forward from the mcp-agents service in the mcp-agents namespace on port 8000:80. Then activate the `.venv` virtual environment and run `python scripts/label_episodes.py --port 8000 --agent-id mcp-agents --limit 20` to automatically label captured episodes with rewards. The script connects to the MCP SSE endpoint, lists all episodes via `learning_list_episodes`, scores each one using quality heuristics (tool correctness, output completeness, error detection), and assigns rewards via `learning_assign_reward`. Display the output showing the reward distribution summary."

### Manual Override

If you want to override specific episode labels, you can call the `learning_assign_reward` MCP tool directly for individual episodes.

### Reward Guidelines

| Reward Score | Quality Level | Criteria |
|--------------|---------------|----------|
| 0.9 - 1.0 | Excellent | Correct tool, complete response, accurate data |
| 0.7 - 0.89 | Good | Correct tool, mostly complete response |
| 0.5 - 0.69 | Acceptable | Partially correct, minor issues |
| 0.3 - 0.49 | Poor | Wrong approach but recoverable |
| -1.0 - 0.29 | Failed | Wrong tool, incorrect response, errors |

---

## Step 4.7: Initialize the Policy with Copilot

Define the discrete **action space** the policy will optimize over, then create the policy.

**Prompt Copilot (Agent Mode) with:**
> "Initialize the softmax policy for the autonomous-agent using the `learning_init_policy` MCP tool with a small action set such as `["concise", "detailed"]` (each action is a prompt/behavior variant the agent can choose). Alternatively, run `agent-learn init-policy --agent-id autonomous-agent --actions ./actions.json`. Confirm the tool returns a policy id and version 0 with the two actions."

> **Note:** Actions are named configuration choices — prompt templates, retrieval-k settings, tool-selection strategies, etc. The learner never sees their semantics; it only optimizes the probability of choosing each one.

---

## Step 4.8: Run a Learning Batch with Copilot

Apply one offline REINFORCE-with-baseline update over the scored episodes. This runs **in-process** and completes in milliseconds — there is no external training job to monitor.

**Prompt Copilot (Agent Mode) with:**
> "Run one offline learning batch for the autonomous-agent using the `learning_run_training` MCP tool (agent_id=autonomous-agent, limit=200, score_missing=true), or run `python scripts/run_learning.py --port 8000 --agent-id autonomous-agent`. Note the returned training run id and the reported metrics (episodes_used, mean_reward, baseline_after)."

Expected output:

```json
{
  "success": true,
  "training_run_id": "run-9f3c...",
  "algorithm": "ReinforceLearner",
  "status": "succeeded",
  "episodes_used": 30,
  "metrics": { "episodes_used": 30, "mean_reward": 0.41, "baseline_after": 0.39 }
}
```

> **Cadence:** Each `learning_run_training` call applies one update and increments the policy version. Run it periodically (cron, manual, or event-driven) as new episodes accumulate.

---

## Step 4.9: Inspect the Learned Policy with Copilot

Instead of deploying a tuned model, you inspect the updated **policy** — the interpretable artifact of learning.

**Prompt Copilot (Agent Mode) with:**
> "Show the current policy for the autonomous-agent using the `learning_get_policy` MCP tool (or `agent-learn policy --agent-id autonomous-agent`). Display the policy version, the action probabilities, and the number of updates applied. Note how the probability mass has shifted toward the higher-reward action."

Expected output:

```json
{
  "has_policy": true,
  "version": 1,
  "actions": ["concise", "detailed"],
  "action_probabilities": { "concise": 0.44, "detailed": 0.56 },
  "updates_applied": 1
}
```

---

## Step 4.10: Post-Learning Evaluation (After Learning) with Copilot

Run the **same evaluation dataset** from Step 4.2 against the agent using the updated policy. This is the critical comparison.

**Prompt Copilot (Agent Mode) with:**
> "Run the post-learning evaluation using the same dataset and thresholds as the baseline in Step 4.2. Set up a kubectl port-forward from the autonomous-agent service in the mcp-agents namespace on port 8080:80. Then activate the .venv and run: `python -m evals.evaluate_next_best_action --data evals/next_best_action_eval_data.jsonl --out evals/eval_results --direct --strict`. After the evaluation completes, read the generated eval_summary JSON and compare the scores against the baseline results from Step 4.2. Display a before/after comparison table."

### Compare Before/After Results

Fill in the table below with your actual scores. Expected improvement ranges are provided based on typical policy-learning results:

| Evaluator | Baseline (Step 4.2) | After Learning | Expected Delta | Status |
|-----------|--------------------|-----------------|----------------|--------|
| Intent Resolution | ___ / 5 | ___ / 5 | +0.3 to +1.0 | |
| Tool Call Accuracy | ___ / 5 | ___ / 5 | +0.3 to +1.0 | |
| Task Adherence | pass/fail | pass/fail | fewer flags | |

### Review Learning Statistics

**Prompt Copilot (Agent Mode) with:**
> "Show a summary of the learning run using the `learning_get_stats` MCP tool for the autonomous-agent. Display the total episodes, total rewards, average aggregate reward, number of training runs, and the active policy version + action probabilities."

### Example Comparison Output

```
Evaluation Comparison: baseline → policy v1

| Metric            | Baseline | Current | Change |
|-------------------|----------|---------|--------|
| Intent Resolution | 3.2      | 4.1     | +0.9   |
| Tool Accuracy     | 2.8      | 3.7     | +0.9   |
| Task Adherence    | 60% pass | 85% pass| +25%   |

✅ All metrics improved after policy learning
```

---

## Step 4.11: Decision Gate — Did Learning Improve the Agent?

This is the most important step. Based on your evaluation comparison, take one of three actions:

| Outcome | Signal | Action |
|---------|--------|--------|
| **Improved** | All eval scores increased | Keep the current policy |
| **Regressed** | Any eval score decreased | Re-run learning with better rewards, or reset the policy |
| **Flat** | Scores unchanged (±0.2) | Collect more diverse episodes and run another batch |

### If Improved — Keep It

**Prompt Copilot (Agent Mode) with:**
> "Confirm the active policy for the autonomous-agent with the `learning_get_policy` MCP tool (or `agent-learn policy --agent-id autonomous-agent`). Record the policy version and action probabilities as the current production policy."

### If Regressed — Reset / Re-learn

**Prompt Copilot (Agent Mode) with:**
> "The eval scores regressed. Re-initialize the policy with `learning_init_policy` (resetting the logits to uniform), verify the episode rewards are correct (re-score with `learning_score_episode` if needed), and run `learning_run_training` again. Because learning is in-process, no model rollback or redeploy is required."

### If Flat — Diagnose and Retry

If scores didn't meaningfully change, the issue is usually one of:

| Root Cause | Fix |
|------------|-----|
| Too few episodes | Capture more interactions (aim for 100+) |
| Weak reward signal | Ensure the judges are configured, or raise the bar on manual labels |
| Low diversity in episodes | Add episodes covering edge cases (scheduling, investigation) |
| Learning rate too low/high | Tune `AGENT_LEARNING_LR` (default 0.05) |
| Action space too coarse | Add more meaningful action variants to the policy |

---

## Step 4.12: Store Evaluation Results with Copilot

### Store Results for Tracking

**Prompt Copilot (Agent Mode) with:**
> "Store the evaluation results for historical tracking. Run: `python -m evals.store_results --input evals/eval_results/eval_summary_*.json --agent-id autonomous-agent --version policy-v1`. Then verify the results were stored by querying Cosmos DB for evaluation records for the autonomous-agent."

---

## Completion Checklist

- [ ] Evaluation dataset created with test cases
- [ ] **Baseline evaluation** run before learning (scores recorded)
- [ ] Episode capture enabled for agents (`AGENT_LEARNING_ENABLE_CAPTURE=true`)
- [ ] Episodes generated through test interactions
- [ ] Rewards produced (judges via `learning_score_episode` and/or `learning_assign_reward`)
- [ ] Policy initialized with a discrete action set
- [ ] Learning batch run (`learning_run_training`) — policy version incremented
- [ ] Updated policy inspected (`learning_get_policy`)
- [ ] **Post-learning evaluation** re-run on same dataset
- [ ] Before/after scores compared — improvement validated
- [ ] Decision gate applied (keep, re-learn, or collect more data)
- [ ] Evaluation results stored for historical tracking

---

## Summary

You have completed the learning and evaluation exercise:

| Evaluation | Baseline | After Learning | Target | Status |
|------------|----------|----------------|--------|--------|
| Intent Resolution | ___ / 5 | ___ / 5 | ≥ 3 | |
| Tool Call Accuracy | ___ / 5 | ___ / 5 | ≥ 3 | |
| Task Adherence | | | not flagged | |
| **Overall Improved?** | | | yes | |

### Key Takeaways

1. **Evaluate before you learn** — baseline scores are your control group; without them you can't prove improvement
2. **Same dataset, same thresholds** — changing the eval between runs invalidates the comparison
3. **Three dimensions cover the full picture** — intent (understanding), tools (action), task (outcome)
4. **In-process learning is fast and interpretable** — the policy is a small softmax you can read and reason about
5. **Decision gates prevent regressions** — never keep a policy update without re-evaluating
6. **Reward quality matters** — judge configuration and episode diversity directly determine learning outcomes

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No episodes captured | Verify `AGENT_LEARNING_ENABLE_CAPTURE=true`, check Cosmos DB connection |
| `learning_score_episode` writes no rewards | The judges are not configured — set `AGENT_LEARNING_JUDGE_ENDPOINT`/`_DEPLOYMENT` |
| Policy not improving | Add more diverse episodes, verify rewards, tune `AGENT_LEARNING_LR` |
| Eval scores regressed | Re-initialize the policy and re-run learning with corrected rewards |
| Task adherence flags correct responses | Review the evaluator prompt — may need calibration for your domain |
| No policy found on training | Run `learning_init_policy` (or `agent-learn init-policy`) first |
| Flat scores after learning | More episode diversity needed; check the reward signal quality |

---

## Congratulations!

You've completed the **Build Your Own Agent** lab! Through 4 exercises, you:

1. ✅ **Reviewed** the lab objectives and solution architecture
2. ✅ **Built** an autonomous agent using GitHub Copilot with SpecKit
3. ✅ **Reviewed** security, governance, memory, and observability
4. ✅ **Optimized and evaluated** your agent using the Azure Agents Learning SDK (in-process reinforcement learning) with the Azure AI Evaluation SDK

Your agents now benefit from:
- **Enterprise security/governance** through APIM policies
- **Persistent memory** with Cosmos DB
- **Intelligent search** via AI Foundry and AI Search/FoundryIQ integration
- **Continuous improvement** via the Azure Agents Learning SDK policy learner
- **Quality assurance** through eval-gated learning with before/after measurement
- **Full observability** with Azure Monitor integration

---
