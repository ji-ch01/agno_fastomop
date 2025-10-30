from agno.agent import Agent
from agno.tools.mcp import MCPTools
from agno_fastomop.observability.tracer import get_langfuse_client
from agno_fastomop.agents.factory import create_model
from agno_fastomop.config import get_agent_config, config
from agno_fastomop.agents.semantic import create_semantic_agent
from agno_fastomop.agents.database import create_database_agent
from agno.db.sqlite import SqliteDb
from pathlib import Path
import os


async def create_supervisor_agent(mcp_tools: MCPTools) -> Agent:
    """
    Create supervisor agent for orchestrating the semantic and database agents

    Args:
        mcp_tools: Shared MCP connection (to avoid DuckDB lock conflicts)
    """

    agent_config = get_agent_config("orchestrator")
    model = create_model(agent_config)

    # Shared database for conversation history and memory
    db = SqliteDb(db_file="db_agent.db")

    # Fetch prompt from Langfuse
    try:
        langfuse = get_langfuse_client()
        prompt = langfuse.get_prompt("supervisor", label="dev")
        system_prompt = prompt.prompt
        print(f"✓ Loaded supervisor prompt from Langfuse (version: {prompt.version})")
    except Exception as e:
        print(f"Warning: Failed to load prompt from Langfuse: {e}")
        print("Falling back to local prompt file")
        prompt_path = Path(__file__).parent.parent / "prompts" / "supervisor.txt"
        with open(prompt_path, 'r') as f:
            system_prompt = f.read()

    # Create sub-agents with shared MCP connection
    semantic_agent = create_semantic_agent(mcp_tools)
    database_agent = create_database_agent(mcp_tools)

    agent = Agent(
        name=agent_config["name"],
        model=model,
        instructions=system_prompt,
        tools=[semantic_agent, database_agent],  # Agents can be passed directly as tools
        knowledge=None,
        reasoning=agent_config.get("reasoning", True),
        markdown=agent_config.get("markdown", True),
    )
    return agent