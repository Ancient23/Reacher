"""Phase-0 smoke test: prove the Claude Agent SDK drives Claude Code on the Max plan
(no API key). Run AFTER `claude setup-token` + setting CLAUDE_CODE_OAUTH_TOKEN, or while
logged into Claude Code interactively (the SDK reuses that session).

Run:  C:\\Source\\reacher\\.phase0\\.venv\\Scripts\\python.exe C:\\Source\\reacher\\.phase0\\claude_test.py
"""
import anyio
from claude_agent_sdk import query, ClaudeAgentOptions


async def main() -> None:
    opts = ClaudeAgentOptions(max_turns=1)
    print("Querying Claude Code via the Agent SDK (Max-plan auth)...\n")
    async for message in query(
        prompt="Reply with exactly this and nothing else: fleet supervisor online",
        options=opts,
    ):
        print(repr(message))


if __name__ == "__main__":
    anyio.run(main)
