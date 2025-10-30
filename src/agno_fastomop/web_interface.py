"""
FastOMOP Web Interface

Exposes the FastOMOP workflow through AgentOS with built-in web UI.

Usage:
    uv run python -m agno_fastomop.web_interface

Then visit http://localhost:7777

Note: Auto-reload is disabled due to DuckDB file locking constraints.
"""

import asyncio
from contextlib import asynccontextmanager
from agno.os import AgentOS
from agno_fastomop.workflows.omop_workflow import initialize_workflow, cleanup_workflow
import uvicorn

# Global storage
_workflow = None
_agents = None
_agent_os = None
_app = None


@asynccontextmanager
async def app_lifespan(app):
    """Handle graceful shutdown - cleanup MCP connections"""
    # Startup: nothing to do (workflow already initialized)
    yield
    # Shutdown: cleanup MCP subprocess to release DuckDB lock
    print("Shutting down FastOMOP - cleaning up MCP connections...")
    await cleanup_workflow()
    print("Cleanup complete")


async def initialize():
    """Initialize workflow and create AgentOS - all in the same event loop"""
    global _workflow, _agents, _agent_os, _app

    print("Initializing FastOMOP workflow...")
    _workflow = await initialize_workflow()
    _agents = [step.agent for step in _workflow.steps]
    print("✓ Workflow initialized")

    # Create AgentOS with pre-initialized workflow
    _agent_os = AgentOS(
        name="FastOMOP",
        description="Natural language interface for OMOP clinical databases",
        workflows=[_workflow],
        agents=_agents,
        lifespan=app_lifespan,
    )

    _app = _agent_os.get_app()
    print("✓ AgentOS created")


async def main():
    """Main async entry point"""
    # Initialize everything in this event loop
    await initialize()

    # Configure and run uvicorn server
    config = uvicorn.Config(
        app=_app,
        host="localhost",
        port=7777,
        reload=False,
        log_level="info",
    )
    server = uvicorn.Server(config)

    # Run server in the same event loop
    await server.serve()


if __name__ == "__main__":
    """Visit http://localhost:7777 to interact with FastOMOP"""
    asyncio.run(main())
