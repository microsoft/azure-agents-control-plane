"""
AKS Next Best Action Agent
FastAPI MCP Server
Implements Model Context Protocol (MCP) with SSE support
Enhanced with Microsoft Agent Framework for AI agent capabilities
Integrated with CosmosDB for task and plan storage with semantic reasoning
Features Memory Provider abstraction for short-term (CosmosDB), long-term (AI Search), and facts (Fabric IQ) memory
Includes Agent Lightning for fine-tuning and behavior optimization
"""

import json
import logging
import asyncio
import uuid
import time
import numpy as np
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential
from azure.cosmos import CosmosClient, exceptions as cosmos_exceptions
import os

# Microsoft Agent Framework imports
from agent_framework import ai_function, AIFunction
from agent_framework.azure import AzureAIAgentClient

# Memory Provider imports
from memory import (
    ShortTermMemory, MemoryEntry, MemoryType, CompositeMemory, LongTermMemory,
    AISEARCH_CONTEXT_PROVIDER_AVAILABLE,
    # Fabric IQ Facts Memory
    FactsMemory, Fact, FactSearchResult, OntologyEntity, EntityType, RelationshipType,
    # Domain ontology data generators
    CustomerDataGenerator, CustomerProfile, CustomerSegment, ChurnRiskLevel,
    PipelineDataGenerator, Pipeline, PipelineRun, PipelineStatus,
    UserAccessDataGenerator, User, AuthEvent, AuthEventType,
)

# Fabric Data Agents imports
try:
    from fabric_tools import (
        fabric_query_lakehouse_tool,
        fabric_query_warehouse_tool,
        fabric_trigger_pipeline_tool,
        fabric_get_pipeline_status_tool,
        fabric_query_semantic_model_tool,
        fabric_list_resources_tool,
        FABRIC_DATA_AGENTS_ENABLED,
    )
    FABRIC_DATA_AGENTS_AVAILABLE = True
except ImportError:
    FABRIC_DATA_AGENTS_AVAILABLE = False
    print("WARNING: fabric_tools not available - Fabric Data Agents will be disabled")

# Fabric Agent Orchestrator imports (intelligent agent layer)
try:
    from fabric_agents import (
        FabricAgentOrchestrator,
        LakehouseAgent,
        WarehouseAgent,
        PipelineAgent,
        SemanticModelAgent,
        AgentResponse as FabricAgentResponse,
        get_fabric_orchestrator,
    )
    FABRIC_AGENTS_AVAILABLE = True
except ImportError:
    FABRIC_AGENTS_AVAILABLE = False
    print("WARNING: fabric_agents not available - Fabric Agent Orchestrator will be disabled")

# OneLake file operations imports
try:
    from fabric_onelake import (
        onelake_list_files_tool,
        onelake_read_file_tool,
        onelake_write_file_tool,
        onelake_delete_file_tool,
        onelake_get_file_properties_tool,
    )
    ONELAKE_AVAILABLE = True
except ImportError:
    ONELAKE_AVAILABLE = False
    print("WARNING: fabric_onelake not available - OneLake operations will be disabled")

# Agent Lightning imports (for fine-tuning and behavior optimization)
try:
    from lightning import (
        EpisodeCaptureHook, get_capture_hook,
        DeploymentRegistry, get_deployment_registry,
        RewardWriter, get_reward_writer,
        DatasetBuilder, get_dataset_builder,
        TrainingRunner, get_training_runner,
        RLLedgerCosmos, get_rl_ledger,
        Episode, Reward, RewardSource, Dataset, TrainingRun, TrainingStatus, Deployment,
    )
    LIGHTNING_AVAILABLE = True
except ImportError:
    LIGHTNING_AVAILABLE = False

# Azure AI Evaluation SDK imports (for agent evaluators)
try:
    from azure.ai.evaluation import (
        IntentResolutionEvaluator,
        ToolCallAccuracyEvaluator,
        TaskAdherenceEvaluator,
        GroundednessEvaluator,
        RelevanceEvaluator,
    )
    EVALUATION_AVAILABLE = True
except ImportError:
    EVALUATION_AVAILABLE = False
    # Logger not yet defined, will log later in startup

# Agent 365 / Entra Agent Registry imports (for Agents approval workflows)
try:
    from agent365_approval import (
        ApprovalContract,
        ApprovalDecision,
        AgentValidationStatus,
        Agent365AvailabilityChecker,
        ApprovalWorkflowEngine,
        get_approval_workflow_engine,
        require_agents_approval,
    )
    AGENT365_APPROVAL_AVAILABLE = True
except ImportError:
    AGENT365_APPROVAL_AVAILABLE = False
    # Fallback: approval features will be disabled

from dotenv import load_dotenv

# Load environment variables
load_dotenv(override=True)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Log evaluation availability (after logger is defined)
if not EVALUATION_AVAILABLE:
    logger.warning("azure-ai-evaluation not available - evaluation tools will be disabled")

# Log Agent 365 approval availability
if AGENT365_APPROVAL_AVAILABLE:
    logger.info("Agent 365 approval workflow available - Agents tasks will require human-in-the-loop approval")
else:
    logger.warning("agent365_approval not available - Agents approval workflows will be disabled")

# Initialize FastAPI app
app = FastAPI(
    title="AKS Next Best Action MCP Server",
    description="Model Context Protocol Server for AI Agents with Semantic Reasoning",
    version="1.0.0"
)

# Azure Storage configuration
STORAGE_ACCOUNT_URL = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "")
STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")

# CosmosDB configuration
COSMOSDB_ENDPOINT = os.getenv("COSMOSDB_ENDPOINT", "")
COSMOSDB_DATABASE_NAME = os.getenv("COSMOSDB_DATABASE_NAME", "mcpdb")
COSMOSDB_TASKS_CONTAINER = "tasks"
COSMOSDB_PLANS_CONTAINER = "plans"

# Initialize storage client
if STORAGE_CONNECTION_STRING:
    blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
elif STORAGE_ACCOUNT_URL:
    credential = DefaultAzureCredential()
    blob_service_client = BlobServiceClient(account_url=STORAGE_ACCOUNT_URL, credential=credential)
else:
    logger.warning("No storage configuration found - snippet storage will not work")
    blob_service_client = None

# Initialize CosmosDB client
cosmos_client = None
cosmos_database = None
cosmos_tasks_container = None
cosmos_plans_container = None

if COSMOSDB_ENDPOINT:
    try:
        credential = DefaultAzureCredential()
        cosmos_client = CosmosClient(COSMOSDB_ENDPOINT, credential=credential)
        cosmos_database = cosmos_client.get_database_client(COSMOSDB_DATABASE_NAME)
        cosmos_tasks_container = cosmos_database.get_container_client(COSMOSDB_TASKS_CONTAINER)
        cosmos_plans_container = cosmos_database.get_container_client(COSMOSDB_PLANS_CONTAINER)
        logger.info("CosmosDB client initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize CosmosDB client: {e}")
else:
    logger.warning("COSMOSDB_ENDPOINT not configured - task storage will not work")

# Initialize Memory Providers
short_term_memory: Optional[ShortTermMemory] = None
composite_memory: Optional[CompositeMemory] = None

if COSMOSDB_ENDPOINT:
    try:
        short_term_memory = ShortTermMemory(
            endpoint=COSMOSDB_ENDPOINT,
            database_name=COSMOSDB_DATABASE_NAME,
            container_name="short_term_memory",
            default_ttl=3600,  # 1 hour default TTL
        )
        
        # Create composite memory (long-term will be added later with AI Search)
        composite_memory = CompositeMemory(
            short_term=short_term_memory,
            long_term=None,  # Will be AI Search / FoundryIQ
        )
        
        logger.info("Memory providers initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize memory providers: {e}")
else:
    logger.warning("COSMOSDB_ENDPOINT not configured - memory providers will not work")

SNIPPETS_CONTAINER = "snippets"

# In-memory session storage (replace with Redis for production)
sessions: Dict[str, Dict[str, Any]] = {}

# Microsoft Agent Framework configuration
FOUNDRY_PROJECT_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "")
FOUNDRY_MODEL_DEPLOYMENT_NAME = os.getenv("FOUNDRY_MODEL_DEPLOYMENT_NAME", "gpt-4o-mini")
EVALUATOR_MODEL_DEPLOYMENT_NAME = os.getenv("EVALUATOR_MODEL_DEPLOYMENT_NAME", "gpt-5.2-chat")
EMBEDDING_MODEL_DEPLOYMENT_NAME = os.getenv("EMBEDDING_MODEL_DEPLOYMENT_NAME", "text-embedding-3-large")

# Agent Lightning configuration for fine-tuning and behavior optimization
LIGHTNING_AGENT_ID = os.getenv("LIGHTNING_AGENT_ID", "mcp-agents")
ENABLE_LIGHTNING_CAPTURE = os.getenv("ENABLE_LIGHTNING_CAPTURE", "false").lower() == "true"
USE_TUNED_MODEL = os.getenv("USE_TUNED_MODEL", "false").lower() == "true"
TUNED_MODEL_DEPLOYMENT_NAME = os.getenv("TUNED_MODEL_DEPLOYMENT_NAME", "")  # Fallback tuned model

# Initialize Agent Lightning components (if available)
episode_capture_hook: Optional[EpisodeCaptureHook] = None
deployment_registry: Optional[DeploymentRegistry] = None
reward_writer: Optional[RewardWriter] = None
dataset_builder: Optional[DatasetBuilder] = None
training_runner: Optional[TrainingRunner] = None
rl_ledger: Optional[RLLedgerCosmos] = None

if LIGHTNING_AVAILABLE:
    try:
        episode_capture_hook = get_capture_hook()
        deployment_registry = get_deployment_registry()
        reward_writer = get_reward_writer()
        dataset_builder = get_dataset_builder()
        training_runner = get_training_runner()
        rl_ledger = get_rl_ledger()
        logger.info(f"Agent Lightning initialized (capture={ENABLE_LIGHTNING_CAPTURE}, use_tuned={USE_TUNED_MODEL})")
    except Exception as e:
        logger.warning(f"Failed to initialize Agent Lightning: {e}")
else:
    logger.info("Agent Lightning not available - fine-tuning features disabled")


def get_model_deployment() -> str:
    """
    Get the model deployment name to use.
    
    Selection order (when USE_TUNED_MODEL=true):
    1. Active tuned deployment from Cosmos DB
    2. TUNED_MODEL_DEPLOYMENT_NAME env var
    3. Base model (FOUNDRY_MODEL_DEPLOYMENT_NAME)
    """
    if not USE_TUNED_MODEL:
        return FOUNDRY_MODEL_DEPLOYMENT_NAME
    
    # Try to get active tuned model from Cosmos
    if deployment_registry:
        try:
            tuned_model = deployment_registry.get_active_model(LIGHTNING_AGENT_ID)
            if tuned_model:
                logger.debug(f"Using tuned model from Cosmos: {tuned_model}")
                return tuned_model
        except Exception as e:
            logger.warning(f"Failed to get tuned model from registry: {e}")
    
    # Fallback to env var
    if TUNED_MODEL_DEPLOYMENT_NAME:
        logger.debug(f"Using fallback tuned model: {TUNED_MODEL_DEPLOYMENT_NAME}")
        return TUNED_MODEL_DEPLOYMENT_NAME
    
    # Use base model
    return FOUNDRY_MODEL_DEPLOYMENT_NAME


# Azure AI Search configuration for long-term memory
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "")
AZURE_SEARCH_INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX_NAME", "task-instructions")
AZURE_SEARCH_KNOWLEDGE_BASE_NAME = os.getenv("AZURE_SEARCH_KNOWLEDGE_BASE_NAME", "task-instructions-kb")

# Microsoft Fabric IQ configuration for Facts Memory
FABRIC_ENABLED = os.getenv("FABRIC_ENABLED", "false").lower() == "true"
FABRIC_ENDPOINT = os.getenv("FABRIC_ENDPOINT", "")
FABRIC_WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "")
FABRIC_ONTOLOGY_NAME = os.getenv("FABRIC_ONTOLOGY_NAME", "agent-ontology")
# OneLake configuration for ontology storage (when Fabric is enabled)
FABRIC_ONELAKE_DFS_ENDPOINT = os.getenv("FABRIC_ONELAKE_DFS_ENDPOINT", "https://onelake.dfs.fabric.microsoft.com")
FABRIC_ONELAKE_BLOB_ENDPOINT = os.getenv("FABRIC_ONELAKE_BLOB_ENDPOINT", "https://onelake.blob.fabric.microsoft.com")
FABRIC_LAKEHOUSE_NAME = os.getenv("FABRIC_LAKEHOUSE_NAME", "mcpontologies")
FABRIC_ONTOLOGY_PATH = os.getenv("FABRIC_ONTOLOGY_PATH", "Files/ontology")
# Ontology storage configuration (when Fabric is disabled - uses Azure Blob Storage)
ONTOLOGY_CONTAINER_NAME = os.getenv("ONTOLOGY_CONTAINER_NAME", "ontologies")
AZURE_STORAGE_ACCOUNT_URL = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "")

# AI Search Long-Term Memory with AzureAISearchContextProvider
long_term_memory: Optional[LongTermMemory] = None

# Fabric IQ Facts Memory for ontology-grounded facts
facts_memory: Optional[FactsMemory] = None


# =========================================
# Embedding and Semantic Reasoning Helpers
# =========================================

def get_embedding(text: str) -> List[float]:
    """
    Generate embeddings for text using Azure AI Foundry's text-embedding-3-large model.
    
    Args:
        text: The text to generate embeddings for
    
    Returns:
        A list of floats representing the embedding vector (3072 dimensions)
    """
    if not FOUNDRY_PROJECT_ENDPOINT:
        raise ValueError("Foundry endpoint not configured")
    
    from openai import AzureOpenAI
    
    credential = DefaultAzureCredential()
    token = credential.get_token("https://cognitiveservices.azure.com/.default")
    
    base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
    
    client = AzureOpenAI(
        azure_endpoint=base_endpoint,
        api_key=token.token,
        api_version="2024-02-15-preview"
    )
    
    response = client.embeddings.create(
        model=EMBEDDING_MODEL_DEPLOYMENT_NAME,
        input=text
    )
    
    return response.data[0].embedding


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    Calculate cosine similarity between two vectors.
    
    Args:
        vec1: First embedding vector
        vec2: Second embedding vector
    
    Returns:
        Cosine similarity score between -1 and 1
    """
    arr1 = np.array(vec1)
    arr2 = np.array(vec2)
    
    dot_product = np.dot(arr1, arr2)
    norm1 = np.linalg.norm(arr1)
    norm2 = np.linalg.norm(arr2)
    
    if norm1 == 0 or norm2 == 0:
        return 0.0
    
    return float(dot_product / (norm1 * norm2))


def find_similar_tasks(task_embedding: List[float], threshold: float = 0.7, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Find similar tasks in CosmosDB using cosine similarity.
    
    Args:
        task_embedding: The embedding vector of the current task
        threshold: Minimum similarity score (0-1)
        limit: Maximum number of similar tasks to return
    
    Returns:
        List of similar tasks with their similarity scores
    """
    if not cosmos_tasks_container:
        return []
    
    try:
        # Query all tasks with embeddings
        query = "SELECT c.id, c.task, c.intent, c.embedding, c.created_at FROM c WHERE IS_DEFINED(c.embedding)"
        items = list(cosmos_tasks_container.query_items(query=query, enable_cross_partition_query=True))
        
        similar_tasks = []
        for item in items:
            if 'embedding' in item and item['embedding']:
                similarity = cosine_similarity(task_embedding, item['embedding'])
                if similarity >= threshold:
                    similar_tasks.append({
                        'id': item['id'],
                        'task': item.get('task', ''),
                        'intent': item.get('intent', ''),
                        'similarity': similarity,
                        'created_at': item.get('created_at', '')
                    })
        
        # Sort by similarity descending and limit results
        similar_tasks.sort(key=lambda x: x['similarity'], reverse=True)
        return similar_tasks[:limit]
    
    except Exception as e:
        logger.error(f"Error finding similar tasks: {e}")
        return []


def analyze_intent(task: str) -> str:
    """
    Use the LLM to analyze and categorize the intent of a task.
    
    Args:
        task: The task description in natural language
    
    Returns:
        A string describing the analyzed intent
    """
    if not FOUNDRY_PROJECT_ENDPOINT:
        return "unknown"
    
    try:
        from openai import AzureOpenAI
        
        credential = DefaultAzureCredential()
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        
        base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
        
        client = AzureOpenAI(
            azure_endpoint=base_endpoint,
            api_key=token.token,
            api_version="2024-02-15-preview"
        )
        
        # Use tuned model if available (Agent Lightning)
        model_deployment = get_model_deployment()
        
        response = client.chat.completions.create(
            model=model_deployment,
            messages=[
                {
                    "role": "system",
                    "content": "You are a healthcare digital quality management task analyzer. Analyze the given task and provide a brief categorization of its intent. Return only a short phrase describing the primary intent (e.g., 'quality_gap_identification', 'action_scoring', 'member_outreach', 'care_alert_creation', 'intervention_logging', 'proactive_gap_closure', 'next_best_action_recommendation', 'risk_stratification', 'outreach_effectiveness_analysis', 'provider_realtime_recommendation')."
                },
                {"role": "user", "content": f"Analyze this task: {task}"}
            ]
        )
        
        if response.choices and len(response.choices) > 0:
            return response.choices[0].message.content.strip()
        return "unknown"
    
    except Exception as e:
        logger.error(f"Error analyzing intent: {e}")
        return "unknown"


