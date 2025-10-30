from agno.workflow import Workflow, Step
from agno.tools.mcp import MCPTools
from agno_fastomop.agents.semantic import create_semantic_agent
from agno_fastomop.agents.database import create_database_agent
from agno_fastomop.config import config
from agno_fastomop.observability.trace_context import write_trace_context_otel, clear_trace_context
from agno.db.sqlite import SqliteDb
from langfuse import observe, Langfuse, get_client
import asyncio
import os
import json

# Module-level storage for workflow (created once, reused)
_omop_workflow = None
_mcp_tools = None
_init_lock = asyncio.Lock()


async def initialize_workflow():
    """
    Initialize Workflow with semantic -> database pipeline.
    FastOMOP approach: ONE shared MCP connection for both agents.
    """
    global _omop_workflow, _mcp_tools

    async with _init_lock:
        if _omop_workflow is not None:
            return _omop_workflow

        # Create ONE MCP connection (shared by both agents to avoid DuckDB lock)
        # Pass Langfuse credentials to OMCP subprocess for trace propagation
        omcp_config = config["omcp"]
        _mcp_tools = MCPTools(
            transport=omcp_config["transport"],
            command=omcp_config["command"],
            env={
                "DB_PATH": os.getenv("DB_PATH", ""),
                "LANGFUSE_PUBLIC_KEY": os.getenv("LANGFUSE_PUBLIC_KEY", ""),
                "LANGFUSE_SECRET_KEY": os.getenv("LANGFUSE_SECRET_KEY", ""),
                "LANGFUSE_HOST": os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            }
        )

        # Manually connect MCP once
        await _mcp_tools._connect()

        # Create shared database for conversation history and memory
        db = SqliteDb(db_file="db_agent.db")

        # Create agents with shared MCP - both query the database
        semantic_agent = create_semantic_agent(_mcp_tools)  # Queries concept table
        database_agent = create_database_agent(_mcp_tools)  # Generates & executes SQL

        # Create linear workflow (supports structured output passing)
        _omop_workflow = Workflow(
            name="OMOP Clinical Query Workflow",
            db=db,  # Shared database enables conversation history across workflow runs
            steps=[
                Step(
                    name="Semantic Extraction",
                    agent=semantic_agent,
                    description="Extract clinical concepts and map to OMOP codes",
                ),
                Step(
                    name="SQL Generation and Execution",
                    agent=database_agent,
                    description="Generate SQL from semantic context and execute",
                ),
            ],
        )

        return _omop_workflow


def extract_final_query_from_step(step_response) -> str:
    """
    Extract final query from a single step's response (database agent).

    Args:
        step_response: Agent's RunResponse object

    Returns:
        str: The final successful query, or None if not found
    """
    final_query = None

    try:
        # Check if step_response has messages
        messages = []
        if hasattr(step_response, 'messages') and step_response.messages:
            messages = step_response.messages
        elif hasattr(step_response, 'run_response') and hasattr(step_response.run_response, 'messages'):
            messages = step_response.run_response.messages

        # Iterate through messages to find tool calls
        for message in messages:
            if hasattr(message, 'tool_calls') and message.tool_calls:
                for tool_call in message.tool_calls:
                    # Extract tool name
                    tool_name = None
                    if hasattr(tool_call, 'function') and tool_call.function:
                        if hasattr(tool_call.function, 'name'):
                            tool_name = tool_call.function.name
                        elif hasattr(tool_call, 'tool_name'):
                            tool_name = tool_call.tool_name

                    # Check if this is a select_query call
                    if tool_name == 'select_query':
                        # Check if it was successful (no error)
                        # tool_call_error == False or missing means success
                        has_error = getattr(tool_call, 'tool_call_error', False)

                        if not has_error:
                            # Successful call - extract the query from arguments
                            args = None
                            if hasattr(tool_call, 'function') and tool_call.function:
                                if hasattr(tool_call.function, 'arguments'):
                                    try:
                                        args_raw = tool_call.function.arguments
                                        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                                    except:
                                        pass
                            elif hasattr(tool_call, 'tool_args'):
                                args = tool_call.tool_args

                            if args and 'query' in args:
                                final_query = args['query']

    except Exception as e:
        print(f"Warning: Could not extract final query from step: {e}")

    return final_query


