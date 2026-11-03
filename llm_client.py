from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generator
import os
import sys

_DEBUG = os.getenv("AGENT_TREE_DEBUG", "").strip() in ("1", "true", "yes")


def _dbg(tag: str, msg: str):
    if _DEBUG:
        sys.stderr.write(f"\033[90m[DBG:{tag}] {msg}\033[0m\n")
        sys.stderr.flush()


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: list) -> int:
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        if m.get("tool_calls"):
            for tc in m["tool_calls"]:
                args = tc.get("function", {}).get("arguments", "")
                total += estimate_tokens(str(args))
    return total


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCall] | None
    usage: dict
    finish_reason: str


@dataclass
class Chunk:
    type: str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    name: str | None = None
    usage: dict | None = None
    finish_reason: str = ""


class LLMClient(ABC):
    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    def ask(self, prompt: str, system: str | None = None) -> tuple[str, dict]: ...

    @abstractmethod
    def ask_stream(self, prompt: str, system: str | None = None) -> Generator[str, None, None]: ...

    @abstractmethod
    def chat(self, messages: list, tools: list | None = None) -> ChatResponse: ...

    @abstractmethod
    def chat_stream(self, messages: list, tools: list | None = None) -> Generator[Chunk, None, None]: ...


def create_llm_client(model_spec: str) -> LLMClient:
    if "/" not in model_spec:
        raise ValueError(f"Model spec must be 'provider/model', got: {model_spec!r}")
    provider, model_id = model_spec.split("/", 1)

    if provider == "opencode":
        from bridge_client import OpenCodeBridgeClient
        return OpenCodeBridgeClient(provider, model_id)

    if provider == "github-copilot":
        env_prefix = provider.upper().replace("-", "_")
        api_key = os.getenv(f"{env_prefix}_API_KEY") or os.getenv("LLM_API_KEY", "")
        if api_key:
            from direct_client import DirectOpenAIClient
            return DirectOpenAIClient(provider, model_id)
        from bridge_client import OpenCodeBridgeClient
        return OpenCodeBridgeClient(provider, model_id)

    from direct_client import DirectOpenAIClient
    return DirectOpenAIClient(provider, model_id)