def generate_plan(task: str, similar_tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Generate a plan of steps to accomplish the task, optionally learning from similar past tasks.
    
    Args:
        task: The task description
        similar_tasks: List of similar tasks for context
    
    Returns:
        List of planned steps
    """
    if not FOUNDRY_PROJECT_ENDPOINT:
        return [{"step": 1, "action": "Manual planning required", "description": "Foundry not configured"}]
    
    try:
        from openai import AzureOpenAI
        
        credential = DefaultAzureCredential()
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        
        base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
        
        client = AzureOpenAI(
            azure_endpoint=base_endpoint,
            api_key=token.token,
            api_version="2024-02-15-preview"
        )
        
        # Build context from similar tasks
        context = ""
        if similar_tasks:
            context = "\n\nSimilar past tasks for reference:\n"
            for st in similar_tasks[:3]:
                context += f"- {st['task']} (intent: {st['intent']}, similarity: {st['similarity']:.2f})\n"
        
        # Use tuned model if available (Agent Lightning)
        model_deployment = get_model_deployment()
        
        response = client.chat.completions.create(
            model=model_deployment,
            messages=[
                {
                    "role": "system",
                    "content": """You are a Healthcare Digital Quality Management Next Best Action agent that executes quality improvement actions and delivers concrete results.

IMPORTANT: You must deliver CONCRETE RESULTS, not action plans. Execute the requested task and return specific data, confirmed actions, or quantified outcomes.

For each task, return a JSON object with:
- "status": "completed" or "in_progress"
- "results": object containing the actual data, scores, confirmations, or findings
- "actions_taken": array of actions that were executed (not planned)
- "recommendations": specific next steps with quantified expected impact

Return ONLY valid JSON, no markdown or explanation."""
                },
                {"role": "user", "content": f"Create a plan for this task: {task}{context}"}
            ]
        )
        
        if response.choices and len(response.choices) > 0:
            content = response.choices[0].message.content.strip()
            # Parse JSON from response
            try:
                # Handle potential markdown code blocks
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                return json.loads(content)
            except json.JSONDecodeError:
                return [{"step": 1, "action": "Execute task", "description": content, "estimated_effort": "medium"}]
        
        return [{"step": 1, "action": "Execute task", "description": task, "estimated_effort": "medium"}]
    
    except Exception as e:
        logger.error(f"Error generating plan: {e}")
        return [{"step": 1, "action": "Error", "description": str(e), "estimated_effort": "unknown"}]


def generate_plan_with_instructions(
    task: str,
    similar_tasks: List[Dict[str, Any]],
    task_instructions: List[Dict[str, Any]],
    domain_facts: List[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Generate a plan of steps to accomplish the task using:
    1. Similar past tasks from CosmosDB (short-term memory)
    2. Task instructions from AI Search (long-term memory)
    3. Domain facts from Fabric IQ (ontology-grounded facts memory)
    
    Args:
        task: The task description
        similar_tasks: List of similar tasks from CosmosDB
        task_instructions: List of task instructions from AI Search
        domain_facts: List of relevant facts from Fabric IQ ontology
    
    Returns:
        List of planned steps
    """
    if not FOUNDRY_PROJECT_ENDPOINT:
        return [{"step": 1, "action": "Manual planning required", "description": "Foundry not configured"}]
    
    try:
        from openai import AzureOpenAI
        
        credential = DefaultAzureCredential()
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        
        base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
        
        client = AzureOpenAI(
            azure_endpoint=base_endpoint,
            api_key=token.token,
            api_version="2024-02-15-preview"
        )
        
        # Build context from similar tasks (short-term memory)
        context = ""
        if similar_tasks:
            context = "\n\n## Similar Past Tasks (Short-Term Memory):\n"
            for st in similar_tasks[:3]:
                context += f"- {st['task']} (intent: {st['intent']}, similarity: {st['similarity']:.2f})\n"
        
        # Build context from task instructions (long-term memory from AI Search)
        if task_instructions:
            context += "\n\n## Task Instructions (Long-Term Memory):\n"
            for ti in task_instructions[:2]:
                context += f"\n### {ti.get('title', 'Untitled')} (relevance: {ti.get('score', 0):.2f})\n"
                context += f"Category: {ti.get('category', 'N/A')}\n"
                context += f"Description: {ti.get('description', 'N/A')}\n"
                
                # Include reference steps if available
                ref_steps = ti.get('steps', [])
                if ref_steps:
                    context += "Reference Steps:\n"
                    for step in ref_steps[:5]:  # Limit to first 5 steps
                        context += f"  {step.get('step', '?')}. {step.get('action', 'N/A')}: {step.get('description', 'N/A')[:100]}...\n"
                
                # Include content excerpt
                content_excerpt = ti.get('content_excerpt', '')
                if content_excerpt:
                    context += f"\nKey Information:\n{content_excerpt[:500]}...\n"
        
        # Build context from domain facts (Fabric IQ ontology-grounded facts)
        if domain_facts:
            context += "\n\n## Domain Facts (Fabric IQ Ontology):\n"
            context += "The following facts are derived from the knowledge graph and provide grounded context:\n"
            for fact in domain_facts[:5]:
                context += f"\n### {fact.get('domain', 'unknown').upper()} Domain Fact\n"
                context += f"- Statement: {fact.get('statement', 'N/A')}\n"
                context += f"- Confidence: {fact.get('confidence', 0):.0%}\n"
                context += f"- Type: {fact.get('fact_type', 'N/A')}\n"
                # Include relevant context from the fact
                fact_context = fact.get('context', {})
                if fact_context:
                    context += "- Key Metrics:\n"
                    for key, value in list(fact_context.items())[:5]:
                        context += f"    - {key}: {value}\n"
        
        # Use tuned model if available (Agent Lightning)
        model_deployment = get_model_deployment()
        
        response = client.chat.completions.create(
            model=model_deployment,
            messages=[
                {
                    "role": "system",
                    "content": """You are a Healthcare Digital Quality Management Next Best Action agent that EXECUTES quality improvement tasks and delivers CONCRETE RESULTS with specific data.

You have access to:
1. Short-term memory (similar past tasks and their outcomes)
2. Long-term memory (detailed task instructions and best practices)
3. Facts memory (ontology-grounded domain facts from Fabric IQ with real metrics)

CRITICAL INSTRUCTION: Do NOT return action plans or step-by-step outlines. Instead, EXECUTE the task and return:
- Specific data values (member IDs, scores, dates, measure names)
- Confirmed action outcomes ("outreach queued", "alert created", "intervention logged")
- Quantified results (closure probabilities, risk scores, gap counts)
- Concrete recommendations with expected impact percentages

Use ALL provided context to deliver results grounded in the domain knowledge.
When domain facts are available, incorporate specific metrics into your results.
When task instructions are available, follow the proven approaches.

Return a JSON object with:
- "status": "completed"
- "results": object with specific findings and data
- "actions_taken": array of completed actions with confirmation details
- "recommendations": array of specific next steps with expected impact
- "source": "fact-grounded" if based on domain facts, "adapted" if based on instructions, "original" otherwise

Return ONLY valid JSON, no markdown or explanation."""
                },
                {"role": "user", "content": f"Create a detailed plan for this task: {task}{context}"}
            ]
        )
        
        if response.choices and len(response.choices) > 0:
            content = response.choices[0].message.content.strip()
            try:
                # Handle potential markdown code blocks
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                return json.loads(content)
            except json.JSONDecodeError:
                return [{"step": 1, "action": "Execute task", "description": content, "estimated_effort": "medium", "source": "original"}]
        
        return [{"step": 1, "action": "Execute task", "description": task, "estimated_effort": "medium", "source": "original"}]
    
    except Exception as e:
        logger.error(f"Error generating plan with instructions: {e}")
        # Fallback to basic plan generation
        return generate_plan(task, similar_tasks)


# =========================================
# Initialize AI Search Long-Term Memory
# (After helper functions are defined)
# =========================================

def _initialize_long_term_memory():
    """Initialize AI Search long-term memory with AzureAISearchContextProvider."""
    global long_term_memory
    
    if AZURE_SEARCH_ENDPOINT and FOUNDRY_PROJECT_ENDPOINT:
        try:
            long_term_memory = LongTermMemory(
                search_endpoint=AZURE_SEARCH_ENDPOINT,
                foundry_endpoint=FOUNDRY_PROJECT_ENDPOINT,
                index_name=AZURE_SEARCH_INDEX_NAME,
                knowledge_base_name=AZURE_SEARCH_KNOWLEDGE_BASE_NAME,
                mode="agentic",
            )
            # Set embedding function for the long-term memory
            long_term_memory.set_embedding_function(get_embedding)
            
            # Update composite memory with long-term if it exists
            if composite_memory:
                composite_memory._long_term = long_term_memory
            
            if AISEARCH_CONTEXT_PROVIDER_AVAILABLE:
                logger.info(f"LongTermMemory initialized with AzureAISearchContextProvider: {AZURE_SEARCH_INDEX_NAME}")
            else:
                logger.warning(f"LongTermMemory initialized WITHOUT AzureAISearchContextProvider (package not installed): {AZURE_SEARCH_INDEX_NAME}")
            
        except Exception as e:
            logger.error(f"Failed to initialize long-term memory: {e}")
    else:
        logger.warning("AZURE_SEARCH_ENDPOINT or FOUNDRY_PROJECT_ENDPOINT not configured - long-term memory will not work")


def _initialize_facts_memory():
    """
    Initialize Facts Memory with ontology-grounded facts.
    
    Uses Azure Blob Storage by default (when FABRIC_ENABLED=false).
    Uses Fabric IQ when FABRIC_ENABLED=true.
    
    Loads sample data for Customer, DevOps, and User Management domains.
    """
    global facts_memory
    
    try:
        # Initialize with Blob Storage (default) or Fabric IQ mode
        facts_memory = FactsMemory(
            storage_account_url=AZURE_STORAGE_ACCOUNT_URL,
            ontology_container=ONTOLOGY_CONTAINER_NAME,
            fabric_enabled=FABRIC_ENABLED,
            fabric_endpoint=FABRIC_ENDPOINT,
            workspace_id=FABRIC_WORKSPACE_ID,
            ontology_name=FABRIC_ONTOLOGY_NAME,
        )
        
        # Set embedding function if available
        if FOUNDRY_PROJECT_ENDPOINT:
            facts_memory.set_embedding_function(get_embedding)
        
        mode = "Fabric IQ" if FABRIC_ENABLED else "Azure Blob Storage"
        logger.info(f"Facts Memory initialized: ontology={FABRIC_ONTOLOGY_NAME}, mode={mode}")
        
        # Load ontologies from storage (if not using in-memory sample data)
        async def load_from_storage():
            if not FABRIC_ENABLED and AZURE_STORAGE_ACCOUNT_URL:
                # Try to load ontologies from Blob Storage
                loaded = await facts_memory.load_all_ontologies()
                if loaded > 0:
                    logger.info(f"Loaded {loaded} ontologies from Azure Blob Storage")
                    return
            # Fallback to sample data if no ontologies loaded from storage
            await _load_sample_ontology_data()
        
        asyncio.get_event_loop().run_until_complete(load_from_storage())
        
    except Exception as e:
        logger.error(f"Failed to initialize Facts Memory: {e}")


async def _load_sample_ontology_data():
    """Load sample ontology data for all three domains."""
    if not facts_memory:
        return
    
    logger.info("Loading sample ontology data for Fabric IQ...")
    
    # =========================================
    # 1. Customer Churn Analysis Domain
    # =========================================
    customers = CustomerDataGenerator.generate_customers(count=25)
    for customer in customers:
        entity = OntologyEntity(
            id=customer.customer_id,
            entity_type=EntityType.CUSTOMER,
            properties=customer.to_dict(),
        )
        await facts_memory.store_entity(entity)
        
        # Derive facts for high-risk customers
        if customer.churn_risk > 0.5:
            fact = Fact(
                id=f"fact-churn-{customer.customer_id}",
                fact_type="prediction",
                domain="customer",
                statement=f"Customer '{customer.name}' ({customer.segment.value} segment) has {customer.churn_risk:.0%} churn risk. "
                          f"Key indicators: {customer.days_since_last_login} days since last login, "
                          f"feature usage score of {customer.feature_usage_score:.0f}/100, "
                          f"NPS score of {customer.nps_score}.",
                confidence=customer.churn_risk,
                evidence=[customer.customer_id],
                context={
                    "segment": customer.segment.value,
                    "risk_level": customer.risk_level.value,
                    "tenure_months": customer.tenure_months,
                    "monthly_spend": customer.monthly_spend,
                },
            )
            await facts_memory.store_fact(fact)
    
    logger.info(f"Loaded {len(customers)} customer entities with churn analysis facts")
    
    # =========================================
    # 2. CI/CD Pipeline Domain
    # =========================================
    pipelines = PipelineDataGenerator.generate_pipelines(count=6)
    total_runs = 0
    
    for pipeline in pipelines:
        entity = OntologyEntity(
            id=pipeline.pipeline_id,
            entity_type=EntityType.PIPELINE,
            properties=pipeline.to_dict(),
        )
        await facts_memory.store_entity(entity)
        
        # Generate pipeline runs with successes and failures
        runs = PipelineDataGenerator.generate_pipeline_runs(pipeline, count=20)
        total_runs += len(runs)
        
        success_runs = [r for r in runs if r.status == PipelineStatus.SUCCESS]
        failed_runs = [r for r in runs if r.status == PipelineStatus.FAILURE]
        
        for run in runs:
            run_entity = OntologyEntity(
                id=run.run_id,
                entity_type=EntityType.PIPELINE_RUN,
                properties=run.to_dict(),
            )
            await facts_memory.store_entity(run_entity)
        
        # Create pipeline health facts
        success_rate = len(success_runs) / len(runs) if runs else 0
        fact = Fact(
            id=f"fact-pipeline-{pipeline.pipeline_id}",
            fact_type="observation",
            domain="devops",
            statement=f"Pipeline '{pipeline.name}' for {pipeline.service_name} has {success_rate:.0%} success rate "
                      f"over {len(runs)} recent runs. Target cluster: {pipeline.target_cluster}. "
                      f"{len(failed_runs)} failures detected.",
            confidence=0.95,
            evidence=[pipeline.pipeline_id] + [r.run_id for r in runs[:5]],
            context={
                "success_rate": success_rate,
                "total_runs": len(runs),
                "failures": len(failed_runs),
                "avg_duration": pipeline.avg_duration_seconds,
                "service": pipeline.service_name,
            },
        )
        await facts_memory.store_fact(fact)
        
        # Create facts for significant failures
        for run in failed_runs[:3]:
            failure_fact = Fact(
                id=f"fact-failure-{run.run_id}",
                fact_type="observation",
                domain="devops",
                statement=f"Pipeline run {run.run_id} failed at stage '{run.failure_stage}' with error: {run.failure_message}. "
                          f"Category: {run.failure_category}. Triggered by: {run.triggered_by}.",
                confidence=1.0,
                evidence=[run.run_id, pipeline.pipeline_id],
                context={
                    "failure_category": run.failure_category,
                    "failure_stage": run.failure_stage,
                    "commit_sha": run.commit_sha,
                    "duration_seconds": run.duration_seconds,
                },
            )
            await facts_memory.store_fact(failure_fact)
    
    logger.info(f"Loaded {len(pipelines)} pipelines with {total_runs} execution runs")
    
    # =========================================
    # 3. User Management Domain
    # =========================================
    users = UserAccessDataGenerator.generate_users(count=15)
    total_auth_events = 0
    
    for user in users:
        entity = OntologyEntity(
            id=user.user_id,
            entity_type=EntityType.USER,
            properties=user.to_dict(),
        )
        await facts_memory.store_entity(entity)
        
        # Generate auth events
        auth_events = UserAccessDataGenerator.generate_auth_events(user, count=30)
        total_auth_events += len(auth_events)
        
        for event in auth_events:
            event_entity = OntologyEntity(
                id=event.event_id,
                entity_type=EntityType.AUTH_EVENT,
                properties=event.to_dict(),
            )
            await facts_memory.store_entity(event_entity)
        
        # Create user activity facts
        login_successes = len([e for e in auth_events if e.event_type == AuthEventType.LOGIN_SUCCESS])
        login_failures = len([e for e in auth_events if e.event_type == AuthEventType.LOGIN_FAILURE])
        high_risk_events = len([e for e in auth_events if e.risk_score > 0.5])
        
        fact = Fact(
            id=f"fact-user-{user.user_id}",
            fact_type="observation",
            domain="user_management",
            statement=f"User '{user.username}' ({', '.join(user.roles)}) has {login_successes} successful logins "
                      f"and {login_failures} failed attempts. MFA enabled: {user.mfa_enabled}. "
                      f"Status: {user.status.value}. {high_risk_events} high-risk authentication events detected.",
            confidence=0.9,
            evidence=[user.user_id] + [e.event_id for e in auth_events[:5]],
            context={
                "roles": user.roles,
                "mfa_enabled": user.mfa_enabled,
                "status": user.status.value,
                "login_successes": login_successes,
                "login_failures": login_failures,
                "high_risk_events": high_risk_events,
            },
        )
        await facts_memory.store_fact(fact)
        
        # Flag suspicious users
        if login_failures > 5 or high_risk_events > 3:
            security_fact = Fact(
                id=f"fact-security-{user.user_id}",
                fact_type="derived",
                domain="user_management",
                statement=f"SECURITY ALERT: User '{user.username}' shows suspicious activity pattern. "
                          f"{login_failures} failed login attempts, {high_risk_events} high-risk events. "
                          f"Recommend review of account activity.",
                confidence=0.85,
                evidence=[user.user_id],
                context={
                    "alert_type": "suspicious_activity",
                    "failed_logins": login_failures,
                    "high_risk_events": high_risk_events,
                },
            )
            await facts_memory.store_fact(security_fact)
    
    logger.info(f"Loaded {len(users)} users with {total_auth_events} auth events")
    
    # Log final statistics
    stats = facts_memory.get_stats()
    logger.info(f"Facts Memory loaded: {stats['total_entities']} entities, {stats['total_facts']} facts")


# Initialize long-term memory now that helper functions are available
_initialize_long_term_memory()

# Initialize facts memory with sample ontology data
_initialize_facts_memory()


# Define Agent Framework tools using @ai_function decorator
@ai_function
def hello_mcp_tool() -> str:
    """Hello world MCP tool that returns a greeting message."""
    return "Hello I am MCPTool!"


@ai_function
def get_snippet_tool(snippetname: str) -> str:
    """
    Retrieve a snippet by name from Azure Blob Storage.
    
    Args:
        snippetname: The name of the snippet to retrieve
    
    Returns:
        The content of the snippet
    """
    if not blob_service_client:
        return "Error: Storage not configured"
    
    try:
        blob_client = blob_service_client.get_blob_client(
            container=SNIPPETS_CONTAINER,
            blob=f"{snippetname}.json"
        )
        blob_data = blob_client.download_blob().readall()
        return blob_data.decode('utf-8')
    except Exception as e:
        logger.error(f"Error retrieving snippet: {e}")
        return f"Error retrieving snippet: {str(e)}"


@ai_function
def save_snippet_tool(snippetname: str, snippet: str) -> str:
    """
    Save a snippet with a name to Azure Blob Storage.
    
    Args:
        snippetname: The name of the snippet
        snippet: The content of the snippet
    
    Returns:
        Success or error message
    """
    if not blob_service_client:
        return "Error: Storage not configured"
    
    try:
        blob_client = blob_service_client.get_blob_client(
            container=SNIPPETS_CONTAINER,
            blob=f"{snippetname}.json"
        )
        blob_client.upload_blob(snippet.encode('utf-8'), overwrite=True)
        return f"Snippet '{snippetname}' saved successfully"
    except Exception as e:
        logger.error(f"Error saving snippet: {e}")
        return f"Error saving snippet: {str(e)}"


@ai_function
def ask_foundry_tool(question: str) -> str:
    """
    Ask a question and get an answer using the Azure AI Foundry model.
    
    Args:
        question: The question to ask the AI model
    
    Returns:
        The AI model's response to the question
    """
    if not FOUNDRY_PROJECT_ENDPOINT:
        return "Error: Foundry endpoint not configured"
    
    try:
        from openai import AzureOpenAI
        
        credential = DefaultAzureCredential()
        # Get a token for Azure Cognitive Services
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        
        # Extract the base endpoint (remove /api/projects/proj-default if present)
        # Use the services.ai.azure.com endpoint directly
        base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
        
        client = AzureOpenAI(
            azure_endpoint=base_endpoint,
            api_key=token.token,
            api_version="2024-02-15-preview"
        )
        
        # Use tuned model if available (Agent Lightning)
        model_deployment = get_model_deployment()
        logger.info(f"ask_foundry using model: {model_deployment}")
        
        response = client.chat.completions.create(
            model=model_deployment,
            messages=[{"role": "user", "content": question}]
        )
        
        if response.choices and len(response.choices) > 0:
            return response.choices[0].message.content
        return "No response generated"
    except Exception as e:
        logger.error(f"Error calling Foundry model: {e}")
        return f"Error calling Foundry model: {str(e)}"


@ai_function
def next_best_action_tool(task: str) -> str:
    """
    Analyze a task using semantic reasoning with three memory layers:
    1. Short-term memory (CosmosDB) - finds similar past tasks using cosine similarity
    2. Long-term memory (Foundry IQ) - retrieves relevant task instructions
    3. Facts memory (Fabric IQ) - queries domain facts from ontologies
    
    The planning is grounded in domain knowledge from Fabric IQ ontologies:
    - Customer domain: churn predictions, segment analysis, risk assessments
    - DevOps domain: pipeline health, deployment status, failure patterns
    - User Management domain: authentication patterns, security alerts
    
    Args:
        task: The task description in natural language (English sentence)
    
    Returns:
        A JSON response containing task analysis, similar tasks, domain facts, and planned steps
    """
    if not FOUNDRY_PROJECT_ENDPOINT:
        return json.dumps({"error": "Foundry endpoint not configured"})
    
    if not cosmos_tasks_container or not cosmos_plans_container:
        return json.dumps({"error": "CosmosDB not configured"})
    
    try:
        import asyncio
        
        task_id = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat()
        
        # ================================================================
        # AGENT 365 CI/CD APPROVAL CHECKPOINT
        # For CI/CD pipeline tasks, require human-in-the-loop approval
        # via Microsoft Teams before proceeding with plan generation.
        # ================================================================
        approval_result = None
        agents_approval_required = False
        CICD_TASK_PATTERN = "Set up a Agents pipeline for deploying microservices to Kubernetes"
        
        if AGENT365_APPROVAL_AVAILABLE and CICD_TASK_PATTERN.lower() in task.lower():
            agents_approval_required = True
            logger.info("=" * 70)
            logger.info("🔒 Agents APPROVAL CHECKPOINT TRIGGERED")
            logger.info("=" * 70)
            logger.info(f"Task: {task}")
            logger.info("This task requires human-in-the-loop approval via Microsoft Teams")
            
            try:
                # Initialize approval workflow engine
                approval_engine = get_approval_workflow_engine()
                
                # Create approval request
                loop = asyncio.new_event_loop()
                approval_contract = loop.run_until_complete(
                    approval_engine.initiate_approval(
                        task=task,
                        requested_by=os.getenv("AZURE_CLIENT_ID", "mcp-agent"),
                        environment=os.getenv("DEPLOYMENT_ENVIRONMENT", "staging"),
                        cluster=os.getenv("AKS_CLUSTER_NAME", "aks-mcp-cluster"),
                        namespace=os.getenv("K8S_NAMESPACE", "mcp-agents"),
                        image_tags=[os.getenv("IMAGE_TAG", "latest")],
                        commit_sha=os.getenv("COMMIT_SHA", "unknown"),
                        pipeline_url=os.getenv("PIPELINE_URL", ""),
                        rollback_url=os.getenv("ROLLBACK_URL", ""),
                    )
                )
                loop.close()
                
                logger.info(f"📋 Approval ID: {approval_contract.approval_id}")
                logger.info(f"⏳ Waiting for approval in Microsoft Teams...")
                
                # NOTE: In production, this would block until approval is received
                # For demo/testing, we'll check if approval was pre-configured
                approval_result = {
                    "approval_id": approval_contract.approval_id,
                    "status": approval_contract.decision,
                    "agent_validation": approval_contract.agent_validation,
                    "message": "Approval request sent to Microsoft Teams",
                    "approval_contract": approval_contract.to_dict()
                }
                
                # If approval is still pending, return early with approval info
                if approval_contract.decision == ApprovalDecision.PENDING.value:
                    logger.warning("⚠️ Approval is pending - task execution blocked")
                    return json.dumps({
                        "task_id": task_id,
                        "task": task,
                        "status": "approval_pending",
                        "approval": approval_result,
                        "message": "Agents deployment requires human approval via Microsoft Teams. "
                                   "Please approve the request in Teams to proceed.",
                        "approval_contract": {
                            "approval_id": approval_contract.approval_id,
                            "requested_by": approval_contract.requested_by,
                            "task": task,
                            "environment": approval_contract.environment,
                            "decision": approval_contract.decision,
                            "approved_by": approval_contract.approved_by,
                            "timestamp": approval_contract.timestamp,
                            "agent_validation": approval_contract.agent_validation
                        }
                    }, indent=2)
                
                # Check if approval was rejected
                if approval_contract.decision == ApprovalDecision.REJECTED.value:
                    logger.error(f"❌ Approval rejected by {approval_contract.approved_by}")
                    return json.dumps({
                        "task_id": task_id,
                        "task": task,
                        "status": "approval_rejected",
                        "approval": approval_result,
                        "error": f"Agents deployment rejected: {approval_contract.comment or 'No reason provided'}",
                        "approval_contract": approval_contract.to_dict()
                    }, indent=2)
                
                logger.info(f"✅ Approval granted by {approval_contract.approved_by}")
                logger.info(f"✅ Agent validation: {approval_contract.agent_validation}")
                
            except Exception as e:
                logger.error(f"Approval workflow error: {e}")
                approval_result = {
                    "error": str(e),
                    "status": "approval_error",
                    "message": "Approval workflow encountered an error. Proceeding with caution."
                }
        
        # Step 1: Generate embedding for the task
        logger.info(f"Generating embedding for task: {task[:100]}...")
        task_embedding = get_embedding(task)
        
        # Step 2: Analyze intent
        logger.info("Analyzing task intent...")
        intent = analyze_intent(task)
        
        # Step 3: Find similar tasks using cosine similarity (short-term memory from CosmosDB)
        logger.info("Searching for similar past tasks in CosmosDB...")
        similar_tasks = find_similar_tasks(task_embedding, threshold=0.7, limit=5)
        
        # Step 4: Search for task instructions in AI Search long-term memory
        # Uses AzureAISearchContextProvider for enhanced agentic retrieval
        task_instructions = []
        long_term_context = ""
        
        if long_term_memory:
            # First, get context via AzureAISearchContextProvider
            logger.info("Retrieving context via LongTermMemory with AzureAISearchContextProvider...")
            try:
                loop = asyncio.new_event_loop()
                long_term_context = loop.run_until_complete(
                    long_term_memory.get_context(task)
                )
                loop.close()
                if long_term_context:
                    logger.info(f"AzureAISearchContextProvider returned context: {len(long_term_context)} chars")
                else:
                    logger.info("AzureAISearchContextProvider returned no context")
            except Exception as e:
                logger.warning(f"Failed to retrieve context from AzureAISearchContextProvider: {e}")
            
            # Also get structured task instructions via hybrid search
            logger.info("Searching for task instructions in LongTermMemory...")
            try:
                loop = asyncio.new_event_loop()
                task_instructions = loop.run_until_complete(
                    long_term_memory.search_task_instructions(
                        task_description=task,
                        limit=3,
                        include_steps=True
                    )
                )
                loop.close()
                logger.info(f"Found {len(task_instructions)} relevant task instructions from LongTermMemory")
            except Exception as e:
                logger.warning(f"Failed to retrieve task instructions from LongTermMemory: {e}")
        else:
            logger.info("Long-term memory not configured - skipping task instructions lookup")
        
        # Step 5: Search for domain facts in Fabric IQ facts memory
        domain_facts = []
        if facts_memory:
            logger.info("Searching for domain facts in Fabric IQ...")
            try:
                loop = asyncio.new_event_loop()
                # Search for facts relevant to the task across all domains
                fact_results = loop.run_until_complete(
                    facts_memory.search_facts(
                        query=task,
                        domain=None,  # Search all domains
                        limit=5,
                    )
                )
                loop.close()
                
                # Convert fact results to dictionary format for planning
                for result in fact_results:
                    domain_facts.append({
                        'id': result.fact.id,
                        'statement': result.fact.statement,
                        'domain': result.fact.domain,
                        'fact_type': result.fact.fact_type,
                        'confidence': result.fact.confidence,
                        'relevance_score': result.score,
                        'context': result.fact.context,
                        'evidence': result.fact.evidence,
                    })
                logger.info(f"Found {len(domain_facts)} relevant domain facts from Fabric IQ")
            except Exception as e:
                logger.warning(f"Failed to retrieve facts from Fabric IQ: {e}")
        else:
            logger.info("Fabric IQ facts memory not configured - skipping domain facts lookup")
        
        # Step 6: Generate plan based on task, similar past tasks, task instructions, AND domain facts
        logger.info("Generating execution plan with all memory contexts...")
        plan_steps = generate_plan_with_instructions(task, similar_tasks, task_instructions, domain_facts)
        
        # Step 7: Store task in CosmosDB
        task_doc = {
            'id': task_id,
            'task': task,
            'intent': intent,
            'embedding': task_embedding,
            'created_at': timestamp,
            'similar_task_count': len(similar_tasks),
            'task_instructions_found': len(task_instructions),
            'domain_facts_found': len(domain_facts)
        }
        cosmos_tasks_container.upsert_item(task_doc)
        logger.info(f"Task stored in CosmosDB with id: {task_id}")
        
        # Step 8: Store plan in CosmosDB
        plan_doc = {
            'id': str(uuid.uuid4()),
            'taskId': task_id,
            'task': task,
            'intent': intent,
            'steps': plan_steps,
            'similar_tasks_referenced': [{'id': st['id'], 'similarity': st['similarity']} for st in similar_tasks],
            'task_instructions_used': [ti.get('document_id', '') for ti in task_instructions],
            'domain_facts_used': [df.get('id', '') for df in domain_facts],
            'created_at': timestamp,
            'status': 'planned'
        }
        cosmos_plans_container.upsert_item(plan_doc)
        logger.info(f"Plan stored in CosmosDB for task: {task_id}")
        
        # Build response
        response = {
            'task_id': task_id,
            'task': task,
            'intent': intent,
            'analysis': {
                'similar_tasks_found': len(similar_tasks),
                'similar_tasks': [
                    {
                        'task': st['task'],
                        'intent': st['intent'],
                        'similarity_score': round(st['similarity'], 3)
                    }
                    for st in similar_tasks
                ],
                'task_instructions_found': len(task_instructions),
                'task_instructions': [
                    {
                        'title': ti.get('title', ''),
                        'category': ti.get('category', ''),
                        'intent': ti.get('intent', ''),
                        'description': ti.get('description', ''),
                        'relevance_score': round(ti.get('score', 0), 3),
                        'estimated_effort': ti.get('estimated_effort', ''),
                        'reference_steps_count': len(ti.get('steps', []))
                    }
                    for ti in task_instructions
                ],
                'domain_facts_found': len(domain_facts),
                'domain_facts': [
                    {
                        'id': df.get('id', ''),
                        'domain': df.get('domain', ''),
                        'fact_type': df.get('fact_type', ''),
                        'statement': df.get('statement', '')[:200] + '...' if len(df.get('statement', '')) > 200 else df.get('statement', ''),
                        'confidence': round(df.get('confidence', 0), 3),
                        'relevance_score': round(df.get('relevance_score', 0), 3),
                    }
                    for df in domain_facts
                ]
            },
            'plan': {
                'steps': plan_steps,
                'total_steps': len(plan_steps)
            },
            'metadata': {
                'created_at': timestamp,
                'embedding_dimensions': len(task_embedding),
                'stored_in_cosmos': True,
                'long_term_memory_used': len(task_instructions) > 0,
                'facts_memory_used': len(domain_facts) > 0,
                'agents_approval_required': agents_approval_required,
                'approval_result': approval_result
            }
        }
        
        return json.dumps(response, indent=2)
    
    except Exception as e:
        logger.error(f"Error in next_best_action: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def store_memory_tool(content: str, session_id: str, memory_type: str = "context") -> str:
    """
    Store information in short-term memory for later retrieval.
    
    Args:
        content: The content to remember
        session_id: The session ID to associate the memory with
        memory_type: Type of memory (context, conversation, task, plan)
    
    Returns:
        JSON response with the stored memory ID
    """
    if not short_term_memory:
        return json.dumps({"error": "Memory provider not configured"})
    
    try:
        import asyncio
        
        # Map string to MemoryType enum
        type_map = {
            "context": MemoryType.CONTEXT,
            "conversation": MemoryType.CONVERSATION,
            "task": MemoryType.TASK,
            "plan": MemoryType.PLAN,
        }
        mem_type = type_map.get(memory_type.lower(), MemoryType.CONTEXT)
        
        # Generate embedding for the content
        embedding = None
        if FOUNDRY_PROJECT_ENDPOINT:
            try:
                embedding = get_embedding(content)
            except Exception as e:
                logger.warning(f"Failed to generate embedding: {e}")
        
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            content=content,
            memory_type=mem_type,
            embedding=embedding,
            session_id=session_id,
        )
        
        # Run async store in sync context
        loop = asyncio.new_event_loop()
        entry_id = loop.run_until_complete(short_term_memory.store(entry))
        loop.close()
        
        return json.dumps({
            "success": True,
            "memory_id": entry_id,
            "session_id": session_id,
            "memory_type": memory_type,
            "has_embedding": embedding is not None,
        })
    
    except Exception as e:
        logger.error(f"Error storing memory: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def recall_memory_tool(query: str, session_id: str, limit: int = 5) -> str:
    """
    Recall relevant memories from short-term memory based on semantic similarity.
    
    Args:
        query: The query to search for relevant memories
        session_id: The session ID to search within
        limit: Maximum number of memories to return
    
    Returns:
        JSON response with relevant memories
    """
    if not short_term_memory:
        return json.dumps({"error": "Memory provider not configured"})
    
    if not FOUNDRY_PROJECT_ENDPOINT:
        return json.dumps({"error": "Foundry endpoint not configured for embeddings"})
    
    try:
        import asyncio
        
        # Generate embedding for the query
        query_embedding = get_embedding(query)
        
        # Search for similar memories
        loop = asyncio.new_event_loop()
        results = loop.run_until_complete(short_term_memory.search(
            query_embedding=query_embedding,
            limit=limit,
            threshold=0.6,
            session_id=session_id,
        ))
        loop.close()
        
        memories = [
            {
                "id": r.entry.id,
                "content": r.entry.content,
                "memory_type": r.entry.memory_type.value,
                "similarity_score": round(r.score, 3),
                "created_at": r.entry.created_at,
            }
            for r in results
        ]
        
        return json.dumps({
            "query": query,
            "session_id": session_id,
            "memories_found": len(memories),
            "memories": memories,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error recalling memory: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def get_session_history_tool(session_id: str, limit: int = 20) -> str:
    """
    Get conversation history for a session.
    
    Args:
        session_id: The session ID to get history for
        limit: Maximum number of messages to return
    
    Returns:
        JSON response with conversation history
    """
    if not short_term_memory:
        return json.dumps({"error": "Memory provider not configured"})
    
    try:
        import asyncio
        
        loop = asyncio.new_event_loop()
        history = loop.run_until_complete(
            short_term_memory.get_conversation_history(session_id, limit)
        )
        loop.close()
        
        return json.dumps({
            "session_id": session_id,
            "message_count": len(history),
            "messages": history,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error getting session history: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def clear_session_memory_tool(session_id: str) -> str:
    """
    Clear all short-term memory for a session.
    
    Args:
        session_id: The session ID to clear
    
    Returns:
        JSON response with number of entries cleared
    """
    if not short_term_memory:
        return json.dumps({"error": "Memory provider not configured"})
    
    try:
        import asyncio
        
        loop = asyncio.new_event_loop()
        count = loop.run_until_complete(short_term_memory.clear_session(session_id))
        loop.close()
        
        return json.dumps({
            "success": True,
            "session_id": session_id,
            "entries_cleared": count,
        })
    
    except Exception as e:
        logger.error(f"Error clearing session memory: {e}")
        return json.dumps({"error": str(e)})


# =========================================
# Fabric IQ Facts Memory Tools
# =========================================

@ai_function
def search_facts_tool(query: str, domain: str = None, limit: int = 5) -> str:
    """
    Search for relevant facts from Fabric IQ ontology-grounded knowledge.
    Uses semantic search to find facts across Customer, DevOps, and User Management domains.
    
    Args:
        query: Natural language query to search for relevant facts
        domain: Optional domain filter (customer, devops, user_management)
        limit: Maximum number of facts to return (default: 5)
    
    Returns:
        JSON response with matching facts and their relevance scores
    """
    if not facts_memory:
        return json.dumps({"error": "Facts memory not configured"})
    
    try:
        import asyncio
        
        loop = asyncio.new_event_loop()
        results = loop.run_until_complete(
            facts_memory.search_facts(
                query=query,
                domain=domain,
                limit=limit,
            )
        )
        loop.close()
        
        facts_data = [
            {
                "id": r.fact.id,
                "statement": r.fact.statement,
                "domain": r.fact.domain,
                "fact_type": r.fact.fact_type,
                "confidence": round(r.fact.confidence, 3),
                "relevance_score": round(r.score, 3),
                "evidence_count": len(r.fact.evidence),
                "context": r.fact.context,
            }
            for r in results
        ]
        
        return json.dumps({
            "query": query,
            "domain_filter": domain,
            "facts_found": len(facts_data),
            "facts": facts_data,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error searching facts: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def get_customer_churn_facts_tool(risk_level: str = None) -> str:
    """
    Retrieve customer churn analysis facts from Fabric IQ.
    Returns predictions and observations about customer churn risk.
    
    Args:
        risk_level: Optional filter by risk level (critical, high, medium, low, minimal)
    
    Returns:
        JSON response with customer churn facts and statistics
    """
    if not facts_memory:
        return json.dumps({"error": "Facts memory not configured"})
    
    try:
        import asyncio
        
        loop = asyncio.new_event_loop()
        
        # Search for churn-related facts
        results = loop.run_until_complete(
            facts_memory.search_facts(
                query="customer churn risk prediction",
                domain="customer",
                fact_type="prediction",
                limit=20,
            )
        )
        loop.close()
        
        # Filter by risk level if specified
        facts_data = []
        for r in results:
            if risk_level:
                fact_risk = r.fact.context.get("risk_level", "")
                if fact_risk.lower() != risk_level.lower():
                    continue
            
            facts_data.append({
                "customer_id": r.fact.evidence[0] if r.fact.evidence else None,
                "statement": r.fact.statement,
                "churn_risk": r.fact.confidence,
                "risk_level": r.fact.context.get("risk_level"),
                "segment": r.fact.context.get("segment"),
                "tenure_months": r.fact.context.get("tenure_months"),
                "monthly_spend": r.fact.context.get("monthly_spend"),
            })
        
        # Calculate summary statistics
        if facts_data:
            avg_risk = sum(f["churn_risk"] for f in facts_data) / len(facts_data)
            risk_distribution = {}
            for f in facts_data:
                level = f["risk_level"]
                risk_distribution[level] = risk_distribution.get(level, 0) + 1
        else:
            avg_risk = 0
            risk_distribution = {}
        
        return json.dumps({
            "total_at_risk_customers": len(facts_data),
            "average_churn_risk": round(avg_risk, 3),
            "risk_distribution": risk_distribution,
            "filter_applied": risk_level,
            "customers": facts_data[:10],  # Limit response size
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error getting customer churn facts: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def get_pipeline_health_facts_tool(include_failures: bool = True) -> str:
    """
    Retrieve Agents pipeline health facts from Fabric IQ.
    Returns observations about pipeline success rates and failures.
    
    Args:
        include_failures: Whether to include detailed failure information (default: True)
    
    Returns:
        JSON response with pipeline health facts and failure details
    """
    if not facts_memory:
        return json.dumps({"error": "Facts memory not configured"})
    
    try:
        import asyncio
        
        loop = asyncio.new_event_loop()
        
        # Get pipeline health observations
        health_results = loop.run_until_complete(
            facts_memory.search_facts(
                query="pipeline success rate deployment",
                domain="devops",
                fact_type="observation",
                limit=10,
            )
        )
        
        pipeline_facts = []
        for r in health_results:
            if "pipeline" in r.fact.id.lower():
                pipeline_facts.append({
                    "pipeline_id": r.fact.evidence[0] if r.fact.evidence else None,
                    "statement": r.fact.statement,
                    "success_rate": r.fact.context.get("success_rate"),
                    "total_runs": r.fact.context.get("total_runs"),
                    "failures": r.fact.context.get("failures"),
                    "service": r.fact.context.get("service"),
                    "avg_duration_seconds": r.fact.context.get("avg_duration"),
                })
        
        # Get failure details if requested
        failure_facts = []
        if include_failures:
            failure_results = loop.run_until_complete(
                facts_memory.search_facts(
                    query="pipeline failure error",
                    domain="devops",
                    fact_type="observation",
                    limit=10,
                )
            )
            
            for r in failure_results:
                if "failure" in r.fact.id.lower():
                    failure_facts.append({
                        "run_id": r.fact.evidence[0] if r.fact.evidence else None,
                        "statement": r.fact.statement,
                        "failure_category": r.fact.context.get("failure_category"),
                        "failure_stage": r.fact.context.get("failure_stage"),
                        "commit_sha": r.fact.context.get("commit_sha"),
                    })
        
        loop.close()
        
        # Calculate summary
        if pipeline_facts:
            avg_success_rate = sum(
                p["success_rate"] or 0 for p in pipeline_facts
            ) / len(pipeline_facts)
            total_failures = sum(p["failures"] or 0 for p in pipeline_facts)
        else:
            avg_success_rate = 0
            total_failures = 0
        
        return json.dumps({
            "total_pipelines": len(pipeline_facts),
            "average_success_rate": round(avg_success_rate, 3),
            "total_recent_failures": total_failures,
            "pipelines": pipeline_facts,
            "failure_details": failure_facts if include_failures else [],
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error getting pipeline health facts: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def get_user_security_facts_tool(include_alerts: bool = True) -> str:
    """
    Retrieve user security and access facts from Fabric IQ.
    Returns observations about user activity and security alerts.
    
    Args:
        include_alerts: Whether to include security alert facts (default: True)
    
    Returns:
        JSON response with user activity facts and security alerts
    """
    if not facts_memory:
        return json.dumps({"error": "Facts memory not configured"})
    
    try:
        import asyncio
        
        loop = asyncio.new_event_loop()
        
        # Get user activity observations
        activity_results = loop.run_until_complete(
            facts_memory.search_facts(
                query="user login authentication activity",
                domain="user_management",
                fact_type="observation",
                limit=15,
            )
        )
        
        user_facts = []
        for r in activity_results:
            if "user-" in r.fact.id:
                user_facts.append({
                    "user_id": r.fact.evidence[0] if r.fact.evidence else None,
                    "statement": r.fact.statement,
                    "roles": r.fact.context.get("roles"),
                    "mfa_enabled": r.fact.context.get("mfa_enabled"),
                    "status": r.fact.context.get("status"),
                    "login_successes": r.fact.context.get("login_successes"),
                    "login_failures": r.fact.context.get("login_failures"),
                    "high_risk_events": r.fact.context.get("high_risk_events"),
                })
        
        # Get security alerts if requested
        alert_facts = []
        if include_alerts:
            alert_results = loop.run_until_complete(
                facts_memory.search_facts(
                    query="security alert suspicious activity",
                    domain="user_management",
                    fact_type="derived",
                    limit=10,
                )
            )
            
            for r in alert_results:
                if "security" in r.fact.id.lower():
                    alert_facts.append({
                        "user_id": r.fact.evidence[0] if r.fact.evidence else None,
                        "statement": r.fact.statement,
                        "alert_type": r.fact.context.get("alert_type"),
                        "failed_logins": r.fact.context.get("failed_logins"),
                        "high_risk_events": r.fact.context.get("high_risk_events"),
                        "confidence": r.fact.confidence,
                    })
        
        loop.close()
        
        # Calculate summary
        total_users = len(user_facts)
        mfa_enabled_count = sum(1 for u in user_facts if u.get("mfa_enabled"))
        high_risk_users = len(alert_facts)
        
        return json.dumps({
            "total_users_analyzed": total_users,
            "mfa_adoption_rate": round(mfa_enabled_count / total_users, 3) if total_users else 0,
            "security_alerts_count": high_risk_users,
            "users": user_facts[:10],  # Limit response size
            "security_alerts": alert_facts if include_alerts else [],
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error getting user security facts: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def cross_domain_analysis_tool(query: str, source_domain: str, target_domain: str) -> str:
    """
    Perform cross-domain reasoning to find connections between different domains.
    Leverages Fabric IQ's graph capabilities for entity-relationship traversal.
    
    Args:
        query: Natural language query describing the connection to find
        source_domain: Starting domain (customer, devops, user_management)
        target_domain: Target domain to connect to
    
    Returns:
        JSON response with cross-domain connections and insights
    """
    if not facts_memory:
        return json.dumps({"error": "Facts memory not configured"})
    
    try:
        import asyncio
        
        loop = asyncio.new_event_loop()
        connections = loop.run_until_complete(
            facts_memory.cross_domain_query(
                query=query,
                source_domain=source_domain,
                target_domain=target_domain,
            )
        )
        loop.close()
        
        return json.dumps({
            "query": query,
            "source_domain": source_domain,
            "target_domain": target_domain,
            "connections_found": len(connections),
            "connections": connections[:5],  # Limit to top 5 connections
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error in cross-domain analysis: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def get_facts_memory_stats_tool() -> str:
    """
    Get statistics about the Fabric IQ Facts Memory.
    Returns counts of entities and facts by domain.
    
    Returns:
        JSON response with facts memory statistics
    """
    if not facts_memory:
        return json.dumps({"error": "Facts memory not configured"})
    
    try:
        stats = facts_memory.get_stats()
        
        return json.dumps({
            "total_entities": stats["total_entities"],
            "total_relationships": stats["total_relationships"],
            "total_facts": stats["total_facts"],
            "entities_by_type": stats["entities_by_type"],
            "entities_by_domain": stats["entities_by_domain"],
            "facts_by_domain": stats["facts_by_domain"],
            "ontology_name": FABRIC_ONTOLOGY_NAME,
            "fabric_endpoint_configured": bool(FABRIC_ENDPOINT),
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error getting facts memory stats: {e}")
        return json.dumps({"error": str(e)})


# =========================================
# Agent Lightning Tools (RLHF Fine-Tuning)
# =========================================

@ai_function
def lightning_list_episodes_tool(
    agent_id: str = None,
    limit: int = 20,
    start_date: str = None,
    end_date: str = None,
) -> str:
    """
    List captured episodes from Agent Lightning.
    Episodes represent agent interactions (user input → tool calls → response).
    
    Args:
        agent_id: Filter by agent ID (default: mcp-agents)
        limit: Maximum number of episodes to return (default: 20)
        start_date: Filter episodes after this date (ISO format)
        end_date: Filter episodes before this date (ISO format)
    
    Returns:
        JSON response with list of episodes
    """
    if not LIGHTNING_AVAILABLE or not rl_ledger:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        episodes = rl_ledger.query_episodes(
            agent_id=agent,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
        
        episodes_data = []
        for ep in episodes:
            episodes_data.append({
                "id": ep.id,
                "agent_id": ep.agent_id,
                "user_input": ep.user_input[:200] + "..." if len(ep.user_input) > 200 else ep.user_input,
                "assistant_output": ep.assistant_output[:200] + "..." if len(ep.assistant_output) > 200 else ep.assistant_output,
                "tool_calls_count": len(ep.tool_calls),
                "model_deployment": ep.model_deployment,
                "request_latency_ms": ep.request_latency_ms,
                "created_at": ep.created_at,
            })
        
        return json.dumps({
            "agent_id": agent,
            "episodes_found": len(episodes_data),
            "episodes": episodes_data,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error listing episodes: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_get_episode_tool(episode_id: str, agent_id: str = None) -> str:
    """
    Get detailed information about a specific episode.
    
    Args:
        episode_id: The ID of the episode to retrieve
        agent_id: Agent ID (default: mcp-agents)
    
    Returns:
        JSON response with full episode details including tool calls
    """
    if not LIGHTNING_AVAILABLE or not rl_ledger:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        episode = rl_ledger.get_episode(episode_id, agent)
        
        if not episode:
            return json.dumps({"error": f"Episode {episode_id} not found"})
        
        tool_calls_data = []
        for tc in episode.tool_calls:
            tool_calls_data.append({
                "tool_name": tc.tool_name,
                "arguments": tc.arguments,
                "result": tc.result[:500] + "..." if tc.result and len(tc.result) > 500 else tc.result,
                "duration_ms": tc.duration_ms,
                "error": tc.error,
            })
        
        return json.dumps({
            "id": episode.id,
            "agent_id": episode.agent_id,
            "user_input": episode.user_input,
            "assistant_output": episode.assistant_output,
            "tool_calls": tool_calls_data,
            "model_deployment": episode.model_deployment,
            "correlation_id": episode.correlation_id,
            "session_id": episode.session_id,
            "request_latency_ms": episode.request_latency_ms,
            "token_usage": episode.token_usage,
            "metadata": episode.metadata,
            "created_at": episode.created_at,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error getting episode: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_assign_reward_tool(
    episode_id: str,
    reward_value: float,
    reward_source: str = "human_approval",
    agent_id: str = None,
    rubric: str = None,
    evaluator: str = None,
    comments: str = None,
) -> str:
    """
    Assign a reward/label to an episode for RLHF training.
    
    Args:
        episode_id: The ID of the episode to reward
        reward_value: Reward value from -1.0 (bad) to 1.0 (good)
        reward_source: Source of reward (human_approval, eval_score, test_result, safety_check)
        agent_id: Agent ID (default: mcp-agents)
        rubric: Evaluation rubric/criteria used
        evaluator: Who/what evaluated
        comments: Additional comments
    
    Returns:
        JSON response with stored reward details
    """
    if not LIGHTNING_AVAILABLE or not reward_writer:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        
        # Map string to RewardSource enum
        source_map = {
            "human_approval": RewardSource.HUMAN_APPROVAL,
            "eval_score": RewardSource.EVAL_SCORE,
            "test_result": RewardSource.TEST_RESULT,
            "safety_check": RewardSource.SAFETY_CHECK,
            "cost_penalty": RewardSource.COST_PENALTY,
            "latency_penalty": RewardSource.LATENCY_PENALTY,
            "golden_conversation": RewardSource.GOLDEN_CONVERSATION,
        }
        source = source_map.get(reward_source.lower(), RewardSource.EVAL_SCORE)
        
        reward = reward_writer.record_reward(
            episode_id=episode_id,
            agent_id=agent,
            source=source,
            value=reward_value,
            rubric=rubric,
            evaluator=evaluator,
            metadata={"comments": comments} if comments else {},
        )
        
        if reward:
            return json.dumps({
                "success": True,
                "reward_id": reward.id,
                "episode_id": episode_id,
                "value": reward.value,
                "source": source.value,
                "rubric": rubric,
                "evaluator": evaluator,
                "created_at": reward.created_at,
            }, indent=2)
        else:
            return json.dumps({"error": "Failed to store reward"})
    
    except Exception as e:
        logger.error(f"Error assigning reward: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_list_rewards_tool(
    episode_id: str = None,
    agent_id: str = None,
    limit: int = 50,
) -> str:
    """
    List rewards assigned to episodes.
    
    Args:
        episode_id: Filter by episode ID (optional)
        agent_id: Filter by agent ID (default: mcp-agents)
        limit: Maximum number of rewards to return
    
    Returns:
        JSON response with list of rewards
    """
    if not LIGHTNING_AVAILABLE or not rl_ledger:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        rewards = rl_ledger.query_rewards(
            agent_id=agent,
            episode_id=episode_id,
            limit=limit,
        )
        
        rewards_data = []
        for r in rewards:
            rewards_data.append({
                "id": r.id,
                "episode_id": r.episode_id,
                "source": r.source.value,
                "value": r.value,
                "raw_value": r.raw_value,
                "rubric": r.rubric,
                "evaluator": r.evaluator,
                "created_at": r.created_at,
            })
        
        return json.dumps({
            "agent_id": agent,
            "episode_filter": episode_id,
            "rewards_found": len(rewards_data),
            "rewards": rewards_data,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error listing rewards: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_build_dataset_tool(
    name: str,
    agent_id: str = None,
    description: str = None,
    min_reward: float = 0.5,
) -> str:
    """
    Build a fine-tuning dataset from rewarded episodes.
    
    Creates JSONL files for Azure OpenAI fine-tuning from episodes
    that have positive rewards.
    
    Args:
        name: Name for the dataset
        agent_id: Agent ID (default: mcp-agents)
        description: Optional description
        min_reward: Minimum average reward for episode inclusion (default: 0.5)
    
    Returns:
        JSON response with dataset manifest
    """
    if not LIGHTNING_AVAILABLE or not dataset_builder:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        
        dataset = dataset_builder.build_dataset(
            agent_id=agent,
            name=name,
            description=description,
            min_reward=min_reward,
        )
        
        if dataset:
            return json.dumps({
                "success": True,
                "dataset_id": dataset.id,
                "name": dataset.name,
                "description": dataset.description,
                "agent_id": dataset.agent_id,
                "training_count": dataset.training_count,
                "validation_count": dataset.validation_count,
                "episode_count": len(dataset.episode_ids),
                "reward_threshold": dataset.reward_threshold,
                "local_path": dataset.local_path,
                "validation_path": dataset.metadata.get("validation_path"),
                "created_at": dataset.created_at,
            }, indent=2)
        else:
            return json.dumps({"error": "No qualifying episodes found for dataset"})
    
    except Exception as e:
        logger.error(f"Error building dataset: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_list_datasets_tool(agent_id: str = None, limit: int = 20) -> str:
    """
    List available fine-tuning datasets.
    
    Args:
        agent_id: Filter by agent ID (default: mcp-agents)
        limit: Maximum number of datasets to return
    
    Returns:
        JSON response with list of datasets
    """
    if not LIGHTNING_AVAILABLE or not rl_ledger:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        datasets = rl_ledger.list_datasets(agent_id=agent, limit=limit)
        
        datasets_data = []
        for ds in datasets:
            datasets_data.append({
                "id": ds.id,
                "name": ds.name,
                "description": ds.description,
                "training_count": ds.training_count,
                "validation_count": ds.validation_count,
                "episode_count": len(ds.episode_ids),
                "reward_threshold": ds.reward_threshold,
                "created_at": ds.created_at,
            })
        
        return json.dumps({
            "agent_id": agent,
            "datasets_found": len(datasets_data),
            "datasets": datasets_data,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error listing datasets: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_start_training_tool(
    dataset_id: str,
    agent_id: str = None,
    base_model: str = None,
    n_epochs: int = None,
) -> str:
    """
    Start a fine-tuning training run using Azure OpenAI.
    
    Args:
        dataset_id: ID of the dataset to use for training
        agent_id: Agent ID (default: mcp-agents)
        base_model: Base model to fine-tune (default: gpt-4o-mini-2024-07-18)
        n_epochs: Number of training epochs (default: 3)
    
    Returns:
        JSON response with training run details
    """
    if not LIGHTNING_AVAILABLE or not training_runner:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        
        hyperparams = {}
        if n_epochs:
            hyperparams["n_epochs"] = n_epochs
        
        run = training_runner.start_training(
            dataset_id=dataset_id,
            agent_id=agent,
            base_model=base_model,
            hyperparameters=hyperparams if hyperparams else None,
        )
        
        if run:
            return json.dumps({
                "success": True,
                "training_run_id": run.id,
                "dataset_id": run.dataset_id,
                "base_model": run.base_model,
                "status": run.status.value,
                "hyperparameters": run.hyperparameters,
                "aoai_job_id": run.aoai_job_id,
                "created_at": run.created_at,
            }, indent=2)
        else:
            return json.dumps({"error": "Failed to start training"})
    
    except Exception as e:
        logger.error(f"Error starting training: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_get_training_status_tool(training_run_id: str, agent_id: str = None) -> str:
    """
    Get the status of a training run.
    Polls the Azure OpenAI API to sync status, then returns from Cosmos.
    
    Args:
        training_run_id: ID of the training run
        agent_id: Agent ID (default: mcp-agents)
    
    Returns:
        JSON response with training run status and metrics
    """
    if not LIGHTNING_AVAILABLE or not rl_ledger:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        
        # Use training_runner.check_status() to poll AOAI API and sync Cosmos
        if training_runner:
            run = training_runner.check_status(training_run_id, agent)
        else:
            run = rl_ledger.get_training_run(training_run_id, agent)
        
        if not run:
            return json.dumps({"error": f"Training run {training_run_id} not found"})
        
        return json.dumps({
            "id": run.id,
            "agent_id": run.agent_id,
            "dataset_id": run.dataset_id,
            "base_model": run.base_model,
            "tuned_model_name": run.tuned_model_name,
            "status": run.status.value,
            "hyperparameters": run.hyperparameters,
            "metrics": run.metrics,
            "aoai_job_id": run.aoai_job_id,
            "error_message": run.error_message,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "created_at": run.created_at,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error getting training status: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_list_training_runs_tool(agent_id: str = None, limit: int = 20) -> str:
    """
    List training runs.
    
    Args:
        agent_id: Filter by agent ID (default: mcp-agents)
        limit: Maximum number of runs to return
    
    Returns:
        JSON response with list of training runs
    """
    if not LIGHTNING_AVAILABLE or not rl_ledger:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        runs = rl_ledger.list_training_runs(agent_id=agent, limit=limit)
        
        runs_data = []
        for run in runs:
            runs_data.append({
                "id": run.id,
                "dataset_id": run.dataset_id,
                "base_model": run.base_model,
                "tuned_model_name": run.tuned_model_name,
                "status": run.status.value,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "created_at": run.created_at,
            })
        
        return json.dumps({
            "agent_id": agent,
            "runs_found": len(runs_data),
            "training_runs": runs_data,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error listing training runs: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_promote_deployment_tool(
    training_run_id: str,
    agent_id: str = None,
    promoted_by: str = None,
) -> str:
    """
    Promote a tuned model to active deployment.
    
    Makes the fine-tuned model from a training run the active model
    for the agent, so all subsequent requests use the tuned model.
    
    Args:
        training_run_id: ID of the completed training run
        agent_id: Agent ID (default: mcp-agents)
        promoted_by: Who is promoting (for audit trail)
    
    Returns:
        JSON response with deployment details
    """
    if not LIGHTNING_AVAILABLE or not deployment_registry:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        
        deployment = deployment_registry.promote(
            agent_id=agent,
            training_run_id=training_run_id,
            promoted_by=promoted_by,
        )
        
        if deployment:
            return json.dumps({
                "success": True,
                "deployment_id": deployment.id,
                "training_run_id": deployment.training_run_id,
                "tuned_model_name": deployment.tuned_model_name,
                "is_active": deployment.is_active,
                "promoted_at": deployment.promoted_at,
                "promoted_by": deployment.promoted_by,
            }, indent=2)
        else:
            return json.dumps({"error": "Failed to promote deployment"})
    
    except Exception as e:
        logger.error(f"Error promoting deployment: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_get_active_deployment_tool(agent_id: str = None) -> str:
    """
    Get the currently active tuned model deployment.
    
    Args:
        agent_id: Agent ID (default: mcp-agents)
    
    Returns:
        JSON response with active deployment details
    """
    if not LIGHTNING_AVAILABLE or not deployment_registry:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        deployment = deployment_registry.get_active_deployment(agent)
        
        if deployment:
            return json.dumps({
                "has_active_deployment": True,
                "deployment_id": deployment.id,
                "training_run_id": deployment.training_run_id,
                "tuned_model_name": deployment.tuned_model_name,
                "promoted_at": deployment.promoted_at,
                "promoted_by": deployment.promoted_by,
            }, indent=2)
        else:
            return json.dumps({
                "has_active_deployment": False,
                "message": "No active tuned model deployment. Using base model.",
                "base_model": FOUNDRY_MODEL_DEPLOYMENT_NAME,
            }, indent=2)
    
    except Exception as e:
        logger.error(f"Error getting active deployment: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_list_deployments_tool(agent_id: str = None, limit: int = 20) -> str:
    """
    List all deployments (active and historical).
    
    Args:
        agent_id: Filter by agent ID (default: mcp-agents)
        limit: Maximum number of deployments to return
    
    Returns:
        JSON response with list of deployments
    """
    if not LIGHTNING_AVAILABLE or not rl_ledger:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        deployments = rl_ledger.list_deployments(agent_id=agent, limit=limit)
        
        deployments_data = []
        for dep in deployments:
            deployments_data.append({
                "id": dep.id,
                "training_run_id": dep.training_run_id,
                "tuned_model_name": dep.tuned_model_name,
                "is_active": dep.is_active,
                "promoted_at": dep.promoted_at,
                "promoted_by": dep.promoted_by,
                "rollback_from": dep.rollback_from,
                "rollback_reason": dep.rollback_reason,
            })
        
        return json.dumps({
            "agent_id": agent,
            "deployments_found": len(deployments_data),
            "deployments": deployments_data,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error listing deployments: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_rollback_deployment_tool(
    agent_id: str = None,
    target_deployment_id: str = None,
    reason: str = None,
    rolled_back_by: str = None,
) -> str:
    """
    Rollback to a previous deployment.
    
    If no target is specified, rolls back to the most recent previous deployment.
    
    Args:
        agent_id: Agent ID (default: mcp-agents)
        target_deployment_id: Specific deployment to roll back to (optional)
        reason: Reason for rollback
        rolled_back_by: Who is rolling back (for audit trail)
    
    Returns:
        JSON response with new active deployment details
    """
    if not LIGHTNING_AVAILABLE or not deployment_registry:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        
        deployment = deployment_registry.rollback(
            agent_id=agent,
            target_deployment_id=target_deployment_id,
            reason=reason,
            rolled_back_by=rolled_back_by,
        )
        
        if deployment:
            return json.dumps({
                "success": True,
                "deployment_id": deployment.id,
                "tuned_model_name": deployment.tuned_model_name,
                "is_active": deployment.is_active,
                "rollback_reason": reason,
                "rolled_back_by": rolled_back_by,
            }, indent=2)
        else:
            return json.dumps({"error": "No previous deployment to roll back to"})
    
    except Exception as e:
        logger.error(f"Error rolling back deployment: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_deactivate_deployment_tool(agent_id: str = None, reason: str = None) -> str:
    """
    Deactivate the current tuned model deployment.
    
    This causes the agent to revert to using the base model.
    
    Args:
        agent_id: Agent ID (default: mcp-agents)
        reason: Reason for deactivation
    
    Returns:
        JSON response confirming deactivation
    """
    if not LIGHTNING_AVAILABLE or not rl_ledger:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        
        success = rl_ledger.deactivate_deployment(agent_id=agent)
        
        if success:
            return json.dumps({
                "success": True,
                "message": f"Tuned model deactivated for agent {agent}. Now using base model.",
                "base_model": FOUNDRY_MODEL_DEPLOYMENT_NAME,
                "reason": reason,
            }, indent=2)
        else:
            return json.dumps({"error": "Failed to deactivate deployment"})
    
    except Exception as e:
        logger.error(f"Error deactivating deployment: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def lightning_get_stats_tool(agent_id: str = None) -> str:
    """
    Get comprehensive statistics about Agent Lightning for an agent.
    
    Args:
        agent_id: Agent ID (default: mcp-agents)
    
    Returns:
        JSON response with episode, reward, dataset, and deployment statistics
    """
    if not LIGHTNING_AVAILABLE or not rl_ledger:
        return json.dumps({"error": "Agent Lightning not available"})
    
    try:
        agent = agent_id or LIGHTNING_AGENT_ID
        
        # Get counts from ledger
        episodes = rl_ledger.query_episodes(agent_id=agent, limit=1000)
        rewards = rl_ledger.query_rewards(agent_id=agent, limit=1000)
        datasets = rl_ledger.list_datasets(agent_id=agent, limit=100)
        runs = rl_ledger.list_training_runs(agent_id=agent, limit=100)
        deployments = rl_ledger.list_deployments(agent_id=agent, limit=100)
        
        # Get active deployment
        active_deployment = deployment_registry.get_active_deployment(agent) if deployment_registry else None
        
        # Calculate reward statistics
        reward_values = [r.value for r in rewards]
        avg_reward = sum(reward_values) / len(reward_values) if reward_values else 0
        
        # Count by status
        status_counts = {}
        for run in runs:
            status = run.status.value
            status_counts[status] = status_counts.get(status, 0) + 1
        
        return json.dumps({
            "agent_id": agent,
            "lightning_enabled": ENABLE_LIGHTNING_CAPTURE,
            "use_tuned_model": USE_TUNED_MODEL,
            "statistics": {
                "total_episodes": len(episodes),
                "total_rewards": len(rewards),
                "average_reward": round(avg_reward, 3),
                "total_datasets": len(datasets),
                "total_training_runs": len(runs),
                "training_run_status": status_counts,
                "total_deployments": len(deployments),
            },
            "active_deployment": {
                "has_active": active_deployment is not None,
                "model_name": active_deployment.tuned_model_name if active_deployment else None,
                "promoted_at": active_deployment.promoted_at if active_deployment else None,
            } if active_deployment else {"has_active": False},
            "current_model": get_model_deployment(),
            "base_model": FOUNDRY_MODEL_DEPLOYMENT_NAME,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error getting Lightning stats: {e}")
        return json.dumps({"error": str(e)})


# =========================================
# Fabric Data Agents Tools (delegated to fabric_tools.py)
# =========================================

@ai_function
def fabric_query_lakehouse(lakehouse_id: str, query: str, lakehouse_name: str = "") -> str:
    """
    Execute a Spark SQL query against a Fabric Lakehouse.
    
    This tool allows AI agents to query data in Fabric Lakehouses using Spark SQL.
    Use this for big data analytics, ETL operations, and data exploration.
    
    Args:
        lakehouse_id: The ID of the lakehouse to query
        query: The Spark SQL query to execute (e.g., "SELECT * FROM sales LIMIT 10")
        lakehouse_name: Optional friendly name of the lakehouse for logging
    
    Returns:
        JSON string containing query results with schema and data
    """
    if not FABRIC_DATA_AGENTS_AVAILABLE:
        return json.dumps({"error": "Fabric Data Agents not available - install required packages"})
    
    return fabric_query_lakehouse_tool(lakehouse_id, query, lakehouse_name)


@ai_function
def fabric_query_warehouse(warehouse_id: str, query: str, warehouse_name: str = "") -> str:
    """
    Execute a T-SQL query against a Fabric Data Warehouse.
    
    This tool allows AI agents to query data in Fabric Data Warehouses using T-SQL.
    Use this for structured data analytics, reporting, and SQL-based operations.
    
    Args:
        warehouse_id: The ID of the warehouse to query
        query: The T-SQL query to execute (e.g., "SELECT TOP 10 * FROM customers")
        warehouse_name: Optional friendly name of the warehouse for logging
    
    Returns:
        JSON string containing query results with schema and data
    """
    if not FABRIC_DATA_AGENTS_AVAILABLE:
        return json.dumps({"error": "Fabric Data Agents not available - install required packages"})
    
    return fabric_query_warehouse_tool(warehouse_id, query, warehouse_name)


@ai_function
def fabric_trigger_pipeline(pipeline_id: str, pipeline_name: str = "", parameters: str = "{}") -> str:
    """
    Trigger execution of a Fabric Data Pipeline.
    
    This tool allows AI agents to start Fabric Data Pipelines for ETL, data movement,
    and orchestration operations.
    
    Args:
        pipeline_id: The ID of the pipeline to trigger
        pipeline_name: Optional friendly name of the pipeline for logging
        parameters: JSON string of parameters to pass to the pipeline (default: empty dict)
    
    Returns:
        JSON string containing pipeline run information including run ID
    """
    if not FABRIC_DATA_AGENTS_AVAILABLE:
        return json.dumps({"error": "Fabric Data Agents not available - install required packages"})
    
    return fabric_trigger_pipeline_tool(pipeline_id, pipeline_name, parameters)


@ai_function
def fabric_get_pipeline_status(pipeline_id: str, run_id: str, pipeline_name: str = "") -> str:
    """
    Get the status of a Fabric Data Pipeline run.
    
    This tool allows AI agents to monitor the execution status of Fabric Data Pipelines.
    Use this to check if a pipeline has completed, failed, or is still running.
    
    Args:
        pipeline_id: The ID of the pipeline
        run_id: The ID of the pipeline run to check
        pipeline_name: Optional friendly name of the pipeline for logging
    
    Returns:
        JSON string containing pipeline run status and details
    """
    if not FABRIC_DATA_AGENTS_AVAILABLE:
        return json.dumps({"error": "Fabric Data Agents not available - install required packages"})
    
    return fabric_get_pipeline_status_tool(pipeline_id, run_id, pipeline_name)


@ai_function
def fabric_query_semantic_model(
    dataset_id: str,
    query: str,
    dataset_name: str = "",
    query_language: str = "DAX"
) -> str:
    """
    Query a Power BI semantic model (dataset) using DAX or MDX.
    
    This tool allows AI agents to query Power BI semantic models for analytics
    and reporting. Supports both DAX (Data Analysis Expressions) and MDX queries.
    
    Args:
        dataset_id: The ID of the semantic model (dataset) to query
        query: The DAX or MDX query to execute
        dataset_name: Optional friendly name of the dataset for logging
        query_language: Query language to use ("DAX" or "MDX", default: "DAX")
    
    Returns:
        JSON string containing query results with schema and data
    """
    if not FABRIC_DATA_AGENTS_AVAILABLE:
        return json.dumps({"error": "Fabric Data Agents not available - install required packages"})
    
    return fabric_query_semantic_model_tool(dataset_id, query, dataset_name, query_language)


@ai_function
def fabric_list_resources(resource_type: str = "all") -> str:
    """
    List Fabric resources in the workspace.
    
    This tool allows AI agents to discover available Fabric resources
    (lakehouses, warehouses, pipelines, semantic models) in the workspace.
    
    Args:
        resource_type: Type of resources to list ("lakehouse", "warehouse", "pipeline",
                      "semantic_model", or "all" for all types)
    
    Returns:
        JSON string containing list of resources
    """
    if not FABRIC_DATA_AGENTS_AVAILABLE:
        return json.dumps({"error": "Fabric Data Agents not available - install required packages"})
    
    return fabric_list_resources_tool(resource_type)


# Create the AI Agent with tools
def create_mcp_agent():
    """Create and configure the MCP AI Agent with Microsoft Agent Framework."""
    if not FOUNDRY_PROJECT_ENDPOINT:
        logger.warning("FOUNDRY_PROJECT_ENDPOINT not configured - AI Agent will not be available")
        return None
    
    try:
        agent_credential = DefaultAzureCredential()
        client = AzureAIAgentClient(
            endpoint=FOUNDRY_PROJECT_ENDPOINT,
            credential=agent_credential,
        )
        logger.info("MCP AI Agent Client created successfully")
        return client
    except Exception as e:
        logger.error(f"Error creating AI Agent: {e}")
        return None


# Initialize the AI agent client (will be set on startup)
mcp_ai_agent = None


@dataclass
class MCPTool:
    """MCP Tool definition"""
    name: str
    description: str
    inputSchema: Dict[str, Any]


@dataclass
class MCPToolResult:
    """MCP Tool execution result"""
    content: list
    isError: bool = False


# Define MCP tools
TOOLS = [
    MCPTool(
        name="hello_mcp",
        description="Hello world MCP tool.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": []
        }
    ),
    MCPTool(
        name="get_snippet",
        description="Retrieve a snippet by name from Azure Blob Storage.",
        inputSchema={
            "type": "object",
            "properties": {
                "snippetname": {
                    "type": "string",
                    "description": "The name of the snippet to retrieve"
                }
            },
            "required": ["snippetname"]
        }
    ),
    MCPTool(
        name="save_snippet",
        description="Save a snippet with a name to Azure Blob Storage.",
        inputSchema={
            "type": "object",
            "properties": {
                "snippetname": {
                    "type": "string",
                    "description": "The name of the snippet"
                },
                "snippet": {
                    "type": "string",
                    "description": "The content of the snippet"
                }
            },
            "required": ["snippetname", "snippet"]
        }
    ),
    MCPTool(
        name="ask_foundry",
        description="Ask a question and get an answer using the Azure AI Foundry model.",
        inputSchema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the AI model"
                }
            },
            "required": ["question"]
        }
    ),
    MCPTool(
        name="next_best_action",
        description="Execute a healthcare digital quality management task and return concrete results. Uses three memory layers: (1) Short-term memory - finds similar past tasks from CosmosDB, (2) Long-term memory - retrieves task instructions from AI Search, (3) Facts memory - queries domain facts from Fabric IQ ontologies. Identifies quality gaps, scores actions, queues outreach, creates care alerts, logs interventions, and delivers specific data-driven recommendations. Returns executed results with confirmed actions and quantified outcomes.",
        inputSchema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task description in natural language (English sentence) to analyze and plan"
                }
            },
            "required": ["task"]
        }
    ),
    MCPTool(
        name="store_memory",
        description="Store information in short-term memory for later retrieval. Useful for remembering context, user preferences, or intermediate results within a session.",
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The content to remember"
                },
                "session_id": {
                    "type": "string",
                    "description": "The session ID to associate the memory with"
                },
                "memory_type": {
                    "type": "string",
                    "description": "Type of memory: context, conversation, task, or plan",
                    "enum": ["context", "conversation", "task", "plan"]
                }
            },
            "required": ["content", "session_id"]
        }
    ),
    MCPTool(
        name="recall_memory",
        description="Recall relevant memories from short-term memory based on semantic similarity. Returns memories that are contextually related to the query.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The query to search for relevant memories"
                },
                "session_id": {
                    "type": "string",
                    "description": "The session ID to search within"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of memories to return (default: 5)"
                }
            },
            "required": ["query", "session_id"]
        }
    ),
    MCPTool(
        name="get_session_history",
        description="Get conversation history for a session. Returns the messages exchanged in the session.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID to get history for"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to return (default: 20)"
                }
            },
            "required": ["session_id"]
        }
    ),
    MCPTool(
        name="clear_session_memory",
        description="Clear all short-term memory for a session. Use when starting fresh or cleaning up.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID to clear"
                }
            },
            "required": ["session_id"]
        }
    ),
    # =========================================
    # Fabric IQ Facts Memory Tools
    # =========================================
    MCPTool(
        name="search_facts",
        description="Search for relevant facts from Fabric IQ ontology-grounded knowledge. Uses semantic search to find facts across Customer, DevOps, and User Management domains.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query to search for relevant facts"
                },
                "domain": {
                    "type": "string",
                    "description": "Optional domain filter (customer, devops, user_management)",
                    "enum": ["customer", "devops", "user_management"]
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of facts to return (default: 5)"
                }
            },
            "required": ["query"]
        }
    ),
    MCPTool(
        name="get_customer_churn_facts",
        description="Retrieve customer churn analysis facts from Fabric IQ. Returns predictions and observations about customer churn risk with segment analysis.",
        inputSchema={
            "type": "object",
            "properties": {
                "risk_level": {
                    "type": "string",
                    "description": "Optional filter by risk level",
                    "enum": ["critical", "high", "medium", "low", "minimal"]
                }
            },
            "required": []
        }
    ),
    MCPTool(
        name="get_pipeline_health_facts",
        description="Retrieve CI/CD pipeline health facts from Fabric IQ. Returns observations about pipeline success rates, failures, and deployment status.",
        inputSchema={
            "type": "object",
            "properties": {
                "include_failures": {
                    "type": "boolean",
                    "description": "Whether to include detailed failure information (default: true)"
                }
            },
            "required": []
        }
    ),
    MCPTool(
        name="get_user_security_facts",
        description="Retrieve user security and access facts from Fabric IQ. Returns observations about user activity, authentication patterns, and security alerts.",
        inputSchema={
            "type": "object",
            "properties": {
                "include_alerts": {
                    "type": "boolean",
                    "description": "Whether to include security alert facts (default: true)"
                }
            },
            "required": []
        }
    ),
    MCPTool(
        name="cross_domain_analysis",
        description="Perform cross-domain reasoning to find connections between different domains. Leverages Fabric IQ's graph capabilities for entity-relationship traversal.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query describing the connection to find"
                },
                "source_domain": {
                    "type": "string",
                    "description": "Starting domain for analysis",
                    "enum": ["customer", "devops", "user_management"]
                },
                "target_domain": {
                    "type": "string",
                    "description": "Target domain to connect to",
                    "enum": ["customer", "devops", "user_management"]
                }
            },
            "required": ["query", "source_domain", "target_domain"]
        }
    ),
    MCPTool(
        name="get_facts_memory_stats",
        description="Get statistics about the Fabric IQ Facts Memory. Returns counts of entities and facts by domain.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": []
        }
    ),
    # =========================================
    # Agent Lightning Tools (RLHF Fine-Tuning)
    # =========================================
    MCPTool(
        name="lightning_list_episodes",
        description="List captured episodes from Agent Lightning. Episodes represent agent interactions (user input → tool calls → response) that can be used for RLHF fine-tuning.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Filter by agent ID (default: mcp-agents)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of episodes to return (default: 20)"
                },
                "start_date": {
                    "type": "string",
                    "description": "Filter episodes after this date (ISO format)"
                },
                "end_date": {
                    "type": "string",
                    "description": "Filter episodes before this date (ISO format)"
                }
            },
            "required": []
        }
    ),
    MCPTool(
        name="lightning_get_episode",
        description="Get detailed information about a specific episode including all tool calls.",
        inputSchema={
            "type": "object",
            "properties": {
                "episode_id": {
                    "type": "string",
                    "description": "The ID of the episode to retrieve"
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID (default: mcp-agents)"
                }
            },
            "required": ["episode_id"]
        }
    ),
    MCPTool(
        name="lightning_assign_reward",
        description="Assign a reward/label to an episode for RLHF training. Rewards indicate the quality of the agent's response.",
        inputSchema={
            "type": "object",
            "properties": {
                "episode_id": {
                    "type": "string",
                    "description": "The ID of the episode to reward"
                },
                "reward_value": {
                    "type": "number",
                    "description": "Reward value from -1.0 (bad) to 1.0 (good)"
                },
                "reward_source": {
                    "type": "string",
                    "description": "Source of reward",
                    "enum": ["human_approval", "eval_score", "test_result", "safety_check"]
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID (default: mcp-agents)"
                },
                "rubric": {
                    "type": "string",
                    "description": "Evaluation rubric/criteria used"
                },
                "evaluator": {
                    "type": "string",
                    "description": "Who/what evaluated"
                },
                "comments": {
                    "type": "string",
                    "description": "Additional comments"
                }
            },
            "required": ["episode_id", "reward_value"]
        }
    ),
    MCPTool(
        name="lightning_list_rewards",
        description="List rewards assigned to episodes.",
        inputSchema={
            "type": "object",
            "properties": {
                "episode_id": {
                    "type": "string",
                    "description": "Filter by episode ID (optional)"
                },
                "agent_id": {
                    "type": "string",
                    "description": "Filter by agent ID (default: mcp-agents)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of rewards to return"
                }
            },
            "required": []
        }
    ),
    MCPTool(
        name="lightning_build_dataset",
        description="Build a fine-tuning dataset from rewarded episodes. Creates JSONL files for Azure OpenAI fine-tuning.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name for the dataset"
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID (default: mcp-agents)"
                },
                "description": {
                    "type": "string",
                    "description": "Optional description"
                },
                "min_reward": {
                    "type": "number",
                    "description": "Minimum average reward for episode inclusion (default: 0.5)"
                }
            },
            "required": ["name"]
        }
    ),
    MCPTool(
        name="lightning_list_datasets",
        description="List available fine-tuning datasets.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Filter by agent ID (default: mcp-agents)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of datasets to return"
                }
            },
            "required": []
        }
    ),
    MCPTool(
        name="lightning_start_training",
        description="Start a fine-tuning training run using Azure OpenAI.",
        inputSchema={
            "type": "object",
            "properties": {
                "dataset_id": {
                    "type": "string",
                    "description": "ID of the dataset to use for training"
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID (default: mcp-agents)"
                },
                "base_model": {
                    "type": "string",
                    "description": "Base model to fine-tune (default: gpt-4o-mini-2024-07-18)"
                },
                "n_epochs": {
                    "type": "integer",
                    "description": "Number of training epochs (default: 3)"
                }
            },
            "required": ["dataset_id"]
        }
    ),
    MCPTool(
        name="lightning_get_training_status",
        description="Get the status of a training run.",
        inputSchema={
            "type": "object",
            "properties": {
                "training_run_id": {
                    "type": "string",
                    "description": "ID of the training run"
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID (default: mcp-agents)"
                }
            },
            "required": ["training_run_id"]
        }
    ),
    MCPTool(
        name="lightning_list_training_runs",
        description="List training runs.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Filter by agent ID (default: mcp-agents)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of runs to return"
                }
            },
            "required": []
        }
    ),
    MCPTool(
        name="lightning_promote_deployment",
        description="Promote a tuned model to active deployment. Makes the fine-tuned model the active model for the agent.",
        inputSchema={
            "type": "object",
            "properties": {
                "training_run_id": {
                    "type": "string",
                    "description": "ID of the completed training run"
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID (default: mcp-agents)"
                },
                "promoted_by": {
                    "type": "string",
                    "description": "Who is promoting (for audit trail)"
                }
            },
            "required": ["training_run_id"]
        }
    ),
    MCPTool(
        name="lightning_get_active_deployment",
        description="Get the currently active tuned model deployment.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID (default: mcp-agents)"
                }
            },
            "required": []
        }
    ),
    MCPTool(
        name="lightning_list_deployments",
        description="List all deployments (active and historical).",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Filter by agent ID (default: mcp-agents)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of deployments to return"
                }
            },
            "required": []
        }
    ),
    MCPTool(
        name="lightning_rollback_deployment",
        description="Rollback to a previous deployment. If no target is specified, rolls back to the most recent previous deployment.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID (default: mcp-agents)"
                },
                "target_deployment_id": {
                    "type": "string",
                    "description": "Specific deployment to roll back to (optional)"
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for rollback"
                },
                "rolled_back_by": {
                    "type": "string",
                    "description": "Who is rolling back (for audit trail)"
                }
            },
            "required": []
        }
    ),
    MCPTool(
        name="lightning_deactivate_deployment",
        description="Deactivate the current tuned model deployment. Causes the agent to revert to using the base model.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID (default: mcp-agents)"
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for deactivation"
                }
            },
            "required": []
        }
    ),
    MCPTool(
        name="lightning_get_stats",
        description="Get comprehensive statistics about Agent Lightning including episodes, rewards, datasets, and deployments.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID (default: mcp-agents)"
                }
            },
            "required": []
        }
    ),
    # =========================================
    # Agent Evaluation Tools (Azure AI Eval SDK)
    # =========================================
    MCPTool(
        name="evaluate_intent_resolution",
        description="Evaluate how well an agent resolved the user's intent using the Azure AI Evaluation SDK IntentResolutionEvaluator. Returns a score from 1-5.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user query or conversation history"
                },
                "response": {
                    "type": "string",
                    "description": "The agent's response to evaluate"
                }
            },
            "required": ["query", "response"]
        }
    ),
    MCPTool(
        name="evaluate_tool_call_accuracy",
        description="Evaluate the accuracy of tool calls made by an agent using the Azure AI Evaluation SDK ToolCallAccuracyEvaluator. Returns a score from 1-5.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user query"
                },
                "tool_calls": {
                    "type": "array",
                    "description": "Array of tool calls made by the agent",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "tool_call_id": {"type": "string"},
                            "name": {"type": "string"},
                            "arguments": {"type": "object"}
                        }
                    }
                },
                "tool_definitions": {
                    "type": "array",
                    "description": "Array of available tool definitions (optional, uses defaults if not provided)"
                }
            },
            "required": ["query", "tool_calls"]
        }
    ),
    MCPTool(
        name="evaluate_task_adherence",
        description="Evaluate how well an agent's response adheres to the assigned task using the Azure AI Evaluation SDK TaskAdherenceEvaluator. Returns flagged (true/false) and reasoning.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user query (task)"
                },
                "response": {
                    "type": "string",
                    "description": "The agent's response"
                },
                "tool_calls": {
                    "type": "array",
                    "description": "Optional array of tool calls made (for context)"
                },
                "system_message": {
                    "type": "string",
                    "description": "Optional system message defining the agent's role"
                }
            },
            "required": ["query", "response"]
        }
    ),
    MCPTool(
        name="evaluate_groundedness",
        description="Evaluate how well an agent's response is grounded in the provided context using the Azure AI Evaluation SDK GroundednessEvaluator. Returns a score from 1-5.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user query"
                },
                "response": {
                    "type": "string",
                    "description": "The agent's response to evaluate"
                },
                "context": {
                    "type": "string",
                    "description": "The grounding context/source documents the response should be based on"
                }
            },
            "required": ["query", "response", "context"]
        }
    ),
    MCPTool(
        name="evaluate_relevance",
        description="Evaluate how relevant an agent's response is to the user query using the Azure AI Evaluation SDK RelevanceEvaluator. Returns a score from 1-5.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user query"
                },
                "response": {
                    "type": "string",
                    "description": "The agent's response to evaluate"
                }
            },
            "required": ["query", "response"]
        }
    ),
    MCPTool(
        name="run_agent_evaluation",
        description="Run a comprehensive evaluation on agent response data using all five evaluators (IntentResolution, ToolCallAccuracy, TaskAdherence, Groundedness, Relevance). Returns scores and pass/fail status.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user query"
                },
                "response": {
                    "type": "string",
                    "description": "The agent's response"
                },
                "tool_calls": {
                    "type": "array",
                    "description": "Array of tool calls made by the agent"
                },
                "tool_definitions": {
                    "type": "array",
                    "description": "Available tool definitions (optional)"
                },
                "system_message": {
                    "type": "string",
                    "description": "Optional system message"
                },
                "context": {
                    "type": "string",
                    "description": "Optional grounding context for GroundednessEvaluator"
                },
                "thresholds": {
                    "type": "object",
                    "description": "Optional score thresholds (default: 3 for each)",
                    "properties": {
                        "intent_resolution": {"type": "integer"},
                        "tool_call_accuracy": {"type": "integer"},
                        "task_adherence": {"type": "integer"},
                        "groundedness": {"type": "integer"},
                        "relevance": {"type": "integer"}
                    }
                }
            },
            "required": ["query", "response"]
        }
    ),
    MCPTool(
        name="run_batch_evaluation",
        description="Run evaluation on multiple query/response pairs. Returns aggregated metrics including average scores and pass rates.",
        inputSchema={
            "type": "object",
            "properties": {
                "evaluation_data": {
                    "type": "array",
                    "description": "Array of evaluation items, each containing query, response, and optional tool_calls/context",
                    "items": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "response": {"type": "string"},
                            "tool_calls": {"type": "array"},
                            "system_message": {"type": "string"},
                            "context": {"type": "string"}
                        },
                        "required": ["query", "response"]
                    }
                },
                "thresholds": {
                    "type": "object",
                    "description": "Optional score thresholds"
                }
            },
            "required": ["evaluation_data"]
        }
    ),
    MCPTool(
        name="get_evaluation_status",
        description="Check if agent evaluation tools are available and properly configured.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": []
        }
    ),
    # =========================================
    # Fabric Agent Orchestrator Tools
    # =========================================
    MCPTool(
        name="fabric_agent_query",
        description="Intelligent Fabric agent that automatically routes your request to the right specialist (Lakehouse, Warehouse, Pipeline, or Semantic Model). Supports natural-language queries, schema exploration, pipeline management, and cross-domain analytics.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language request (e.g., 'show all high-risk customers from the lakehouse')"
                },
                "agent_type": {
                    "type": "string",
                    "description": "Force a specific agent: lakehouse, warehouse, pipeline, semantic_model (optional - auto-detected if omitted)",
                    "enum": ["lakehouse", "warehouse", "pipeline", "semantic_model"]
                },
                "lakehouse_id": {"type": "string", "description": "Target lakehouse ID (optional)"},
                "warehouse_id": {"type": "string", "description": "Target warehouse ID (optional)"},
                "pipeline_id": {"type": "string", "description": "Target pipeline ID (optional)"},
                "dataset_id": {"type": "string", "description": "Target semantic model/dataset ID (optional)"},
                "sql_query": {"type": "string", "description": "Explicit SQL/DAX query to execute (optional)"}
            },
            "required": ["query"]
        }
    ),
    MCPTool(
        name="fabric_agent_cross_domain",
        description="Execute a cross-domain query spanning multiple Fabric resource types (e.g., query lakehouse data then trigger a pipeline refresh).",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Cross-domain natural-language request"
                },
                "lakehouse_id": {"type": "string", "description": "Lakehouse ID (optional)"},
                "warehouse_id": {"type": "string", "description": "Warehouse ID (optional)"},
                "pipeline_id": {"type": "string", "description": "Pipeline ID (optional)"},
                "dataset_id": {"type": "string", "description": "Dataset ID (optional)"}
            },
            "required": ["query"]
        }
    ),
    MCPTool(
        name="fabric_agent_list_all",
        description="List all available Fabric resources (lakehouses, warehouses, pipelines, semantic models) in the workspace.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": []
        }
    ),
    # =========================================
    # OneLake File Operations Tools
    # =========================================
    MCPTool(
        name="onelake_list_files",
        description="List files and directories in a OneLake Lakehouse path.",
        inputSchema={
            "type": "object",
            "properties": {
                "lakehouse_id": {"type": "string", "description": "Lakehouse item ID"},
                "path": {"type": "string", "description": "Directory path (default: root)"},
                "section": {"type": "string", "description": "'Files' or 'Tables'", "enum": ["Files", "Tables"]},
                "recursive": {"type": "boolean", "description": "List recursively (default: false)"}
            },
            "required": ["lakehouse_id"]
        }
    ),
    MCPTool(
        name="onelake_read_file",
        description="Read a file from a OneLake Lakehouse (text and binary supported).",
        inputSchema={
            "type": "object",
            "properties": {
                "lakehouse_id": {"type": "string", "description": "Lakehouse item ID"},
                "path": {"type": "string", "description": "File path within the lakehouse"},
                "section": {"type": "string", "description": "'Files' or 'Tables'", "enum": ["Files", "Tables"]}
            },
            "required": ["lakehouse_id", "path"]
        }
    ),
    MCPTool(
        name="onelake_write_file",
        description="Write / upload a file to a OneLake Lakehouse.",
        inputSchema={
            "type": "object",
            "properties": {
                "lakehouse_id": {"type": "string", "description": "Lakehouse item ID"},
                "path": {"type": "string", "description": "Destination file path"},
                "content": {"type": "string", "description": "File content"},
                "section": {"type": "string", "description": "'Files' or 'Tables'", "enum": ["Files", "Tables"]},
                "overwrite": {"type": "boolean", "description": "Overwrite if exists (default: true)"}
            },
            "required": ["lakehouse_id", "path", "content"]
        }
    ),
    MCPTool(
        name="onelake_delete_file",
        description="Delete a file or directory from a OneLake Lakehouse.",
        inputSchema={
            "type": "object",
            "properties": {
                "lakehouse_id": {"type": "string", "description": "Lakehouse item ID"},
                "path": {"type": "string", "description": "Path to delete"},
                "section": {"type": "string", "description": "'Files' or 'Tables'", "enum": ["Files", "Tables"]},
                "recursive": {"type": "boolean", "description": "Delete directory recursively"}
            },
            "required": ["lakehouse_id", "path"]
        }
    ),
    MCPTool(
        name="onelake_get_file_properties",
        description="Get metadata (size, last modified, content type) for a file in OneLake.",
        inputSchema={
            "type": "object",
            "properties": {
                "lakehouse_id": {"type": "string", "description": "Lakehouse item ID"},
                "path": {"type": "string", "description": "File path"},
                "section": {"type": "string", "description": "'Files' or 'Tables'", "enum": ["Files", "Tables"]}
            },
            "required": ["lakehouse_id", "path"]
        }
    ),
]


