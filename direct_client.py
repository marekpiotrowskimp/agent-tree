import httpx
import json
import os

from llm_client import LLMClient, ChatResponse, Chunk, ToolCall, _dbg, estimate_tokens, estimate_messages_tokens
from log import log


class DirectOpenAIClient(LLMClient):
    def __init__(self, provider: str, model_id: str):
        env_prefix = provider.upper().replace("-", "_")
        self.api_key = os.getenv(f"{env_prefix}_API_KEY") or os.getenv("LLM_API_KEY", "")
        self.provider = provider
        self.provider_id = provider
        self.model_id = model_id
        self._model = f"{provider}/{model_id}"

        if provider == "github-copilot":
            self.base_url = os.getenv(f"{env_prefix}_BASE_URL", "https://api.githubcopilot.com")
        else:
            self.base_url = os.getenv(f"{env_prefix}_BASE_URL", "https://api.openai.com/v1")

    @property
    def model(self) -> str:
        return self._model

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, body: dict, stream: bool = False):
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        timeout = httpx.Timeout(connect=10, read=120, write=30, pool=10)
        log("direct_post", model=self._model, url=url, stream=stream, msg_count=len(body.get("messages", [])),
            tools=len(body.get("tools", [])) if "tools" in body else 0)
        if stream:
            return httpx.stream("POST", url, json=body, headers=self._headers(), timeout=timeout)
        return httpx.post(url, json=body, headers=self._headers(), timeout=timeout)

    def _parse_usage(self, data: dict, messages: list = None, text: str = "", tool_calls: list = None) -> dict:
        usage_raw = data.get("usage", {})
        sent = usage_raw.get("prompt_tokens") or (estimate_messages_tokens(messages) if messages else 0)
        received = usage_raw.get("completion_tokens") or estimate_tokens(text or "")
        if tool_calls:
            for tc in tool_calls:
                received += estimate_tokens(str(tc.arguments))
        return {"sent": sent, "received": received}

    def ask(self, prompt: str, system: str | None = None) -> tuple[str, dict]:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        _dbg("ask", f"model={self._model} prompt_chars={len(prompt)}")
        log("direct_ask_start", model=self._model, prompt_chars=len(prompt), system=bool(system))
        resp = self._post({"model": self.model_id, "messages": messages, "stream": False})
        _dbg("ask", f"HTTP status={resp.status_code}")
        log("direct_ask_http", status=resp.status_code)

        if resp.status_code != 200:
            err_text = resp.text[:300]
            log("direct_ask_error", status=resp.status_code, text=err_text)
            raise RuntimeError(f"API error {resp.status_code}: {err_text}")

        data = resp.json()
        if "error" in data:
            log("direct_ask_api_error", error=data["error"])
            raise RuntimeError(f"API error: {data['error']}")

        choice = data.get("choices", [{}])[0]
        text = choice.get("message", {}).get("content", "") or ""
        _dbg("ask", f"response_chars={len(text)} preview={text[:120]!r}")

        if not text.strip():
            log("direct_ask_empty", model=self._model)
            raise RuntimeError(f"Empty response from {self._model}")

        usage = self._parse_usage(data, messages=messages, text=text)
        log("direct_ask_done", text_chars=len(text), usage=usage)
        return text, usage

    def ask_stream(self, prompt: str, system: str | None = None):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        log("direct_ask_stream_start", model=self._model, prompt_chars=len(prompt), system=bool(system))

        with self._post({"model": self.model_id, "messages": messages, "stream": True}, stream=True) as resp:
            log("direct_ask_stream_connected", status=resp.status_code)
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    log("direct_ask_stream_done")
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        log("direct_ask_stream_chunk", chars=len(content))
                        yield content
                except json.JSONDecodeError:
                    log("direct_ask_stream_parse_error", line=data[:100])
                    pass

    def chat(self, messages: list, tools: list | None = None) -> ChatResponse:
        body = {"model": self.model_id, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        _dbg("chat", f"model={self._model} messages={len(messages)} tools={len(tools) if tools else 0}")
        log("direct_chat_start", model=self._model, messages=len(messages), tools=len(tools) if tools else 0)
        resp = self._post(body)
        log("direct_chat_http", status=resp.status_code)
        data = resp.json()

        if "error" in data:
            log("direct_chat_api_error", error=data["error"])
            raise RuntimeError(f"API error: {data['error']}")

        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        content = msg.get("content", "")
        raw_tool_calls = msg.get("tool_calls") or []

        tool_calls = None
        if raw_tool_calls:
            tool_calls = []
            for tc in raw_tool_calls:
                fn = tc.get("function", {})
                tool_calls.append(ToolCall(
                    id=tc.get("id", ""),
                    name=fn.get("name", ""),
                    arguments=fn.get("arguments", ""),
                ))

        usage = self._parse_usage(data, messages=messages, text=content, tool_calls=tool_calls)
        result = ChatResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=choice.get("finish_reason", ""),
        )
        log("direct_chat_done", content_chars=len(content), tool_calls=len(tool_calls) if tool_calls else 0,
            finish_reason=result.finish_reason, usage=usage)
        return result

    def chat_stream(self, messages: list, tools: list | None = None):
        body = {"model": self.model_id, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        _dbg("chat_stream", f"model={self._model} messages={len(messages)} tools={len(tools) if tools else 0}")
        log("direct_chat_stream_start", model=self._model, messages=len(messages), tools=len(tools) if tools else 0)

        tool_buffers = {}
        text_parts = []
        usage = {}
        finish_reason = ""

        with self._post(body, stream=True) as resp:
            log("direct_chat_stream_connected", status=resp.status_code)
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    log("direct_chat_stream_done_signal")
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    log("direct_chat_stream_parse_error", line=data[:100])
                    continue
                usage = chunk.get("usage", usage)
                choice = chunk.get("choices", [{}])[0]
                finish_reason = choice.get("finish_reason", finish_reason)
                delta = choice.get("delta", {})

                if "content" in delta and delta["content"]:
                    c = delta["content"]
                    text_parts.append(c)
                    log("direct_chat_stream_text", chars=len(c), total=sum(len(t) for t in text_parts))
                    yield Chunk(type="text", content=c)

                if "tool_calls" in delta:
                    for tc_delta in delta["tool_calls"]:
                        idx = tc_delta.get("index", 0)
                        if idx not in tool_buffers:
                            tool_buffers[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                        buf = tool_buffers[idx]
                        if "id" in tc_delta and tc_delta["id"]:
                            buf["id"] = tc_delta["id"]
                        fn = tc_delta.get("function", {})
                        if "name" in fn and fn["name"]:
                            if not buf["function"]["name"]:
                                log("direct_chat_stream_tool_start", name=fn["name"])
                                yield Chunk(type="tool_call_start", name=fn["name"])
                            buf["function"]["name"] = fn["name"]
                        if "arguments" in fn and fn["arguments"]:
                            buf["function"]["arguments"] += fn["arguments"]
                            log("direct_chat_stream_tool_args", idx=idx, args_len=len(buf["function"]["arguments"]))

        log("direct_chat_stream_complete", finish_reason=finish_reason, text_chars=sum(len(t) for t in text_parts),
            tool_count=len(tool_buffers), usage=usage)

        if tool_buffers:
            calls = [ToolCall(
                id=tool_buffers[k]["id"],
                name=tool_buffers[k]["function"]["name"],
                arguments=tool_buffers[k]["function"]["arguments"],
            ) for k in sorted(tool_buffers.keys())]
            log("direct_chat_stream_yield_calls", calls=[tc.name for tc in calls])
            yield Chunk(type="tool_calls", tool_calls=calls)

        yield Chunk(
            type="done",
            usage={
                "sent": usage.get("prompt_tokens") or estimate_messages_tokens(messages),
                "received": usage.get("completion_tokens") or estimate_tokens("".join(text_parts)),
            },
            finish_reason=finish_reason,
        )
