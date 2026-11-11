from mcp_server_polymarket.integrations.langgraph import create_polymarket_agent

# Built once at import so every run shares one HTTP client and write journal. LangGraph Platform
# supplies the checkpointer, which order approvals (interrupts) need.
graph = create_polymarket_agent("anthropic:claude-sonnet-5-5", checkpointer=None)