async def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> MCPToolResult:
    """Execute an MCP tool with optional Agent Lightning episode capture."""
    start_time = time.time()
    result = None
    error_message = None
    
    try:
        result = await _execute_tool_impl(tool_name, arguments)
    except Exception as e:
        logger.error(f"Error executing tool {tool_name}: {e}")
        error_message = str(e)
        result = MCPToolResult(
            content=[{"type": "text", "text": f"Error: {str(e)}"}],
            isError=True
        )
    
    # Capture episode for Agent Lightning if enabled
    if episode_capture_hook and episode_capture_hook.is_enabled():
        try:
            duration_ms = int((time.time() - start_time) * 1000)
            
            # Extract result text for capture
            result_text = ""
            if result and result.content:
                for item in result.content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        result_text += item.get("text", "")
            
            # Build user input from tool invocation
            user_input = f"Call tool '{tool_name}' with arguments: {json.dumps(arguments, default=str)}"
            
            # Capture as episode
            episode_capture_hook.capture_from_tool_result(
                tool_name=tool_name,
                arguments=arguments,
                result=result_text,
                user_input=user_input,
                model_deployment=get_model_deployment(),
                duration_ms=duration_ms,
            )
        except Exception as capture_error:
            # Never fail the tool call due to capture issues
            logger.warning(f"Failed to capture episode: {capture_error}")
    
    return result


