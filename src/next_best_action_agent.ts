import { createHash, randomUUID } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

import { CosmosClient, type Container } from "@azure/cosmos";
import { DefaultAzureCredential, getBearerTokenProvider } from "@azure/identity";
import { AzureOpenAI } from "openai";

const PORT = Number.parseInt(process.env.PORT ?? "8000", 10);
const MCP_BASE_PATH = "/runtime/webhooks/mcp";
const FOUNDRY_PROJECT_ENDPOINT = process.env.FOUNDRY_PROJECT_ENDPOINT ?? "";
const FOUNDRY_MODEL_DEPLOYMENT_NAME =
  process.env.FOUNDRY_MODEL_DEPLOYMENT_NAME ?? "gpt-4o-mini";
const EMBEDDING_MODEL_DEPLOYMENT_NAME =
  process.env.EMBEDDING_MODEL_DEPLOYMENT_NAME ?? "text-embedding-3-large";
const AZURE_OPENAI_API_VERSION =
  process.env.AZURE_OPENAI_API_VERSION ?? "2024-10-21";
const COSMOSDB_ENDPOINT = process.env.COSMOSDB_ENDPOINT ?? "";
const COSMOSDB_DATABASE_NAME = process.env.COSMOSDB_DATABASE_NAME ?? "mcpdb";
const LOCAL_EMBEDDING_DIMENSIONS = 3072;

interface JsonRpcRequest {
  jsonrpc?: string;
  id?: string | number | null;
  method?: string;
  params?: Record<string, unknown>;
}

interface TaskDocument {
  id: string;
  task: string;
  intent: string;
  embedding: number[];
  created_at: string;
}

interface SimilarTask {
  id: string;
  task: string;
  intent: string;
  similarity: number;
}

interface PlanStep {
  step: number;
  action: string;
  description: string;
  estimated_effort: string;
}

interface TaskAnalysis {
  intent: string;
  steps: PlanStep[];
}

interface CosmosContainers {
  tasks: Container;
  plans: Container;
}

const localTasks: TaskDocument[] = [];
let credential: DefaultAzureCredential | undefined;
let openAIClient: AzureOpenAI | undefined;
let cosmosContainers: CosmosContainers | undefined;

function getCredential(): DefaultAzureCredential {
  credential ??= new DefaultAzureCredential();
  return credential;
}

function getOpenAIClient(): AzureOpenAI | undefined {
  if (!FOUNDRY_PROJECT_ENDPOINT) {
    return undefined;
  }

  if (!openAIClient) {
    const endpoint = FOUNDRY_PROJECT_ENDPOINT.split("/api/projects")[0]?.replace(
      /\/$/,
      "",
    );
    if (!endpoint) {
      return undefined;
    }

    openAIClient = new AzureOpenAI({
      endpoint,
      apiVersion: AZURE_OPENAI_API_VERSION,
      azureADTokenProvider: getBearerTokenProvider(
        getCredential(),
        "https://cognitiveservices.azure.com/.default",
      ),
    });
  }

  return openAIClient;
}

function getCosmosContainers(): CosmosContainers | undefined {
  if (!COSMOSDB_ENDPOINT) {
    return undefined;
  }

  if (!cosmosContainers) {
    const client = new CosmosClient({
      endpoint: COSMOSDB_ENDPOINT,
      aadCredentials: getCredential(),
    });
    const database = client.database(COSMOSDB_DATABASE_NAME);
    cosmosContainers = {
      tasks: database.container("tasks"),
      plans: database.container("plans"),
    };
  }

  return cosmosContainers;
}

function inferIntent(task: string): string {
  const normalized = task.toLowerCase();
  if (/churn|cancel|retention|at-risk customer/.test(normalized)) {
    return "customer_churn_prediction";
  }
  if (/ci\/cd|pipeline|kubernetes|deploy|microservice/.test(normalized)) {
    return "cicd_pipeline_implementation";
  }
  if (/rest api|authentication|user management|authorization/.test(normalized)) {
    return "user_management_api_design";
  }
  return "next_best_action_planning";
}

function canonicalToken(token: string): string {
  const aliases: Record<string, string> = {
    cancellation: "churn",
    cancel: "churn",
    cancelled: "churn",
    customer: "customer",
    customers: "customer",
    user: "customer",
    users: "customer",
    predictive: "prediction",
    predict: "prediction",
    predicts: "prediction",
    ci: "cicd",
    cd: "cicd",
    deployment: "deploy",
    deploying: "deploy",
    k8s: "kubernetes",
  };
  return aliases[token] ?? token;
}