def extract_final_query(workflow_response) -> str:
    """
    Extract the final successful query from tool execution history.
    Looks for the last select_query tool call where isError == False.

    Args:
        workflow_response: The workflow RunOutput containing execution history

    Returns:
        str: The final successful query, or None if not found
    """
    final_query = None

    try:
        # Method 1: Check step_executor_runs (matches Langfuse output structure)
        if hasattr(workflow_response, 'step_executor_runs') and workflow_response.step_executor_runs:
            # Iterate through step executor runs in reverse (most recent first)
            for step_run in reversed(workflow_response.step_executor_runs):
                if hasattr(step_run, 'tools') and step_run.tools:
                    # Look through tools in reverse order (last tool call first)
                    for tool in reversed(step_run.tools):
                        # Check if this is a Select_Query tool
                        tool_name = getattr(tool, 'tool_name', None)
                        if tool_name == 'Select_Query':
                            # Check if it succeeded (isError == False)
                            result = getattr(tool, 'result', None)
                            if result:
                                is_error = False
                                # Check for isError in result
                                if hasattr(result, 'isError'):
                                    is_error = result.isError
                                elif isinstance(result, dict) and 'isError' in result:
                                    is_error = result['isError']

                                if not is_error:
                                    # Successful query - extract it
                                    tool_args = getattr(tool, 'tool_args', None)
                                    if tool_args:
                                        if isinstance(tool_args, dict) and 'query' in tool_args:
                                            final_query = tool_args['query']
                                            break
                                        elif hasattr(tool_args, 'query'):
                                            final_query = tool_args.query
                                            break

                if final_query:
                    break

        # Method 2: Check if workflow has step_responses
        if not final_query and hasattr(workflow_response, 'step_responses') and workflow_response.step_responses:
            # Database agent is the second step (index 1)
            for step_response in reversed(workflow_response.step_responses):
                query = extract_final_query_from_step(step_response)
                if query:
                    final_query = query
                    break

        # Method 3: Check direct messages in workflow response
        if not final_query:
            final_query = extract_final_query_from_step(workflow_response)

        # Method 4: Access through run_response
        if not final_query and hasattr(workflow_response, 'run_response'):
            final_query = extract_final_query_from_step(workflow_response.run_response)

    except Exception as e:
        import traceback
        print(f"Warning: Could not extract final query: {e}")
        print(f"Traceback: {traceback.format_exc()}")

    return final_query


@observe() #Complete langfuse tracing
async def run_omop_query(user_query: str, session_id: str = None, user_id: str = None) -> str:
    """
    Run OMOP clinical query via Workflow
    Initializes on first call, reuses for subsequent queries

    Args:
        user_query: The clinical query to process
        session_id: Session identifier for conversation history
        user_id: User identifier for personalized memories
    """
    # Inject current OpenTelemetry trace context for OMCP subprocess
    # This uses W3C Trace Context format (traceparent/tracestate)
    try:
        write_trace_context_otel(session_id=session_id)
    except Exception as e:
        # Non-critical: if trace context extraction fails, continue without it
        print(f"Warning: Could not inject OpenTelemetry trace context: {e}")

    workflow = await initialize_workflow()
    response = await workflow.arun(user_query, session_id=session_id, user_id=user_id)

    # Extract final successful query from tool execution history
    try:
        final_query = extract_final_query(response)

        if final_query:
            # Add final_query to Langfuse trace output (V3 API)
            # Preserve existing output and add final_query field
            langfuse = get_client()

            # Get existing output from response
            existing_output = {}
            if hasattr(response, 'to_dict'):
                existing_output = response.to_dict()
            elif hasattr(response, '__dict__'):
                existing_output = {k: v for k, v in response.__dict__.items() if not k.startswith('_')}

            # Add final_query to existing output
            existing_output['final_query'] = final_query

            langfuse.update_current_trace(
                output=existing_output
            )
        else:
            print("WARNING: No final query found in response")

    except Exception as e:
        import traceback
        print(f"ERROR: Could not update trace with final query: {e}")
        print(f"ERROR traceback: {traceback.format_exc()}")

    return response


async def cleanup_workflow():
    """
    Cleanup resources (call on shutdown)
    Closes MCP connection
    """
    global _omop_workflow

    if _omop_workflow is not None and hasattr(_omop_workflow, 'steps'):
        for step in _omop_workflow.steps:
            if hasattr(step.agent, 'tools'):
                for tool in step.agent.tools:
                    if hasattr(tool, 'close'):
                        await tool.close()


    langfuse = Langfuse()
    langfuse.flush()