async def _execute_tool_impl(tool_name: str, arguments: Dict[str, Any]) -> MCPToolResult:
    """Internal implementation of tool execution."""
    try:
        if tool_name == "hello_mcp":
            return MCPToolResult(
                content=[{
                    "type": "text",
                    "text": "Hello I am MCPTool!"
                }]
            )
        
        elif tool_name == "get_snippet":
            snippet_name = arguments.get("snippetname")
            if not snippet_name:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No snippet name provided"}],
                    isError=True
                )
            
            if not blob_service_client:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Storage not configured"}],
                    isError=True
                )
            
            try:
                blob_client = blob_service_client.get_blob_client(
                    container=SNIPPETS_CONTAINER,
                    blob=f"{snippet_name}.json"
                )
                blob_data = blob_client.download_blob().readall()
                snippet_content = blob_data.decode('utf-8')
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": snippet_content
                    }]
                )
            except Exception as e:
                logger.error(f"Error retrieving snippet: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error retrieving snippet: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "save_snippet":
            snippet_name = arguments.get("snippetname")
            snippet_content = arguments.get("snippet")
            
            if not snippet_name:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No snippet name provided"}],
                    isError=True
                )
            
            if not snippet_content:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No snippet content provided"}],
                    isError=True
                )
            
            if not blob_service_client:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Storage not configured"}],
                    isError=True
                )
            
            try:
                blob_client = blob_service_client.get_blob_client(
                    container=SNIPPETS_CONTAINER,
                    blob=f"{snippet_name}.json"
                )
                blob_client.upload_blob(snippet_content.encode('utf-8'), overwrite=True)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": f"Snippet '{snippet_name}' saved successfully"
                    }]
                )
            except Exception as e:
                logger.error(f"Error saving snippet: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error saving snippet: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "ask_foundry":
            question = arguments.get("question")
            if not question:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No question provided"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Foundry endpoint not configured"}],
                    isError=True
                )
            
            try:
                from openai import AzureOpenAI
                
                credential = DefaultAzureCredential()
                # Get a token for Azure Cognitive Services
                token = credential.get_token("https://cognitiveservices.azure.com/.default")
                
                # Extract the base endpoint (remove /api/projects/proj-default if present)
                # Use the services.ai.azure.com endpoint directly
                base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
                
                # Get the model deployment (may be tuned model if enabled)
                model_deployment = get_model_deployment()
                logger.info(f"Using Foundry endpoint: {base_endpoint}, model: {model_deployment}")
                
                client = AzureOpenAI(
                    azure_endpoint=base_endpoint,
                    api_key=token.token,
                    api_version="2024-02-15-preview"
                )
                
                response = client.chat.completions.create(
                    model=model_deployment,
                    messages=[{"role": "user", "content": question}]
                )
                
                answer = "No response generated"
                if response.choices and len(response.choices) > 0:
                    answer = response.choices[0].message.content
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": answer
                    }]
                )
            except Exception as e:
                logger.error(f"Error calling Foundry model: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error calling Foundry model: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "next_best_action":
            task = arguments.get("task")
            if not task:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No task provided"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Foundry endpoint not configured"}],
                    isError=True
                )
            
            if not cosmos_tasks_container or not cosmos_plans_container:
                return MCPToolResult(
                    content=[{"type": "text", "text": "CosmosDB not configured"}],
                    isError=True
                )
            
            try:
                import asyncio
                
                task_id = str(uuid.uuid4())
                timestamp = datetime.utcnow().isoformat()
                
                # Step 1: Generate embedding for the task
                logger.info(f"Generating embedding for task: {task[:100]}...")
                task_embedding = get_embedding(task)
                
                # Step 2: Analyze intent
                logger.info("Analyzing task intent...")
                intent = analyze_intent(task)
                
                # Step 3: Find similar tasks using cosine similarity (short-term memory from CosmosDB)
                logger.info("Searching for similar past tasks in CosmosDB...")
                similar_tasks = find_similar_tasks(task_embedding, threshold=0.7, limit=5)
                
                # Step 4: Search for task instructions in AI Search long-term memory
                # Uses AzureAISearchContextProvider for enhanced agentic retrieval
                task_instructions = []
                long_term_context = ""
                
                if long_term_memory:
                    # First, get context via AzureAISearchContextProvider
                    logger.info("Retrieving context via LongTermMemory with AzureAISearchContextProvider...")
                    try:
                        long_term_context = await long_term_memory.get_context(task)
                        if long_term_context:
                            logger.info(f"AzureAISearchContextProvider returned context: {len(long_term_context)} chars")
                        else:
                            logger.info("AzureAISearchContextProvider returned no context")
                    except Exception as e:
                        logger.warning(f"Failed to retrieve context from AzureAISearchContextProvider: {e}")
                    
                    # Also get structured task instructions via hybrid search
                    logger.info("Searching for task instructions in LongTermMemory...")
                    try:
                        task_instructions = await long_term_memory.search_task_instructions(
                            task_description=task,
                            limit=3,
                            include_steps=True
                        )
                        logger.info(f"Found {len(task_instructions)} relevant task instructions from LongTermMemory")
                    except Exception as e:
                        logger.warning(f"Failed to retrieve task instructions from LongTermMemory: {e}")
                else:
                    logger.info("Long-term memory not configured - skipping task instructions lookup")
                
                # Step 5: Search for domain facts in Fabric IQ facts memory
                domain_facts = []
                if facts_memory:
                    logger.info("Searching for domain facts in Fabric IQ...")
                    try:
                        fact_results = await facts_memory.search_facts(
                            query=task,
                            domain=None,
                            limit=5,
                        )
                        
                        for result in fact_results:
                            domain_facts.append({
                                'id': result.fact.id,
                                'statement': result.fact.statement,
                                'domain': result.fact.domain,
                                'confidence': result.fact.confidence,
                                'relevance': result.relevance,
                            })
                        logger.info(f"Found {len(domain_facts)} relevant domain facts from Fabric IQ")
                    except Exception as e:
                        logger.warning(f"Failed to retrieve domain facts: {e}")
                else:
                    logger.info("Facts memory not configured - skipping domain facts lookup")
                
                # Step 6: Generate plan based on task, similar past tasks, and domain knowledge
                logger.info("Generating execution plan...")
                plan_steps = generate_plan_with_instructions(task, similar_tasks, task_instructions, domain_facts)
                
                # Step 7: Store task in CosmosDB
                task_doc = {
                    'id': task_id,
                    'task': task,
                    'intent': intent,
                    'embedding': task_embedding,
                    'created_at': timestamp,
                    'similar_task_count': len(similar_tasks),
                    'task_instructions_count': len(task_instructions),
                    'domain_facts_count': len(domain_facts),
                    'long_term_memory_used': len(task_instructions) > 0,
                    'facts_memory_used': len(domain_facts) > 0,
                }
                cosmos_tasks_container.upsert_item(task_doc)
                logger.info(f"Task stored in CosmosDB with id: {task_id}")
                
                # Step 8: Store plan in CosmosDB
                plan_doc = {
                    'id': str(uuid.uuid4()),
                    'taskId': task_id,
                    'task': task,
                    'intent': intent,
                    'steps': plan_steps,
                    'similar_tasks_referenced': [{'id': st['id'], 'similarity': st['similarity']} for st in similar_tasks],
                    'task_instructions_used': [{'name': ti.get('name', 'unknown')} for ti in task_instructions] if task_instructions else [],
                    'domain_facts_used': [{'statement': df['statement'][:100]} for df in domain_facts] if domain_facts else [],
                    'created_at': timestamp,
                    'status': 'planned'
                }
                cosmos_plans_container.upsert_item(plan_doc)
                logger.info(f"Plan stored in CosmosDB for task: {task_id}")
                
                # Build response
                response = {
                    'task_id': task_id,
                    'task': task,
                    'intent': intent,
                    'analysis': {
                        'similar_tasks_found': len(similar_tasks),
                        'similar_tasks': [
                            {
                                'task': st['task'],
                                'intent': st['intent'],
                                'similarity_score': round(st['similarity'], 3)
                            }
                            for st in similar_tasks
                        ],
                        'task_instructions_found': len(task_instructions),
                        'task_instructions': [
                            {
                                'name': ti.get('name', 'unknown'),
                                'description': ti.get('description', '')[:200] if ti.get('description') else '',
                            }
                            for ti in task_instructions
                        ] if task_instructions else [],
                        'domain_facts_found': len(domain_facts),
                        'domain_facts': domain_facts,
                    },
                    'plan': {
                        'steps': plan_steps,
                        'total_steps': len(plan_steps)
                    },
                    'metadata': {
                        'created_at': timestamp,
                        'embedding_dimensions': len(task_embedding),
                        'stored_in_cosmos': True,
                        'long_term_memory_used': len(task_instructions) > 0,
                        'facts_memory_used': len(domain_facts) > 0,
                    }
                }
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps(response, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error in next_best_action: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error in next_best_action: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "store_memory":
            content = arguments.get("content")
            session_id = arguments.get("session_id")
            memory_type = arguments.get("memory_type", "context")
            
            if not content:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No content provided"}],
                    isError=True
                )
            
            if not session_id:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No session_id provided"}],
                    isError=True
                )
            
            if not short_term_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Memory provider not configured"}],
                    isError=True
                )
            
            try:
                # Map string to MemoryType enum
                type_map = {
                    "context": MemoryType.CONTEXT,
                    "conversation": MemoryType.CONVERSATION,
                    "task": MemoryType.TASK,
                    "plan": MemoryType.PLAN,
                }
                mem_type = type_map.get(memory_type.lower(), MemoryType.CONTEXT)
                
                # Generate embedding for the content
                embedding = None
                if FOUNDRY_PROJECT_ENDPOINT:
                    try:
                        embedding = get_embedding(content)
                    except Exception as e:
                        logger.warning(f"Failed to generate embedding: {e}")
                
                entry = MemoryEntry(
                    id=str(uuid.uuid4()),
                    content=content,
                    memory_type=mem_type,
                    embedding=embedding,
                    session_id=session_id,
                )
                
                entry_id = await short_term_memory.store(entry)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "success": True,
                            "memory_id": entry_id,
                            "session_id": session_id,
                            "memory_type": memory_type,
                            "has_embedding": embedding is not None,
                        })
                    }]
                )
            except Exception as e:
                logger.error(f"Error storing memory: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error storing memory: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "recall_memory":
            query = arguments.get("query")
            session_id = arguments.get("session_id")
            limit = arguments.get("limit", 5)
            
            if not query:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No query provided"}],
                    isError=True
                )
            
            if not session_id:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No session_id provided"}],
                    isError=True
                )
            
            if not short_term_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Memory provider not configured"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Foundry endpoint not configured for embeddings"}],
                    isError=True
                )
            
            try:
                query_embedding = get_embedding(query)
                
                results = await short_term_memory.search(
                    query_embedding=query_embedding,
                    limit=limit,
                    threshold=0.6,
                    session_id=session_id,
                )
                
                memories = [
                    {
                        "id": r.entry.id,
                        "content": r.entry.content,
                        "memory_type": r.entry.memory_type.value,
                        "similarity_score": round(r.score, 3),
                        "created_at": r.entry.created_at,
                    }
                    for r in results
                ]
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "query": query,
                            "session_id": session_id,
                            "memories_found": len(memories),
                            "memories": memories,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error recalling memory: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error recalling memory: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "get_session_history":
            session_id = arguments.get("session_id")
            limit = arguments.get("limit", 20)
            
            if not session_id:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No session_id provided"}],
                    isError=True
                )
            
            if not short_term_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Memory provider not configured"}],
                    isError=True
                )
            
            try:
                history = await short_term_memory.get_conversation_history(session_id, limit)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "session_id": session_id,
                            "message_count": len(history),
                            "messages": history,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error getting session history: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error getting session history: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "clear_session_memory":
            session_id = arguments.get("session_id")
            
            if not session_id:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No session_id provided"}],
                    isError=True
                )
            
            if not short_term_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Memory provider not configured"}],
                    isError=True
                )
            
            try:
                count = await short_term_memory.clear_session(session_id)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "success": True,
                            "session_id": session_id,
                            "entries_cleared": count,
                        })
                    }]
                )
            except Exception as e:
                logger.error(f"Error clearing session memory: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error clearing session memory: {str(e)}"}],
                    isError=True
                )
        
        # =========================================
        # Fabric IQ Facts Memory Tool Handlers
        # =========================================
        
        elif tool_name == "search_facts":
            query = arguments.get("query")
            domain = arguments.get("domain")
            limit = arguments.get("limit", 5)
            
            if not query:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No query provided"}],
                    isError=True
                )
            
            if not facts_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Facts memory not configured"}],
                    isError=True
                )
            
            try:
                results = await facts_memory.search_facts(
                    query=query,
                    domain=domain,
                    limit=limit,
                )
                
                facts_data = [
                    {
                        "id": r.fact.id,
                        "statement": r.fact.statement,
                        "domain": r.fact.domain,
                        "fact_type": r.fact.fact_type,
                        "confidence": round(r.fact.confidence, 3),
                        "relevance_score": round(r.score, 3),
                        "context": r.fact.context,
                    }
                    for r in results
                ]
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "query": query,
                            "domain_filter": domain,
                            "facts_found": len(facts_data),
                            "facts": facts_data,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error searching facts: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error searching facts: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "get_customer_churn_facts":
            risk_level = arguments.get("risk_level")
            
            if not facts_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Facts memory not configured"}],
                    isError=True
                )
            
            try:
                results = await facts_memory.search_facts(
                    query="customer churn risk prediction",
                    domain="customer",
                    fact_type="prediction",
                    limit=20,
                )
                
                facts_data = []
                for r in results:
                    if risk_level:
                        fact_risk = r.fact.context.get("risk_level", "")
                        if fact_risk.lower() != risk_level.lower():
                            continue
                    
                    facts_data.append({
                        "customer_id": r.fact.evidence[0] if r.fact.evidence else None,
                        "statement": r.fact.statement,
                        "churn_risk": r.fact.confidence,
                        "risk_level": r.fact.context.get("risk_level"),
                        "segment": r.fact.context.get("segment"),
                        "tenure_months": r.fact.context.get("tenure_months"),
                        "monthly_spend": r.fact.context.get("monthly_spend"),
                    })
                
                avg_risk = sum(f["churn_risk"] for f in facts_data) / len(facts_data) if facts_data else 0
                risk_distribution = {}
                for f in facts_data:
                    level = f["risk_level"]
                    risk_distribution[level] = risk_distribution.get(level, 0) + 1
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "total_at_risk_customers": len(facts_data),
                            "average_churn_risk": round(avg_risk, 3),
                            "risk_distribution": risk_distribution,
                            "filter_applied": risk_level,
                            "customers": facts_data[:10],
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error getting customer churn facts: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error getting customer churn facts: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "get_pipeline_health_facts":
            include_failures = arguments.get("include_failures", True)
            
            if not facts_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Facts memory not configured"}],
                    isError=True
                )
            
            try:
                # Get pipeline health observations
                health_results = await facts_memory.search_facts(
                    query="pipeline success rate deployment",
                    domain="devops",
                    fact_type="observation",
                    limit=10,
                )
                
                pipeline_facts = []
                for r in health_results:
                    if "pipeline" in r.fact.id.lower():
                        pipeline_facts.append({
                            "pipeline_id": r.fact.evidence[0] if r.fact.evidence else None,
                            "statement": r.fact.statement,
                            "success_rate": r.fact.context.get("success_rate"),
                            "total_runs": r.fact.context.get("total_runs"),
                            "failures": r.fact.context.get("failures"),
                            "service": r.fact.context.get("service"),
                        })
                
                failure_facts = []
                if include_failures:
                    failure_results = await facts_memory.search_facts(
                        query="pipeline failure error",
                        domain="devops",
                        fact_type="observation",
                        limit=10,
                    )
                    
                    for r in failure_results:
                        if "failure" in r.fact.id.lower():
                            failure_facts.append({
                                "run_id": r.fact.evidence[0] if r.fact.evidence else None,
                                "statement": r.fact.statement,
                                "failure_category": r.fact.context.get("failure_category"),
                                "failure_stage": r.fact.context.get("failure_stage"),
                            })
                
                avg_success = sum(p["success_rate"] or 0 for p in pipeline_facts) / len(pipeline_facts) if pipeline_facts else 0
                total_failures = sum(p["failures"] or 0 for p in pipeline_facts)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "total_pipelines": len(pipeline_facts),
                            "average_success_rate": round(avg_success, 3),
                            "total_recent_failures": total_failures,
                            "pipelines": pipeline_facts,
                            "failure_details": failure_facts,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error getting pipeline health facts: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error getting pipeline health facts: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "get_user_security_facts":
            include_alerts = arguments.get("include_alerts", True)
            
            if not facts_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Facts memory not configured"}],
                    isError=True
                )
            
            try:
                activity_results = await facts_memory.search_facts(
                    query="user login authentication activity",
                    domain="user_management",
                    fact_type="observation",
                    limit=15,
                )
                
                user_facts = []
                for r in activity_results:
                    if "user-" in r.fact.id:
                        user_facts.append({
                            "user_id": r.fact.evidence[0] if r.fact.evidence else None,
                            "statement": r.fact.statement,
                            "roles": r.fact.context.get("roles"),
                            "mfa_enabled": r.fact.context.get("mfa_enabled"),
                            "status": r.fact.context.get("status"),
                            "login_successes": r.fact.context.get("login_successes"),
                            "login_failures": r.fact.context.get("login_failures"),
                        })
                
                alert_facts = []
                if include_alerts:
                    alert_results = await facts_memory.search_facts(
                        query="security alert suspicious activity",
                        domain="user_management",
                        fact_type="derived",
                        limit=10,
                    )
                    
                    for r in alert_results:
                        if "security" in r.fact.id.lower():
                            alert_facts.append({
                                "user_id": r.fact.evidence[0] if r.fact.evidence else None,
                                "statement": r.fact.statement,
                                "alert_type": r.fact.context.get("alert_type"),
                                "confidence": r.fact.confidence,
                            })
                
                total_users = len(user_facts)
                mfa_count = sum(1 for u in user_facts if u.get("mfa_enabled"))
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "total_users_analyzed": total_users,
                            "mfa_adoption_rate": round(mfa_count / total_users, 3) if total_users else 0,
                            "security_alerts_count": len(alert_facts),
                            "users": user_facts[:10],
                            "security_alerts": alert_facts,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error getting user security facts: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error getting user security facts: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "cross_domain_analysis":
            query = arguments.get("query")
            source_domain = arguments.get("source_domain")
            target_domain = arguments.get("target_domain")
            
            if not all([query, source_domain, target_domain]):
                return MCPToolResult(
                    content=[{"type": "text", "text": "Missing required parameters: query, source_domain, target_domain"}],
                    isError=True
                )
            
            if not facts_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Facts memory not configured"}],
                    isError=True
                )
            
            try:
                connections = await facts_memory.cross_domain_query(
                    query=query,
                    source_domain=source_domain,
                    target_domain=target_domain,
                )
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "query": query,
                            "source_domain": source_domain,
                            "target_domain": target_domain,
                            "connections_found": len(connections),
                            "connections": connections[:5],
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error in cross-domain analysis: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error in cross-domain analysis: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "get_facts_memory_stats":
            if not facts_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Facts memory not configured"}],
                    isError=True
                )
            
            try:
                stats = facts_memory.get_stats()
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "total_entities": stats["total_entities"],
                            "total_relationships": stats["total_relationships"],
                            "total_facts": stats["total_facts"],
                            "entities_by_type": stats["entities_by_type"],
                            "entities_by_domain": stats["entities_by_domain"],
                            "facts_by_domain": stats["facts_by_domain"],
                            "ontology_name": FABRIC_ONTOLOGY_NAME,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error getting facts memory stats: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error getting facts memory stats: {str(e)}"}],
                    isError=True
                )
        
        # =========================================
        # Agent Lightning Tool Handlers
        # =========================================
        
        elif tool_name == "lightning_list_episodes":
            if not LIGHTNING_AVAILABLE or not rl_ledger:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            limit = arguments.get("limit", 20)
            start_date = arguments.get("start_date")
            end_date = arguments.get("end_date")
            
            try:
                episodes = rl_ledger.query_episodes(
                    agent_id=agent_id,
                    start_date=start_date,
                    end_date=end_date,
                    limit=limit,
                )
                
                episodes_data = []
                for ep in episodes:
                    episodes_data.append({
                        "id": ep.id,
                        "agent_id": ep.agent_id,
                        "user_input": ep.user_input[:200] + "..." if len(ep.user_input) > 200 else ep.user_input,
                        "assistant_output": ep.assistant_output[:200] + "..." if len(ep.assistant_output) > 200 else ep.assistant_output,
                        "tool_calls_count": len(ep.tool_calls),
                        "model_deployment": ep.model_deployment,
                        "request_latency_ms": ep.request_latency_ms,
                        "created_at": ep.created_at,
                    })
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "agent_id": agent_id,
                            "episodes_found": len(episodes_data),
                            "episodes": episodes_data,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error listing episodes: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error listing episodes: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_get_episode":
            if not LIGHTNING_AVAILABLE or not rl_ledger:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            episode_id = arguments.get("episode_id")
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            
            if not episode_id:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No episode_id provided"}],
                    isError=True
                )
            
            try:
                episode = rl_ledger.get_episode(episode_id, agent_id)
                
                if not episode:
                    return MCPToolResult(
                        content=[{"type": "text", "text": f"Episode {episode_id} not found"}],
                        isError=True
                    )
                
                tool_calls_data = []
                for tc in episode.tool_calls:
                    tool_calls_data.append({
                        "tool_name": tc.tool_name,
                        "arguments": tc.arguments,
                        "result": tc.result[:500] + "..." if tc.result and len(tc.result) > 500 else tc.result,
                        "duration_ms": tc.duration_ms,
                        "error": tc.error,
                    })
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "id": episode.id,
                            "agent_id": episode.agent_id,
                            "user_input": episode.user_input,
                            "assistant_output": episode.assistant_output,
                            "tool_calls": tool_calls_data,
                            "model_deployment": episode.model_deployment,
                            "request_latency_ms": episode.request_latency_ms,
                            "created_at": episode.created_at,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error getting episode: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error getting episode: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_assign_reward":
            if not LIGHTNING_AVAILABLE or not reward_writer:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            episode_id = arguments.get("episode_id")
            reward_value = arguments.get("reward_value")
            reward_source = arguments.get("reward_source", "human_approval")
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            rubric = arguments.get("rubric")
            evaluator = arguments.get("evaluator")
            comments = arguments.get("comments")
            
            if not episode_id or reward_value is None:
                return MCPToolResult(
                    content=[{"type": "text", "text": "episode_id and reward_value are required"}],
                    isError=True
                )
            
            try:
                source_map = {
                    "human_approval": RewardSource.HUMAN_APPROVAL,
                    "eval_score": RewardSource.EVAL_SCORE,
                    "test_result": RewardSource.TEST_RESULT,
                    "safety_check": RewardSource.SAFETY_CHECK,
                }
                source = source_map.get(reward_source.lower(), RewardSource.EVAL_SCORE)
                
                reward = reward_writer.record_reward(
                    episode_id=episode_id,
                    agent_id=agent_id,
                    source=source,
                    value=reward_value,
                    rubric=rubric,
                    evaluator=evaluator,
                    metadata={"comments": comments} if comments else {},
                )
                
                if reward:
                    return MCPToolResult(
                        content=[{
                            "type": "text",
                            "text": json.dumps({
                                "success": True,
                                "reward_id": reward.id,
                                "episode_id": episode_id,
                                "value": reward.value,
                                "source": source.value,
                                "created_at": reward.created_at,
                            }, indent=2)
                        }]
                    )
                else:
                    return MCPToolResult(
                        content=[{"type": "text", "text": "Failed to store reward"}],
                        isError=True
                    )
            except Exception as e:
                logger.error(f"Error assigning reward: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error assigning reward: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_list_rewards":
            if not LIGHTNING_AVAILABLE or not rl_ledger:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            episode_id = arguments.get("episode_id")
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            limit = arguments.get("limit", 50)
            
            try:
                rewards = rl_ledger.query_rewards(
                    agent_id=agent_id,
                    episode_id=episode_id,
                    limit=limit,
                )
                
                rewards_data = []
                for r in rewards:
                    rewards_data.append({
                        "id": r.id,
                        "episode_id": r.episode_id,
                        "source": r.source.value,
                        "value": r.value,
                        "rubric": r.rubric,
                        "evaluator": r.evaluator,
                        "created_at": r.created_at,
                    })
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "agent_id": agent_id,
                            "rewards_found": len(rewards_data),
                            "rewards": rewards_data,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error listing rewards: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error listing rewards: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_build_dataset":
            if not LIGHTNING_AVAILABLE or not dataset_builder:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            name = arguments.get("name")
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            description = arguments.get("description")
            min_reward = arguments.get("min_reward", 0.5)
            
            if not name:
                return MCPToolResult(
                    content=[{"type": "text", "text": "name is required"}],
                    isError=True
                )
            
            try:
                dataset = dataset_builder.build_dataset(
                    agent_id=agent_id,
                    name=name,
                    description=description,
                    min_reward=min_reward,
                )
                
                if dataset:
                    return MCPToolResult(
                        content=[{
                            "type": "text",
                            "text": json.dumps({
                                "success": True,
                                "dataset_id": dataset.id,
                                "name": dataset.name,
                                "training_count": dataset.training_count,
                                "validation_count": dataset.validation_count,
                                "episode_count": len(dataset.episode_ids),
                                "local_path": dataset.local_path,
                                "created_at": dataset.created_at,
                            }, indent=2)
                        }]
                    )
                else:
                    return MCPToolResult(
                        content=[{"type": "text", "text": "No qualifying episodes found for dataset"}],
                        isError=True
                    )
            except Exception as e:
                logger.error(f"Error building dataset: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error building dataset: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_list_datasets":
            if not LIGHTNING_AVAILABLE or not rl_ledger:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            limit = arguments.get("limit", 20)
            
            try:
                datasets = rl_ledger.list_datasets(agent_id=agent_id, limit=limit)
                
                datasets_data = []
                for ds in datasets:
                    datasets_data.append({
                        "id": ds.id,
                        "name": ds.name,
                        "training_count": ds.training_count,
                        "validation_count": ds.validation_count,
                        "episode_count": len(ds.episode_ids),
                        "created_at": ds.created_at,
                    })
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "agent_id": agent_id,
                            "datasets_found": len(datasets_data),
                            "datasets": datasets_data,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error listing datasets: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error listing datasets: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_start_training":
            if not LIGHTNING_AVAILABLE or not training_runner:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            dataset_id = arguments.get("dataset_id")
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            base_model = arguments.get("base_model")
            n_epochs = arguments.get("n_epochs")
            
            if not dataset_id:
                return MCPToolResult(
                    content=[{"type": "text", "text": "dataset_id is required"}],
                    isError=True
                )
            
            try:
                hyperparams = {}
                if n_epochs:
                    hyperparams["n_epochs"] = n_epochs
                
                run = training_runner.start_training(
                    dataset_id=dataset_id,
                    agent_id=agent_id,
                    base_model=base_model,
                    hyperparameters=hyperparams if hyperparams else None,
                )
                
                if run:
                    return MCPToolResult(
                        content=[{
                            "type": "text",
                            "text": json.dumps({
                                "success": True,
                                "training_run_id": run.id,
                                "dataset_id": run.dataset_id,
                                "base_model": run.base_model,
                                "status": run.status.value,
                                "aoai_job_id": run.aoai_job_id,
                                "created_at": run.created_at,
                            }, indent=2)
                        }]
                    )
                else:
                    return MCPToolResult(
                        content=[{"type": "text", "text": "Failed to start training"}],
                        isError=True
                    )
            except Exception as e:
                logger.error(f"Error starting training: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error starting training: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_get_training_status":
            if not LIGHTNING_AVAILABLE or not rl_ledger:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            training_run_id = arguments.get("training_run_id")
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            
            if not training_run_id:
                return MCPToolResult(
                    content=[{"type": "text", "text": "training_run_id is required"}],
                    isError=True
                )
            
            try:
                # Use training_runner.check_status() to poll AOAI API and sync Cosmos
                if training_runner:
                    run = training_runner.check_status(training_run_id, agent_id)
                else:
                    # Fallback to direct Cosmos read if runner not available
                    run = rl_ledger.get_training_run(training_run_id, agent_id)
                
                if not run:
                    return MCPToolResult(
                        content=[{"type": "text", "text": f"Training run {training_run_id} not found"}],
                        isError=True
                    )
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "id": run.id,
                            "dataset_id": run.dataset_id,
                            "base_model": run.base_model,
                            "tuned_model_name": run.tuned_model_name,
                            "status": run.status.value,
                            "metrics": run.metrics,
                            "aoai_job_id": run.aoai_job_id,
                            "error_message": run.error_message,
                            "started_at": run.started_at,
                            "completed_at": run.completed_at,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error getting training status: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error getting training status: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_list_training_runs":
            if not LIGHTNING_AVAILABLE or not rl_ledger:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            limit = arguments.get("limit", 20)
            
            try:
                runs = rl_ledger.list_training_runs(agent_id=agent_id, limit=limit)
                
                runs_data = []
                for run in runs:
                    runs_data.append({
                        "id": run.id,
                        "dataset_id": run.dataset_id,
                        "base_model": run.base_model,
                        "tuned_model_name": run.tuned_model_name,
                        "status": run.status.value,
                        "started_at": run.started_at,
                        "completed_at": run.completed_at,
                    })
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "agent_id": agent_id,
                            "runs_found": len(runs_data),
                            "training_runs": runs_data,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error listing training runs: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error listing training runs: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_promote_deployment":
            if not LIGHTNING_AVAILABLE or not deployment_registry:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            training_run_id = arguments.get("training_run_id")
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            promoted_by = arguments.get("promoted_by")
            
            if not training_run_id:
                return MCPToolResult(
                    content=[{"type": "text", "text": "training_run_id is required"}],
                    isError=True
                )
            
            try:
                deployment = deployment_registry.promote(
                    agent_id=agent_id,
                    training_run_id=training_run_id,
                    promoted_by=promoted_by,
                )
                
                if deployment:
                    return MCPToolResult(
                        content=[{
                            "type": "text",
                            "text": json.dumps({
                                "success": True,
                                "deployment_id": deployment.id,
                                "tuned_model_name": deployment.tuned_model_name,
                                "is_active": deployment.is_active,
                                "promoted_at": deployment.promoted_at,
                            }, indent=2)
                        }]
                    )
                else:
                    return MCPToolResult(
                        content=[{"type": "text", "text": "Failed to promote deployment"}],
                        isError=True
                    )
            except Exception as e:
                logger.error(f"Error promoting deployment: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error promoting deployment: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_get_active_deployment":
            if not LIGHTNING_AVAILABLE or not deployment_registry:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            
            try:
                deployment = deployment_registry.get_active_deployment(agent_id)
                
                if deployment:
                    return MCPToolResult(
                        content=[{
                            "type": "text",
                            "text": json.dumps({
                                "has_active_deployment": True,
                                "deployment_id": deployment.id,
                                "tuned_model_name": deployment.tuned_model_name,
                                "promoted_at": deployment.promoted_at,
                            }, indent=2)
                        }]
                    )
                else:
                    return MCPToolResult(
                        content=[{
                            "type": "text",
                            "text": json.dumps({
                                "has_active_deployment": False,
                                "message": "No active tuned model. Using base model.",
                                "base_model": FOUNDRY_MODEL_DEPLOYMENT_NAME,
                            }, indent=2)
                        }]
                    )
            except Exception as e:
                logger.error(f"Error getting active deployment: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error getting active deployment: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_list_deployments":
            if not LIGHTNING_AVAILABLE or not rl_ledger:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            limit = arguments.get("limit", 20)
            
            try:
                deployments = rl_ledger.list_deployments(agent_id=agent_id, limit=limit)
                
                deployments_data = []
                for dep in deployments:
                    deployments_data.append({
                        "id": dep.id,
                        "tuned_model_name": dep.tuned_model_name,
                        "is_active": dep.is_active,
                        "promoted_at": dep.promoted_at,
                        "rollback_from": dep.rollback_from,
                    })
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "agent_id": agent_id,
                            "deployments_found": len(deployments_data),
                            "deployments": deployments_data,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error listing deployments: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error listing deployments: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_rollback_deployment":
            if not LIGHTNING_AVAILABLE or not deployment_registry:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            target_deployment_id = arguments.get("target_deployment_id")
            reason = arguments.get("reason")
            rolled_back_by = arguments.get("rolled_back_by")
            
            try:
                deployment = deployment_registry.rollback(
                    agent_id=agent_id,
                    target_deployment_id=target_deployment_id,
                    reason=reason,
                    rolled_back_by=rolled_back_by,
                )
                
                if deployment:
                    return MCPToolResult(
                        content=[{
                            "type": "text",
                            "text": json.dumps({
                                "success": True,
                                "deployment_id": deployment.id,
                                "tuned_model_name": deployment.tuned_model_name,
                                "is_active": deployment.is_active,
                                "rollback_reason": reason,
                            }, indent=2)
                        }]
                    )
                else:
                    return MCPToolResult(
                        content=[{"type": "text", "text": "No previous deployment to roll back to"}],
                        isError=True
                    )
            except Exception as e:
                logger.error(f"Error rolling back deployment: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error rolling back deployment: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_deactivate_deployment":
            if not LIGHTNING_AVAILABLE or not rl_ledger:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            reason = arguments.get("reason")
            
            try:
                success = rl_ledger.deactivate_deployment(agent_id=agent_id)
                
                if success:
                    return MCPToolResult(
                        content=[{
                            "type": "text",
                            "text": json.dumps({
                                "success": True,
                                "message": f"Tuned model deactivated. Now using base model.",
                                "base_model": FOUNDRY_MODEL_DEPLOYMENT_NAME,
                                "reason": reason,
                            }, indent=2)
                        }]
                    )
                else:
                    return MCPToolResult(
                        content=[{"type": "text", "text": "Failed to deactivate deployment"}],
                        isError=True
                    )
            except Exception as e:
                logger.error(f"Error deactivating deployment: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error deactivating deployment: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "lightning_get_stats":
            if not LIGHTNING_AVAILABLE or not rl_ledger:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Agent Lightning not available"}],
                    isError=True
                )
            
            agent_id = arguments.get("agent_id") or LIGHTNING_AGENT_ID
            
            try:
                episodes = rl_ledger.query_episodes(agent_id=agent_id, limit=1000)
                rewards = rl_ledger.query_rewards(agent_id=agent_id, limit=1000)
                datasets = rl_ledger.list_datasets(agent_id=agent_id, limit=100)
                runs = rl_ledger.list_training_runs(agent_id=agent_id, limit=100)
                deployments = rl_ledger.list_deployments(agent_id=agent_id, limit=100)
                
                active_deployment = deployment_registry.get_active_deployment(agent_id) if deployment_registry else None
                
                reward_values = [r.value for r in rewards]
                avg_reward = sum(reward_values) / len(reward_values) if reward_values else 0
                
                status_counts = {}
                for run in runs:
                    status = run.status.value
                    status_counts[status] = status_counts.get(status, 0) + 1
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "agent_id": agent_id,
                            "lightning_enabled": ENABLE_LIGHTNING_CAPTURE,
                            "use_tuned_model": USE_TUNED_MODEL,
                            "statistics": {
                                "total_episodes": len(episodes),
                                "total_rewards": len(rewards),
                                "average_reward": round(avg_reward, 3),
                                "total_datasets": len(datasets),
                                "total_training_runs": len(runs),
                                "training_run_status": status_counts,
                                "total_deployments": len(deployments),
                            },
                            "active_deployment": {
                                "has_active": active_deployment is not None,
                                "model_name": active_deployment.tuned_model_name if active_deployment else None,
                            },
                            "current_model": get_model_deployment(),
                            "base_model": FOUNDRY_MODEL_DEPLOYMENT_NAME,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error getting Lightning stats: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error getting Lightning stats: {str(e)}"}],
                    isError=True
                )
        
        # =========================================
        # Agent Evaluation Tool Handlers
        # =========================================
        elif tool_name == "get_evaluation_status":
            return MCPToolResult(
                content=[{
                    "type": "text",
                    "text": json.dumps({
                        "evaluation_available": EVALUATION_AVAILABLE,
                        "foundry_configured": bool(FOUNDRY_PROJECT_ENDPOINT),
                        "model_deployment": FOUNDRY_MODEL_DEPLOYMENT_NAME,
                        "evaluators": ["IntentResolutionEvaluator", "ToolCallAccuracyEvaluator", "TaskAdherenceEvaluator", "GroundednessEvaluator", "RelevanceEvaluator"] if EVALUATION_AVAILABLE else [],
                        "message": "Evaluation tools ready" if EVALUATION_AVAILABLE and FOUNDRY_PROJECT_ENDPOINT else "Evaluation tools not available - check azure-ai-evaluation package and FOUNDRY_PROJECT_ENDPOINT"
                    }, indent=2)
                }]
            )
        
        elif tool_name == "evaluate_intent_resolution":
            if not EVALUATION_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Azure AI Evaluation SDK not available. Install with: pip install azure-ai-evaluation"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "FOUNDRY_PROJECT_ENDPOINT not configured"}],
                    isError=True
                )
            
            query = arguments.get("query")
            response = arguments.get("response")
            
            if not query or not response:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Both 'query' and 'response' are required"}],
                    isError=True
                )
            
            try:
                # Build model config using managed identity
                # Extract base endpoint (remove /api/projects/... path if present)
                base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
                model_config = {
                    "azure_endpoint": base_endpoint.rstrip('/'),
                    "azure_deployment": EVALUATOR_MODEL_DEPLOYMENT_NAME,
                    "api_version": "2024-10-21",
                }
                
                # Initialize evaluator with managed identity credential
                credential = DefaultAzureCredential()
                # Use is_reasoning_model=True for gpt-5.x evaluator model that supports max_completion_tokens
                evaluator = IntentResolutionEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                
                # Run evaluation
                result = evaluator(query=query, response=response)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "evaluator": "IntentResolutionEvaluator",
                            "query": query[:100] + "..." if len(query) > 100 else query,
                            "score": result.get("intent_resolution", 0),
                            "explanation": result.get("intent_resolution_reason", ""),
                            "threshold_recommendation": 3,
                            "passed": result.get("intent_resolution", 0) >= 3
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error in evaluate_intent_resolution: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Evaluation error: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "evaluate_tool_call_accuracy":
            if not EVALUATION_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Azure AI Evaluation SDK not available"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "FOUNDRY_PROJECT_ENDPOINT not configured"}],
                    isError=True
                )
            
            query = arguments.get("query")
            tool_calls = arguments.get("tool_calls", [])
            tool_definitions = arguments.get("tool_definitions")
            
            if not query:
                return MCPToolResult(
                    content=[{"type": "text", "text": "'query' is required"}],
                    isError=True
                )
            
            # Use default NBA tool definitions if not provided
            if not tool_definitions:
                tool_definitions = [
                    {"name": "get_account_profile", "description": "Retrieves account profile and details.", "parameters": {"type": "object", "properties": {"account_id": {"type": "string"}}, "required": ["account_id"]}},
                    {"name": "get_recent_activities", "description": "Gets recent activities for an account.", "parameters": {"type": "object", "properties": {"account_id": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["account_id"]}},
                    {"name": "recommend_next_actions", "description": "Generates recommended next actions.", "parameters": {"type": "object", "properties": {"account_context": {"type": "object"}}, "required": ["account_context"]}},
                    {"name": "create_followup_task", "description": "Creates a follow-up task.", "parameters": {"type": "object", "properties": {"action": {"type": "string"}, "priority": {"type": "string"}, "due_date": {"type": "string"}}, "required": ["action"]}},
                    {"name": "next_best_action", "description": "Analyzes a task and generates an action plan.", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}
                ]
            
            try:
                # Extract base endpoint (remove /api/projects/... path if present)
                base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
                model_config = {
                    "azure_endpoint": base_endpoint.rstrip('/'),
                    "azure_deployment": EVALUATOR_MODEL_DEPLOYMENT_NAME,
                    "api_version": "2024-10-21",
                }
                
                credential = DefaultAzureCredential()
                # Use is_reasoning_model=True for gpt-5.x evaluator model that supports max_completion_tokens
                evaluator = ToolCallAccuracyEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                
                result = evaluator(
                    query=query,
                    tool_calls=tool_calls,
                    tool_definitions=tool_definitions
                )
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "evaluator": "ToolCallAccuracyEvaluator",
                            "query": query[:100] + "..." if len(query) > 100 else query,
                            "tool_calls_count": len(tool_calls),
                            "score": result.get("tool_call_accuracy", 0),
                            "explanation": result.get("tool_call_accuracy_reason", ""),
                            "threshold_recommendation": 3,
                            "passed": result.get("tool_call_accuracy", 0) >= 3
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error in evaluate_tool_call_accuracy: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Evaluation error: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "evaluate_task_adherence":
            if not EVALUATION_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Azure AI Evaluation SDK not available"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "FOUNDRY_PROJECT_ENDPOINT not configured"}],
                    isError=True
                )
            
            query = arguments.get("query")
            response = arguments.get("response")
            tool_calls = arguments.get("tool_calls", [])
            system_message = arguments.get("system_message", "")
            
            if not query or not response:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Both 'query' and 'response' are required"}],
                    isError=True
                )
            
            try:
                # Extract base endpoint (remove /api/projects/... path if present)
                base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
                model_config = {
                    "azure_endpoint": base_endpoint.rstrip('/'),
                    "azure_deployment": EVALUATOR_MODEL_DEPLOYMENT_NAME,
                    "api_version": "2024-10-21",
                }
                
                credential = DefaultAzureCredential()
                # Use is_reasoning_model=True for gpt-5.x evaluator model that supports max_completion_tokens
                evaluator = TaskAdherenceEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                
                # Build kwargs for evaluator
                eval_kwargs = {
                    "query": query,
                    "response": response,
                }
                if tool_calls:
                    eval_kwargs["tool_calls"] = tool_calls
                if system_message:
                    eval_kwargs["system_message"] = system_message
                
                result = evaluator(**eval_kwargs)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "evaluator": "TaskAdherenceEvaluator",
                            "query": query[:100] + "..." if len(query) > 100 else query,
                            "flagged": result.get("task_adherence", False),
                            "reasoning": result.get("task_adherence_reason", ""),
                            "passed": not result.get("task_adherence", True)  # flagged=True means failure
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error in evaluate_task_adherence: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Evaluation error: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "evaluate_groundedness":
            if not EVALUATION_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Azure AI Evaluation SDK not available. Install with: pip install azure-ai-evaluation"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "FOUNDRY_PROJECT_ENDPOINT not configured"}],
                    isError=True
                )
            
            query = arguments.get("query")
            response = arguments.get("response")
            context = arguments.get("context")
            
            if not query or not response or not context:
                return MCPToolResult(
                    content=[{"type": "text", "text": "'query', 'response', and 'context' are all required"}],
                    isError=True
                )
            
            try:
                base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
                model_config = {
                    "azure_endpoint": base_endpoint.rstrip('/'),
                    "azure_deployment": EVALUATOR_MODEL_DEPLOYMENT_NAME,
                    "api_version": "2024-10-21",
                }
                
                credential = DefaultAzureCredential()
                evaluator = GroundednessEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                
                result = evaluator(query=query, response=response, context=context)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "evaluator": "GroundednessEvaluator",
                            "query": query[:100] + "..." if len(query) > 100 else query,
                            "score": result.get("groundedness", 0),
                            "explanation": result.get("groundedness_reason", ""),
                            "threshold_recommendation": 3,
                            "passed": result.get("groundedness", 0) >= 3
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error in evaluate_groundedness: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Evaluation error: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "evaluate_relevance":
            if not EVALUATION_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Azure AI Evaluation SDK not available. Install with: pip install azure-ai-evaluation"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "FOUNDRY_PROJECT_ENDPOINT not configured"}],
                    isError=True
                )
            
            query = arguments.get("query")
            response = arguments.get("response")
            
            if not query or not response:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Both 'query' and 'response' are required"}],
                    isError=True
                )
            
            try:
                base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
                model_config = {
                    "azure_endpoint": base_endpoint.rstrip('/'),
                    "azure_deployment": EVALUATOR_MODEL_DEPLOYMENT_NAME,
                    "api_version": "2024-10-21",
                }
                
                credential = DefaultAzureCredential()
                evaluator = RelevanceEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                
                result = evaluator(query=query, response=response)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "evaluator": "RelevanceEvaluator",
                            "query": query[:100] + "..." if len(query) > 100 else query,
                            "score": result.get("relevance", 0),
                            "explanation": result.get("relevance_reason", ""),
                            "threshold_recommendation": 3,
                            "passed": result.get("relevance", 0) >= 3
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error in evaluate_relevance: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Evaluation error: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "run_agent_evaluation":
            if not EVALUATION_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Azure AI Evaluation SDK not available"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "FOUNDRY_PROJECT_ENDPOINT not configured"}],
                    isError=True
                )
            
            query = arguments.get("query")
            response = arguments.get("response")
            tool_calls = arguments.get("tool_calls", [])
            tool_definitions = arguments.get("tool_definitions")
            system_message = arguments.get("system_message", "")
            context = arguments.get("context", "")
            thresholds = arguments.get("thresholds", {
                "intent_resolution": 3,
                "tool_call_accuracy": 3,
                "task_adherence": 3,
                "groundedness": 3,
                "relevance": 3
            })
            
            if not query or not response:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Both 'query' and 'response' are required"}],
                    isError=True
                )
            
            # Use default tool definitions if not provided
            if not tool_definitions:
                tool_definitions = [
                    {"name": "next_best_action", "description": "Analyzes a task and generates an action plan.", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}
                ]
            
            try:
                # Extract base endpoint (remove /api/projects/... path if present)
                base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
                model_config = {
                    "azure_endpoint": base_endpoint.rstrip('/'),
                    "azure_deployment": EVALUATOR_MODEL_DEPLOYMENT_NAME,
                    "api_version": "2024-10-21",
                }
                
                credential = DefaultAzureCredential()
                results = {
                    "query": query[:200] + "..." if len(query) > 200 else query,
                    "response_preview": response[:200] + "..." if len(response) > 200 else response,
                    "evaluations": {},
                    "all_passed": True
                }
                
                # Run IntentResolutionEvaluator
                try:
                    intent_eval = IntentResolutionEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                    intent_result = intent_eval(query=query, response=response)
                    intent_score = intent_result.get("intent_resolution", 0)
                    results["evaluations"]["intent_resolution"] = {
                        "score": intent_score,
                        "threshold": thresholds.get("intent_resolution", 3),
                        "passed": intent_score >= thresholds.get("intent_resolution", 3),
                        "explanation": intent_result.get("intent_resolution_reason", "")
                    }
                    if not results["evaluations"]["intent_resolution"]["passed"]:
                        results["all_passed"] = False
                except Exception as e:
                    results["evaluations"]["intent_resolution"] = {"error": str(e), "passed": False}
                    results["all_passed"] = False
                
                # Run ToolCallAccuracyEvaluator (if tool_calls provided)
                if tool_calls:
                    try:
                        tool_eval = ToolCallAccuracyEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                        tool_result = tool_eval(query=query, tool_calls=tool_calls, tool_definitions=tool_definitions)
                        tool_score = tool_result.get("tool_call_accuracy", 0)
                        results["evaluations"]["tool_call_accuracy"] = {
                            "score": tool_score,
                            "threshold": thresholds.get("tool_call_accuracy", 3),
                            "passed": tool_score >= thresholds.get("tool_call_accuracy", 3),
                            "explanation": tool_result.get("tool_call_accuracy_reason", "")
                        }
                        if not results["evaluations"]["tool_call_accuracy"]["passed"]:
                            results["all_passed"] = False
                    except Exception as e:
                        results["evaluations"]["tool_call_accuracy"] = {"error": str(e), "passed": False}
                        results["all_passed"] = False
                else:
                    results["evaluations"]["tool_call_accuracy"] = {"skipped": True, "reason": "No tool_calls provided"}
                
                # Run TaskAdherenceEvaluator
                try:
                    task_eval = TaskAdherenceEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                    eval_kwargs = {"query": query, "response": response}
                    if tool_calls:
                        eval_kwargs["tool_calls"] = tool_calls
                    if system_message:
                        eval_kwargs["system_message"] = system_message
                    
                    task_result = task_eval(**eval_kwargs)
                    flagged = task_result.get("task_adherence", False)
                    results["evaluations"]["task_adherence"] = {
                        "flagged": flagged,
                        "passed": not flagged,  # flagged=True means failure
                        "reasoning": task_result.get("task_adherence_reason", "")
                    }
                    if not results["evaluations"]["task_adherence"]["passed"]:
                        results["all_passed"] = False
                except Exception as e:
                    results["evaluations"]["task_adherence"] = {"error": str(e), "passed": False}
                    results["all_passed"] = False
                
                # Run GroundednessEvaluator (if context provided)
                if context:
                    try:
                        groundedness_eval = GroundednessEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                        ground_result = groundedness_eval(query=query, response=response, context=context)
                        ground_score = ground_result.get("groundedness", 0)
                        if isinstance(ground_score, str):
                            try:
                                ground_score = int(float(ground_score))
                            except (ValueError, TypeError):
                                ground_score = 0
                        results["evaluations"]["groundedness"] = {
                            "score": ground_score,
                            "threshold": thresholds.get("groundedness", 3),
                            "passed": ground_score >= thresholds.get("groundedness", 3),
                            "explanation": ground_result.get("groundedness_reason", "")
                        }
                        if not results["evaluations"]["groundedness"]["passed"]:
                            results["all_passed"] = False
                    except Exception as e:
                        results["evaluations"]["groundedness"] = {"error": str(e), "passed": False}
                        results["all_passed"] = False
                else:
                    results["evaluations"]["groundedness"] = {"skipped": True, "reason": "No context provided"}
                
                # Run RelevanceEvaluator
                try:
                    relevance_eval = RelevanceEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                    rel_result = relevance_eval(query=query, response=response)
                    rel_score = rel_result.get("relevance", 0)
                    if isinstance(rel_score, str):
                        try:
                            rel_score = int(float(rel_score))
                        except (ValueError, TypeError):
                            rel_score = 0
                    results["evaluations"]["relevance"] = {
                        "score": rel_score,
                        "threshold": thresholds.get("relevance", 3),
                        "passed": rel_score >= thresholds.get("relevance", 3),
                        "explanation": rel_result.get("relevance_reason", "")
                    }
                    if not results["evaluations"]["relevance"]["passed"]:
                        results["all_passed"] = False
                except Exception as e:
                    results["evaluations"]["relevance"] = {"error": str(e), "passed": False}
                    results["all_passed"] = False
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps(results, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error in run_agent_evaluation: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Evaluation error: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "run_batch_evaluation":
            if not EVALUATION_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Azure AI Evaluation SDK not available"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "FOUNDRY_PROJECT_ENDPOINT not configured"}],
                    isError=True
                )
            
            evaluation_data = arguments.get("evaluation_data", [])
            thresholds = arguments.get("thresholds", {
                "intent_resolution": 3,
                "tool_call_accuracy": 3,
                "task_adherence": 3,
                "groundedness": 3,
                "relevance": 3
            })
            
            if not evaluation_data:
                return MCPToolResult(
                    content=[{"type": "text", "text": "'evaluation_data' array is required"}],
                    isError=True
                )
            
            try:
                # Extract base endpoint (remove /api/projects/... path if present)
                base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
                model_config = {
                    "azure_endpoint": base_endpoint.rstrip('/'),
                    "azure_deployment": EVALUATOR_MODEL_DEPLOYMENT_NAME,
                    "api_version": "2024-10-21",
                }
                
                credential = DefaultAzureCredential()
                
                # Initialize evaluators once
                # Use is_reasoning_model=True for gpt-5.x evaluator model that supports max_completion_tokens
                intent_eval = IntentResolutionEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                tool_eval = ToolCallAccuracyEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                task_eval = TaskAdherenceEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                groundedness_eval = GroundednessEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                relevance_eval = RelevanceEvaluator(model_config=model_config, credential=credential, is_reasoning_model=True)
                
                # Default tool definitions
                default_tool_defs = [
                    {"name": "next_best_action", "description": "Analyzes a task and generates an action plan.", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}
                ]
                
                all_results = []
                intent_scores = []
                tool_scores = []
                task_passes = []
                ground_scores = []
                relevance_scores = []
                
                for idx, item in enumerate(evaluation_data):
                    query = item.get("query", "")
                    response = item.get("response", "")
                    tool_calls = item.get("tool_calls", [])
                    system_message = item.get("system_message", "")
                    context = item.get("context", "")
                    
                    row_result = {
                        "index": idx,
                        "query_preview": query[:50] + "..." if len(query) > 50 else query,
                    }
                    
                    # Intent Resolution
                    try:
                        intent_result = intent_eval(query=query, response=response)
                        score = intent_result.get("intent_resolution", 0)
                        # Handle string scores from evaluator
                        if isinstance(score, str):
                            try:
                                score = int(float(score))
                            except (ValueError, TypeError):
                                score = 0
                        intent_scores.append(score)
                        row_result["intent_resolution"] = {
                            "score": score,
                            "passed": score >= thresholds.get("intent_resolution", 3)
                        }
                    except Exception as e:
                        row_result["intent_resolution"] = {"error": str(e)}
                    
                    # Tool Call Accuracy
                    if tool_calls:
                        try:
                            tool_result = tool_eval(query=query, tool_calls=tool_calls, tool_definitions=default_tool_defs)
                            score = tool_result.get("tool_call_accuracy", 0)
                            # Handle string scores from evaluator
                            if isinstance(score, str):
                                try:
                                    score = int(float(score))
                                except (ValueError, TypeError):
                                    score = 0
                            tool_scores.append(score)
                            row_result["tool_call_accuracy"] = {
                                "score": score,
                                "passed": score >= thresholds.get("tool_call_accuracy", 3)
                            }
                        except Exception as e:
                            row_result["tool_call_accuracy"] = {"error": str(e)}
                    
                    # Task Adherence
                    try:
                        eval_kwargs = {"query": query, "response": response}
                        if tool_calls:
                            eval_kwargs["tool_calls"] = tool_calls
                        if system_message:
                            eval_kwargs["system_message"] = system_message
                        
                        task_result = task_eval(**eval_kwargs)
                        flagged = task_result.get("task_adherence", False)
                        task_passes.append(not flagged)
                        row_result["task_adherence"] = {
                            "flagged": flagged,
                            "passed": not flagged
                        }
                    except Exception as e:
                        row_result["task_adherence"] = {"error": str(e)}
                    
                    # Groundedness (if context provided)
                    if context:
                        try:
                            ground_result = groundedness_eval(query=query, response=response, context=context)
                            score = ground_result.get("groundedness", 0)
                            if isinstance(score, str):
                                try:
                                    score = int(float(score))
                                except (ValueError, TypeError):
                                    score = 0
                            ground_scores.append(score)
                            row_result["groundedness"] = {
                                "score": score,
                                "passed": score >= thresholds.get("groundedness", 3)
                            }
                        except Exception as e:
                            row_result["groundedness"] = {"error": str(e)}
                    
                    # Relevance
                    try:
                        rel_result = relevance_eval(query=query, response=response)
                        score = rel_result.get("relevance", 0)
                        if isinstance(score, str):
                            try:
                                score = int(float(score))
                            except (ValueError, TypeError):
                                score = 0
                        relevance_scores.append(score)
                        row_result["relevance"] = {
                            "score": score,
                            "passed": score >= thresholds.get("relevance", 3)
                        }
                    except Exception as e:
                        row_result["relevance"] = {"error": str(e)}
                    
                    all_results.append(row_result)
                
                # Calculate aggregate metrics
                summary = {
                    "total_evaluated": len(evaluation_data),
                    "metrics": {}
                }
                
                if intent_scores:
                    summary["metrics"]["intent_resolution"] = {
                        "average_score": round(sum(intent_scores) / len(intent_scores), 2),
                        "pass_rate": round(sum(1 for s in intent_scores if s >= thresholds.get("intent_resolution", 3)) / len(intent_scores) * 100, 1),
                        "min": min(intent_scores),
                        "max": max(intent_scores)
                    }
                
                if tool_scores:
                    summary["metrics"]["tool_call_accuracy"] = {
                        "average_score": round(sum(tool_scores) / len(tool_scores), 2),
                        "pass_rate": round(sum(1 for s in tool_scores if s >= thresholds.get("tool_call_accuracy", 3)) / len(tool_scores) * 100, 1),
                        "min": min(tool_scores),
                        "max": max(tool_scores)
                    }
                
                if task_passes:
                    summary["metrics"]["task_adherence"] = {
                        "pass_rate": round(sum(task_passes) / len(task_passes) * 100, 1),
                        "passed_count": sum(task_passes),
                        "failed_count": len(task_passes) - sum(task_passes)
                    }
                
                if ground_scores:
                    summary["metrics"]["groundedness"] = {
                        "average_score": round(sum(ground_scores) / len(ground_scores), 2),
                        "pass_rate": round(sum(1 for s in ground_scores if s >= thresholds.get("groundedness", 3)) / len(ground_scores) * 100, 1),
                        "min": min(ground_scores),
                        "max": max(ground_scores)
                    }
                
                if relevance_scores:
                    summary["metrics"]["relevance"] = {
                        "average_score": round(sum(relevance_scores) / len(relevance_scores), 2),
                        "pass_rate": round(sum(1 for s in relevance_scores if s >= thresholds.get("relevance", 3)) / len(relevance_scores) * 100, 1),
                        "min": min(relevance_scores),
                        "max": max(relevance_scores)
                    }
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "summary": summary,
                            "thresholds": thresholds,
                            "per_row_results": all_results
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error in run_batch_evaluation: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Batch evaluation error: {str(e)}"}],
                    isError=True
                )
        
        # =========================================
        # Fabric Agent Orchestrator tools
        # =========================================
        elif tool_name == "fabric_agent_query":
            if not FABRIC_AGENTS_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Fabric Agent Orchestrator not available"}],
                    isError=True,
                )
            query = arguments.get("query", "")
            agent_kwargs = {}
            for key in ("agent_type", "lakehouse_id", "warehouse_id", "pipeline_id", "dataset_id"):
                if arguments.get(key):
                    agent_kwargs[key] = arguments[key]
            if arguments.get("sql_query"):
                agent_kwargs["query"] = arguments["sql_query"]
            orchestrator = get_fabric_orchestrator()
            resp = await orchestrator.handle(query, **agent_kwargs)
            return MCPToolResult(content=[{"type": "text", "text": resp.to_json()}])

        elif tool_name == "fabric_agent_cross_domain":
            if not FABRIC_AGENTS_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Fabric Agent Orchestrator not available"}],
                    isError=True,
                )
            query = arguments.get("query", "")
            kwargs = {k: arguments[k] for k in ("lakehouse_id", "warehouse_id", "pipeline_id", "dataset_id") if arguments.get(k)}
            orchestrator = get_fabric_orchestrator()
            resp = await orchestrator.cross_domain_query(query, **kwargs)
            return MCPToolResult(content=[{"type": "text", "text": resp.to_json()}])

        elif tool_name == "fabric_agent_list_all":
            if not FABRIC_AGENTS_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Fabric Agent Orchestrator not available"}],
                    isError=True,
                )
            orchestrator = get_fabric_orchestrator()
            resp = await orchestrator.list_all_resources()
            return MCPToolResult(content=[{"type": "text", "text": resp.to_json()}])

        # =========================================
        # OneLake file operations tools
        # =========================================
        elif tool_name == "onelake_list_files":
            if not ONELAKE_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "OneLake operations not available"}],
                    isError=True,
                )
            result = onelake_list_files_tool(
                lakehouse_id=arguments.get("lakehouse_id", ""),
                path=arguments.get("path", ""),
                section=arguments.get("section", "Files"),
                recursive=arguments.get("recursive", False),
            )
            return MCPToolResult(content=[{"type": "text", "text": result}])

        elif tool_name == "onelake_read_file":
            if not ONELAKE_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "OneLake operations not available"}],
                    isError=True,
                )
            result = onelake_read_file_tool(
                lakehouse_id=arguments.get("lakehouse_id", ""),
                path=arguments.get("path", ""),
                section=arguments.get("section", "Files"),
            )
            return MCPToolResult(content=[{"type": "text", "text": result}])

        elif tool_name == "onelake_write_file":
            if not ONELAKE_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "OneLake operations not available"}],
                    isError=True,
                )
            result = onelake_write_file_tool(
                lakehouse_id=arguments.get("lakehouse_id", ""),
                path=arguments.get("path", ""),
                content=arguments.get("content", ""),
                section=arguments.get("section", "Files"),
                overwrite=arguments.get("overwrite", True),
            )
            return MCPToolResult(content=[{"type": "text", "text": result}])

        elif tool_name == "onelake_delete_file":
            if not ONELAKE_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "OneLake operations not available"}],
                    isError=True,
                )
            result = onelake_delete_file_tool(
                lakehouse_id=arguments.get("lakehouse_id", ""),
                path=arguments.get("path", ""),
                section=arguments.get("section", "Files"),
                recursive=arguments.get("recursive", False),
            )
            return MCPToolResult(content=[{"type": "text", "text": result}])

        elif tool_name == "onelake_get_file_properties":
            if not ONELAKE_AVAILABLE:
                return MCPToolResult(
                    content=[{"type": "text", "text": "OneLake operations not available"}],
                    isError=True,
                )
            result = onelake_get_file_properties_tool(
                lakehouse_id=arguments.get("lakehouse_id", ""),
                path=arguments.get("path", ""),
                section=arguments.get("section", "Files"),
            )
            return MCPToolResult(content=[{"type": "text", "text": result}])

        else:
            return MCPToolResult(
                content=[{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                isError=True
            )
    
    except Exception as e:
        logger.error(f"Error executing tool {tool_name}: {e}")
        return MCPToolResult(
            content=[{"type": "text", "text": f"Error: {str(e)}"}],
            isError=True
        )


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


@app.get("/runtime/webhooks/mcp/sse")
async def mcp_sse_endpoint(request: Request):
    """
    SSE endpoint for MCP protocol
    Establishes a long-lived connection for server-sent events
    """
    session_id = str(uuid.uuid4())
    logger.info(f"New SSE session established: {session_id}")
    
    # Store session
    sessions[session_id] = {
        "created_at": datetime.utcnow().isoformat(),
        "message_queue": asyncio.Queue()
    }
    
    async def event_generator():
        try:
            # Send initial connection event with message endpoint
            message_url = f"message?sessionId={session_id}"
            yield f"data: {message_url}\n\n"
            
            # Keep connection alive and send any queued messages
            while True:
                if session_id not in sessions:
                    break
                
                try:
                    # Wait for messages with timeout
                    message = await asyncio.wait_for(
                        sessions[session_id]["message_queue"].get(),
                        timeout=30.0
                    )
                    yield f"data: {json.dumps(message)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield ": keepalive\n\n"
                    
        except asyncio.CancelledError:
            logger.info(f"SSE connection cancelled for session {session_id}")
        finally:
            # Cleanup session
            if session_id in sessions:
                del sessions[session_id]
            logger.info(f"SSE session closed: {session_id}")
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.post("/runtime/webhooks/mcp/message")
async def mcp_message_endpoint(request: Request):
    """
    Message endpoint for MCP protocol
    Handles JSON-RPC 2.0 requests
    """
    try:
        body = await request.json()
        logger.info(f"Received MCP message: {json.dumps(body)[:200]}")
        
        jsonrpc_version = body.get("jsonrpc")
        method = body.get("method")
        params = body.get("params", {})
        request_id = body.get("id")
        
        if jsonrpc_version != "2.0":
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32600, "message": "Invalid Request"},
                    "id": request_id
                }
            )
        
        # Handle initialize
        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "mcp-agents",
                        "version": "1.0.0"
                    }
                },
                "id": request_id
            }
            return JSONResponse(content=response)
        
        # Handle tools/list
        elif method == "tools/list":
            tools_list = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.inputSchema
                }
                for tool in TOOLS
            ]
            
            response = {
                "jsonrpc": "2.0",
                "result": {
                    "tools": tools_list
                },
                "id": request_id
            }
            return JSONResponse(content=response)
        
        # Handle tools/call
        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            
            # Execute the tool
            result = await execute_tool(tool_name, arguments)
            
            response = {
                "jsonrpc": "2.0",
                "result": asdict(result),
                "id": request_id
            }
            return JSONResponse(content=response)
        
        else:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                    "id": request_id
                }
            )
    
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": f"Internal error: {str(e)}"},
                "id": body.get("id") if 'body' in locals() else None
            }
        )


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": "MCP Server",
        "version": "1.0.0",
        "endpoints": {
            "sse": "/runtime/webhooks/mcp/sse",
            "message": "/runtime/webhooks/mcp/message",
            "health": "/health",
            "agent_chat": "/agent/chat"
        },
        "agent_enabled": mcp_ai_agent is not None
    }