function addHashedFeature(vector: number[], feature: string, weight = 1): void {
  const digest = createHash("sha256").update(feature).digest();
  const index = digest.readUInt32BE(0) % vector.length;
  const direction = digest[4]! % 2 === 0 ? 1 : -1;
  vector[index] = (vector[index] ?? 0) + direction * weight;
}

function localEmbedding(text: string): number[] {
  const vector = Array<number>(LOCAL_EMBEDDING_DIMENSIONS).fill(0);
  const tokens = text.toLowerCase().match(/[a-z0-9]+/g) ?? [];

  for (const token of tokens) {
    addHashedFeature(vector, canonicalToken(token));
  }
  addHashedFeature(vector, `intent:${inferIntent(text)}`, 4);

  const magnitude = Math.sqrt(vector.reduce((sum, value) => sum + value * value, 0));
  return magnitude === 0 ? vector : vector.map((value) => value / magnitude);
}

async function generateEmbedding(task: string): Promise<number[]> {
  const client = getOpenAIClient();
  if (!client) {
    return localEmbedding(task);
  }

  try {
    const response = await client.embeddings.create({
      model: EMBEDDING_MODEL_DEPLOYMENT_NAME,
      input: task,
    });
    const embedding = response.data[0]?.embedding;
    return embedding && embedding.length > 0 ? embedding : localEmbedding(task);
  } catch (error) {
    console.warn("Azure embedding failed; using the local embedding fallback.", error);
    return localEmbedding(task);
  }
}

function cosineSimilarity(left: number[], right: number[]): number {
  if (left.length !== right.length || left.length === 0) {
    return 0;
  }

  let dotProduct = 0;
  let leftMagnitude = 0;
  let rightMagnitude = 0;
  for (let index = 0; index < left.length; index += 1) {
    const leftValue = left[index] ?? 0;
    const rightValue = right[index] ?? 0;
    dotProduct += leftValue * rightValue;
    leftMagnitude += leftValue * leftValue;
    rightMagnitude += rightValue * rightValue;
  }

  if (leftMagnitude === 0 || rightMagnitude === 0) {
    return 0;
  }
  return dotProduct / Math.sqrt(leftMagnitude * rightMagnitude);
}

async function findSimilarTasks(embedding: number[]): Promise<SimilarTask[]> {
  let candidates = [...localTasks];
  const containers = getCosmosContainers();

  if (containers) {
    try {
      const query = {
        query:
          "SELECT c.id, c.task, c.intent, c.embedding, c.created_at FROM c WHERE IS_DEFINED(c.embedding)",
      };
      const { resources } = await containers.tasks.items
        .query<TaskDocument>(query)
        .fetchAll();
      const knownIds = new Set(candidates.map((candidate) => candidate.id));
      candidates = candidates.concat(
        resources.filter((candidate) => !knownIds.has(candidate.id)),
      );
    } catch (error) {
      console.warn("Cosmos task lookup failed; using in-memory history.", error);
    }
  }

  return candidates
    .map((candidate) => ({
      id: candidate.id,
      task: candidate.task,
      intent: candidate.intent,
      similarity: cosineSimilarity(embedding, candidate.embedding),
    }))
    .filter((candidate) => candidate.similarity >= 0.7)
    .sort((left, right) => right.similarity - left.similarity)
    .slice(0, 5);
}

function fallbackPlan(task: string): TaskAnalysis {
  return {
    intent: inferIntent(task),
    steps: [
      {
        step: 1,
        action: "Define the outcome",
        description: `Confirm the success criteria, scope, and constraints for: ${task}`,
        estimated_effort: "30 minutes",
      },
      {
        step: 2,
        action: "Prepare the inputs",
        description: "Collect the required data, access, dependencies, and baseline measurements.",
        estimated_effort: "1-2 hours",
      },
      {
        step: 3,
        action: "Implement the solution",
        description: "Build the smallest complete solution and record the decisions that affect operation and support.",
        estimated_effort: "1-2 days",
      },
      {
        step: 4,
        action: "Validate and release",
        description: "Test against the success criteria, address failures, and deploy with monitoring and rollback guidance.",
        estimated_effort: "2-4 hours",
      },
    ],
  };
}

