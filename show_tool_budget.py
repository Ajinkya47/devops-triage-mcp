import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import tool_bridge as tb

HERE = Path(__file__).parent


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable, args=[str(HERE / "server.py")], cwd=str(HERE))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()

    available = [tb.spec_from_mcp(t) for t in listed.tools]
    print(f"Server offers {len(available)} tools\n")
    for mode in (tb.MODE_READ, tb.MODE_PROPOSE):
        exposed = tb.select_tools(available, mode)
        budget = tb.tool_budget(tb.to_openai_tools(exposed))
        print(f"Mode: {mode} ({len(exposed)} tools, about {budget['total']} tokens)")
        for name, tokens in sorted(budget["per_tool"].items(), key=lambda kv: -kv[1]):
            print(f"  {name:18} about {tokens} tokens")
        print()
    hidden = sorted(s.name for s in available if s.name not in tb.READ_TOOLS | tb.PROPOSE_TOOLS)
    print("Never exposed to the model:", hidden or "none")


asyncio.run(main())