@app.on_event("startup")
async def startup_event():
    """Initialize the AI agent and memory providers on startup."""
    global mcp_ai_agent
    
    # Initialize AI Agent
    mcp_ai_agent = create_mcp_agent()
    if mcp_ai_agent:
        logger.info("AI Agent initialized successfully on startup")
    else:
        logger.warning("AI Agent not initialized - check FOUNDRY_PROJECT_ENDPOINT configuration")
    
    # Configure embedding function for memory provider
    if short_term_memory and FOUNDRY_PROJECT_ENDPOINT:
        short_term_memory.set_embedding_function(get_embedding)
        logger.info("Memory provider embedding function configured")
    
    # Log memory provider status
    if composite_memory:
        health = await composite_memory.health_check()
        for provider, is_healthy in health.items():
            status = "healthy" if is_healthy else "unhealthy"
            logger.info(f"Memory provider '{provider}': {status}")


@app.post("/agent/chat")
async def agent_chat(request: Request):
    """
    Chat endpoint for Microsoft Agent Framework.
    Processes user messages using the AI agent with tool capabilities.
    """
    if mcp_ai_agent is None:
        return JSONResponse(
            status_code=503,
            content={
                "error": "AI Agent not available",
                "message": "Configure FOUNDRY_PROJECT_ENDPOINT and install agent-framework packages to enable AI Agent"
            }
        )
    
    try:
        body = await request.json()
        user_message = body.get("message", "")
        conversation_history = body.get("history", [])
        
        if not user_message:
            return JSONResponse(
                status_code=400,
                content={"error": "No message provided"}
            )
        
        # Build messages list for the agent
        messages = []
        
        # Add conversation history
        for hist_msg in conversation_history:
            messages.append({
                "role": hist_msg.get("role", "user"),
                "content": hist_msg.get("content", "")
            })
        
        # Add current user message
        messages.append({"role": "user", "content": user_message})
        
        # Run the agent
        response = await mcp_ai_agent.run(messages)
        
        # Extract assistant response
        assistant_responses = []
        if hasattr(response, 'messages'):
            for msg in response.messages:
                if hasattr(msg, 'role') and str(msg.role).lower() == 'assistant':
                    if hasattr(msg, 'contents'):
                        for content in msg.contents:
                            if hasattr(content, 'text'):
                                assistant_responses.append(content.text)
                    elif hasattr(msg, 'content'):
                        assistant_responses.append(str(msg.content))
        
        return JSONResponse(content={
            "response": "\n".join(assistant_responses) if assistant_responses else "No response generated",
            "message_id": str(uuid.uuid4())
        })
        
    except Exception as e:
        logger.error(f"Error in agent chat: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Agent error: {str(e)}"}
        )


@app.post("/agent/chat/stream")
async def agent_chat_stream(request: Request):
    """
    Streaming chat endpoint for Microsoft Agent Framework.
    Returns responses as Server-Sent Events for real-time streaming.
    """
    if mcp_ai_agent is None:
        return JSONResponse(
            status_code=503,
            content={
                "error": "AI Agent not available",
                "message": "Configure FOUNDRY_PROJECT_ENDPOINT and install agent-framework packages to enable AI Agent"
            }
        )
    
    try:
        body = await request.json()
        user_message = body.get("message", "")
        
        if not user_message:
            return JSONResponse(
                status_code=400,
                content={"error": "No message provided"}
            )
        
        messages = [{"role": "user", "content": user_message}]
        
        async def generate_stream():
            try:
                async for event in mcp_ai_agent.run_stream(messages):
                    if hasattr(event, 'data') and hasattr(event.data, 'contents'):
                        for content in event.data.contents:
                            if hasattr(content, 'text'):
                                yield f"data: {json.dumps({'text': content.text})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
            except Exception as e:
                logger.error(f"Streaming error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        
        return StreamingResponse(
            generate_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive"
            }
        )
        
    except Exception as e:
        logger.error(f"Error in agent chat stream: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Agent error: {str(e)}"}
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