function parseTaskAnalysis(content: string, task: string): TaskAnalysis {
  const fallback = fallbackPlan(task);
  const start = content.indexOf("{");
  const end = content.lastIndexOf("}");
  if (start < 0 || end <= start) {
    return fallback;
  }

  try {
    const parsed = JSON.parse(content.slice(start, end + 1)) as Partial<TaskAnalysis>;
    if (typeof parsed.intent !== "string" || !Array.isArray(parsed.steps)) {
      return fallback;
    }

    const steps = parsed.steps
      .filter(
        (step): step is PlanStep =>
          typeof step === "object" &&
          step !== null &&
          typeof step.action === "string" &&
          typeof step.description === "string",
      )
      .map((step, index) => ({
        step: index + 1,
        action: step.action,
        description: step.description,
        estimated_effort:
          typeof step.estimated_effort === "string"
            ? step.estimated_effort
            : "To be estimated",
      }));

    return steps.length > 0 ? { intent: parsed.intent, steps } : fallback;
  } catch {
    return fallback;
  }
}

async function analyzeAndPlan(
  task: string,
  similarTasks: SimilarTask[],
): Promise<TaskAnalysis> {
  const client = getOpenAIClient();
  if (!client) {
    return fallbackPlan(task);
  }

  try {
    const similarContext = similarTasks
      .map((item) => `- ${item.task} (${item.intent})`)
      .join("\n");
    const response = await client.chat.completions.create({
      model: FOUNDRY_MODEL_DEPLOYMENT_NAME,
      messages: [
        {
          role: "system",
          content:
            "Analyze the task and return only JSON with this shape: " +
            '{"intent":"short_snake_case_intent","steps":[{"step":1,"action":"short action","description":"specific instruction","estimated_effort":"estimate"}]}. ' +
            "Return three to six practical steps.",
        },
        {
          role: "user",
          content: `Task: ${task}\n\nSimilar completed tasks:\n${similarContext || "None"}`,
        },
      ],
    });
    return parseTaskAnalysis(response.choices[0]?.message.content ?? "", task);
  } catch (error) {
    console.warn("Azure task analysis failed; using the local planning fallback.", error);
    return fallbackPlan(task);
  }
}

async function persistResult(
  taskDocument: TaskDocument,
  steps: PlanStep[],
  similarTasks: SimilarTask[],
): Promise<boolean> {
  localTasks.push(taskDocument);
  if (localTasks.length > 100) {
    localTasks.shift();
  }

  const containers = getCosmosContainers();
  if (!containers) {
    return false;
  }

  try {
    await containers.tasks.items.upsert(taskDocument);
    await containers.plans.items.upsert({
      id: randomUUID(),
      taskId: taskDocument.id,
      task: taskDocument.task,
      intent: taskDocument.intent,
      steps,
      similar_tasks_referenced: similarTasks.map((item) => ({
        id: item.id,
        similarity: item.similarity,
      })),
      created_at: taskDocument.created_at,
      status: "planned",
    });
    return true;
  } catch (error) {
    console.warn("Cosmos persistence failed; result remains in memory.", error);
    return false;
  }
}

async function nextBestAction(task: string): Promise<Record<string, unknown>> {
  const taskId = randomUUID();
  const createdAt = new Date().toISOString();
  const embedding = await generateEmbedding(task);
  const similarTasks = await findSimilarTasks(embedding);
  const analysis = await analyzeAndPlan(task, similarTasks);
  const storedInCosmos = await persistResult(
    {
      id: taskId,
      task,
      intent: analysis.intent,
      embedding,
      created_at: createdAt,
    },
    analysis.steps,
    similarTasks,
  );

  return {
    task_id: taskId,
    task,
    intent: analysis.intent,
    analysis: {
      similar_tasks_found: similarTasks.length,
      similar_tasks: similarTasks.map((item) => ({
        task: item.task,
        intent: item.intent,
        similarity_score: Number(item.similarity.toFixed(3)),
      })),
    },
    plan: {
      steps: analysis.steps,
      total_steps: analysis.steps.length,
    },
    metadata: {
      created_at: createdAt,
      embedding_dimensions: embedding.length,
      stored_in_cosmos: storedInCosmos,
      storage_mode: storedInCosmos ? "cosmos" : "memory",
    },
  };
}

