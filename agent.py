import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import agent_core as core
import agent_ui as ui
import tool_bridge as tb
from llm_client import DEFAULT_BASE_URL, ChatClient

HERE = Path(__file__).parent
TOOL_TIMEOUT_SECONDS = 60.0


@asynccontextmanager
async def open_session(extra_env=None):
    """Start server.py as a child process and connect to it. The model key is not passed on."""
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(HERE / "server.py")],
        cwd=str(HERE),
        env=ui.child_env(os.environ, extra_env),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def load_specs(session) -> list:
    listed = await session.list_tools()
    return [tb.spec_from_mcp(t) for t in listed.tools]


def make_call_tool(session, announce=None, timeout: float = TOOL_TIMEOUT_SECONDS):
    async def call_tool(name, arguments):
        if announce:
            announce(name, arguments)
        result = await asyncio.wait_for(session.call_tool(name, arguments), timeout)
        return tb.result_to_text(result)
    return call_tool


def announce(name, arguments) -> None:
    preview = ui.safe_text(json.dumps(arguments, ensure_ascii=False, default=str))
    print(f"  -> {ui.safe_text(name)} {preview[:110]}", file=sys.stderr)


async def sleep_with_notice(seconds: float) -> None:
    print(f"  [waiting {seconds:.0f}s for the rate-limit window]", file=sys.stderr)
    await asyncio.sleep(seconds)


def make_llm(model_override=None) -> ChatClient:
    load_dotenv(HERE / ".env.agent")
    key = os.getenv("GROQ_API_KEY", "").strip()
    model = (model_override or os.getenv("LLM_MODEL", "")).strip()
    if not key:
        raise SystemExit("GROQ_API_KEY is missing. Put it in .env.agent.")
    if not model:
        raise SystemExit("LLM_MODEL is missing. Put it in .env.agent or pass --model.")
    try:
        max_tokens = int(os.getenv("LLM_MAX_COMPLETION_TOKENS", "1200") or 1200)
    except ValueError:
        raise SystemExit("LLM_MAX_COMPLETION_TOKENS must be a whole number.") from None
    return ChatClient(
        key, model, os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL).strip(),
        reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "").strip(),
        max_completion_tokens=max_tokens,
    )


def print_result(res, as_json: bool) -> None:
    if as_json:
        print(json.dumps(ui.result_to_dict(res), indent=2, default=str))
        return
    print("\n" + (ui.safe_text(res.answer) if res.answer else "(no answer)"))
    print(ui.format_run_summary(res))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Triage agent: a Groq model using your MCP server. "
                    "Read-only unless you switch proposals on.")
    p.add_argument("request", nargs="?", help="One-shot request. Leave out for chat mode.")
    p.add_argument("--repo", help="Default repo (owner/name). Also read from DEFAULT_REPO in .env.agent.")
    p.add_argument("--allow-proposals", action="store_true",
                   help="Offer propose_comment and propose_assign (a human still approves).")
    p.add_argument("--model", help="Override LLM_MODEL for this run.")
    p.add_argument("--json", action="store_true", dest="as_json", help="Print the run as JSON.")
    p.add_argument("--max-steps", type=int, default=8)
    p.add_argument("--token-budget", type=int, default=30000)
    return p


async def amain(args) -> int:
    llm = make_llm(args.model)
    default_repo = (args.repo or os.getenv("DEFAULT_REPO") or "").strip() or None
    proposals = bool(args.allow_proposals)
    try:
        ui.build_system_prompt(default_repo, proposals)        # validates the repo name early
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    limits = core.Limits(max_steps=max(1, min(args.max_steps, 20)),
                         max_total_tokens=max(2000, min(args.token_budget, 100000)))
    history: list = []

    async with open_session() as session:
        specs = await load_specs(session)
        call_tool = make_call_tool(session, announce=announce)

        async def turn(text: str, allow: bool):
            mode = tb.MODE_PROPOSE if allow else tb.MODE_READ
            return await core.run_agent(
                text, llm=llm, call_tool=call_tool,
                exposed=tb.select_tools(specs, mode),
                system_prompt=ui.build_system_prompt(default_repo, allow),
                limits=limits, prior_turns=history, sleep=sleep_with_notice)

        if args.request:
            res = await turn(args.request, proposals)
            print_result(res, args.as_json)
            return 0 if res.stop_reason == "answered" else 1

        print(f"Model: {llm.model}   Default repo: {default_repo or 'none'}")
        print("Mode: " + ("proposals ON" if proposals else "read-only") + "   (type /help for commands)")
        while True:
            label = "proposals" if proposals else "read-only"
            try:
                line = (await asyncio.to_thread(input, f"you[{label}]> ")).strip()
            except EOFError:
                break
            if not line:
                continue
            command = ui.parse_command(line)
            if command:
                name = command[0]
                if name in ("quit", "exit"):
                    break
                if name == "allow":
                    proposals = True
                    print("Proposals are ON. You still approve every action yourself.")
                elif name == "readonly":
                    proposals = False
                    print("Read-only mode.")
                elif name == "clear":
                    history.clear()
                    print("Conversation cleared.")
                elif name == "pending":
                    print("Review pending proposals in another terminal:\n"
                          "  uv run python approve.py list")
                else:
                    print(ui.HELP_TEXT)
                continue
            res = await turn(line, proposals)
            print_result(res, False)
            if res.stop_reason == "answered":
                history.append((line, res.answer))
        return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())