function sendJson(
  response: ServerResponse,
  statusCode: number,
  body: Record<string, unknown>,
): void {
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  response.end(JSON.stringify(body));
}

async function readJsonBody(request: IncomingMessage): Promise<JsonRpcRequest> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > 1_048_576) {
      throw new Error("Request body exceeds 1 MiB");
    }
    chunks.push(buffer);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8")) as JsonRpcRequest;
}

async function handleMcpMessage(
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  let body: JsonRpcRequest;
  try {
    body = await readJsonBody(request);
  } catch (error) {
    sendJson(response, 400, {
      jsonrpc: "2.0",
      error: { code: -32700, message: `Parse error: ${String(error)}` },
      id: null,
    });
    return;
  }

  const id = body.id ?? null;
  if (body.jsonrpc !== "2.0") {
    sendJson(response, 400, {
      jsonrpc: "2.0",
      error: { code: -32600, message: "Invalid Request" },
      id,
    });
    return;
  }

  if (body.method === "initialize") {
    sendJson(response, 200, {
      jsonrpc: "2.0",
      result: {
        protocolVersion: "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name: "mcp-agents-typescript", version: "1.0.0" },
      },
      id,
    });
    return;
  }

  if (body.method === "tools/list") {
    sendJson(response, 200, {
      jsonrpc: "2.0",
      result: {
        tools: [
          {
            name: "next_best_action",
            description:
              "Analyze a task, find similar prior tasks, and generate a practical action plan.",
            inputSchema: {
              type: "object",
              properties: {
                task: { type: "string", description: "Task to analyze" },
              },
              required: ["task"],
              additionalProperties: false,
            },
          },
        ],
      },
      id,
    });
    return;
  }

  if (body.method === "tools/call") {
    const toolName = body.params?.name;
    const argumentsValue = body.params?.arguments;
    const task =
      typeof argumentsValue === "object" &&
      argumentsValue !== null &&
      "task" in argumentsValue &&
      typeof argumentsValue.task === "string"
        ? argumentsValue.task.trim()
        : "";

    if (toolName !== "next_best_action" || !task) {
      sendJson(response, 200, {
        jsonrpc: "2.0",
        result: {
          content: [
            {
              type: "text",
              text:
                toolName === "next_best_action"
                  ? "The task argument must be a non-empty string."
                  : `Unknown tool: ${String(toolName)}`,
            },
          ],
          isError: true,
        },
        id,
      });
      return;
    }

    try {
      const result = await nextBestAction(task);
      sendJson(response, 200, {
        jsonrpc: "2.0",
        result: {
          content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
          isError: false,
        },
        id,
      });
    } catch (error) {
      sendJson(response, 200, {
        jsonrpc: "2.0",
        result: {
          content: [{ type: "text", text: `next_best_action failed: ${String(error)}` }],
          isError: true,
        },
        id,
      });
    }
    return;
  }

  sendJson(response, 400, {
    jsonrpc: "2.0",
    error: { code: -32601, message: `Method not found: ${String(body.method)}` },
    id,
  });
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", `http://${request.headers.host ?? "localhost"}`);

  if (request.method === "GET" && url.pathname === "/health") {
    sendJson(response, 200, {
      status: "healthy",
      timestamp: new Date().toISOString(),
    });
    return;
  }

  if (request.method === "GET" && url.pathname === `${MCP_BASE_PATH}/sse`) {
    const sessionId = randomUUID();
    response.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });
    response.write(`data: message?sessionId=${sessionId}\n\n`);
    const keepAlive = setInterval(() => response.write(": keepalive\n\n"), 30_000);
    request.on("close", () => clearInterval(keepAlive));
    return;
  }

  if (request.method === "POST" && url.pathname === `${MCP_BASE_PATH}/message`) {
    await handleMcpMessage(request, response);
    return;
  }

  if (request.method === "GET" && url.pathname === "/") {
    sendJson(response, 200, {
      name: "MCP Server",
      version: "1.0.0",
      implementation: "typescript",
      endpoints: {
        sse: `${MCP_BASE_PATH}/sse`,
        message: `${MCP_BASE_PATH}/message`,
        health: "/health",
      },
    });
    return;
  }

  sendJson(response, 404, { error: "Not found" });
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`TypeScript next_best_action MCP server listening on port ${PORT}`);
});

function shutdown(signal: string): void {
  console.log(`Received ${signal}; shutting down.`);
  server.close(() => process.exit(0));
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));