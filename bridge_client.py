import httpx
import json
import os
import time

from llm_client import LLMClient, ChatResponse, Chunk, ToolCall, _dbg, estimate_tokens, estimate_messages_tokens
from log import log

SERVER_URL = os.getenv("OPENCODE_SERVER", "http://localhost:4096")
BRIDGE_URL = os.getenv("OPENCODE_BRIDGE", "http://localhost:5500")


class OpenCodeBridgeClient(LLMClient):
    def __init__(self, provider_id: str, model_id: str):
        self.provider_id = provider_id
        self.model_id = model_id
        self._model = f"{provider_id}/{model_id}"
        self.workspace: str | None = None

    @property
    def model(self) -> str:
        return self._model

    def ask(self, prompt: str, system: str | None = None) -> tuple[str, dict]:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        sent_est = estimate_messages_tokens(messages)
        _dbg("ask", f"model={self._model} prompt_chars={len(prompt)}")
        log("bridge_ask_start", model=self._model, prompt_chars=len(prompt), system=bool(system))

        with httpx.Client(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            resp = client.post(
                f"{BRIDGE_URL}/v1/chat/completions",
                json={"model": self._model, "messages": messages, "stream": False},
            )

        _dbg("ask", f"HTTP status={resp.status_code}")
        log("bridge_ask_http", status=resp.status_code)

        if resp.status_code != 200:
            err_text = resp.text[:300]
            log("bridge_ask_error", status=resp.status_code, text=err_text)
            raise RuntimeError(f"Bridge error {resp.status_code}: {err_text}")

        data = resp.json()
        if "error" in data:
            err_msg = data["error"].get("message", str(data["error"]))
            _dbg("ask", f"bridge ERROR: {err_msg}")
            log("bridge_ask_api_error", message=err_msg)
            raise RuntimeError(f"Bridge error: {err_msg}")

        choice = data.get("choices", [{}])[0]
        text = choice.get("message", {}).get("content", "") or ""
        _dbg("ask", f"response_chars={len(text)} preview={text[:120]!r}")

        if not text.strip():
            log("bridge_ask_empty", model=self._model)
            raise RuntimeError(f"Empty response from {self._model}")

        usage_raw = data.get("usage", {})
        received = usage_raw.get("completion_tokens") or estimate_tokens(text)
        usage = {
            "sent": usage_raw.get("prompt_tokens") or sent_est,
            "received": received,
        }
        log("bridge_ask_done", text_chars=len(text), usage=usage)
        return text, usage

    def ask_stream(self, prompt: str, system: str | None = None):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        log("bridge_ask_stream_start", model=self._model, prompt_chars=len(prompt), system=bool(system))

        with httpx.Client(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            with client.stream(
                "POST", f"{BRIDGE_URL}/v1/chat/completions",
                json={"model": self._model, "messages": messages, "stream": True},
            ) as resp:
                log("bridge_ask_stream_connected", status=resp.status_code)
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        log("bridge_ask_stream_done")
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            log("bridge_ask_stream_chunk", chars=len(content))
                            yield content
                    except json.JSONDecodeError:
                        log("bridge_ask_stream_parse_error", line=data[:100])
                        pass

    def _send_request(self, messages: list, tools: list | None, stream: bool):
        body = {"model": self._model, "messages": messages, "stream": stream}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self.workspace:
            headers["x-working-directory"] = self.workspace

        return body, headers

    def chat(self, messages: list, tools: list | None = None) -> ChatResponse:
        body, headers = self._send_request(messages, tools, stream=False)
        _dbg("chat", f"model={self._model} messages={len(messages)} tools={len(tools) if tools else 0}")
        log("bridge_chat_start", model=self._model, messages=len(messages), tools=len(tools) if tools else 0)

        with httpx.Client(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            resp = client.post(f"{BRIDGE_URL}/v1/chat/completions", json=body)
            log("bridge_chat_http", status=resp.status_code)

            if "error" in resp.text[:resp.text.find("}")+1] if "}" in resp.text else "":
                try:
                    err_data = resp.json()
                    if "error" in err_data:
                        msg = err_data["error"].get("message", str(err_data["error"]))
                        log("bridge_chat_api_error", message=msg)
                        raise RuntimeError(f"Bridge error: {msg}")
                except (json.JSONDecodeError, RuntimeError):
                    if resp.status_code != 200:
                        raise RuntimeError(f"Bridge error {resp.status_code}: {resp.text[:300]}")

            data = resp.json()
            choice = data.get("choices", [{}])[0]
            msg = choice.get("message", {})
            usage_raw = data.get("usage", {})
            content = msg.get("content", "")
            raw_tool_calls = msg.get("tool_calls") or []
            received_est = usage_raw.get("completion_tokens") or estimate_tokens(content)
            tool_calls = None
            if raw_tool_calls:
                calls = []
                for tc in raw_tool_calls:
                    fn = tc.get("function", {})
                    calls.append(ToolCall(
                        id=tc.get("id", ""),
                        name=fn.get("name", ""),
                        arguments=fn.get("arguments", ""),
                    ))
                    args = fn.get("arguments", "")
                    received_est += estimate_tokens(str(args))
                tool_calls = calls

            result = ChatResponse(
                content=content,
                tool_calls=tool_calls,
                usage={
                    "sent": usage_raw.get("prompt_tokens") or estimate_messages_tokens(messages),
                    "received": received_est,
                },
                finish_reason=choice.get("finish_reason", ""),
            )
            log("bridge_chat_done", content_chars=len(content), tool_calls=len(tool_calls) if tool_calls else 0,
                finish_reason=result.finish_reason, usage=result.usage)
            return result

    def chat_stream(self, messages: list, tools: list | None = None):
        body, headers = self._send_request(messages, tools, stream=True)
        _dbg("stream_tools", f"model={self._model} messages={len(messages)} tools={len(tools) if tools else 0} workspace={self.workspace}")
        log("bridge_chat_stream_start", model=self._model, messages=len(messages), tools=len(tools) if tools else 0, workspace=self.workspace)

        tool_buffers = {}
        text_parts = []
        usage = {}
        finish_reason = ""

        with httpx.Client(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            with client.stream(
                "POST", f"{BRIDGE_URL}/v1/chat/completions", json=body, headers=headers
            ) as resp:
                _dbg("stream_tools", f"HTTP status={resp.status_code}")
                log("bridge_chat_stream_connected", status=resp.status_code)
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        log("bridge_chat_stream_done_signal")
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        log("bridge_chat_stream_parse_error", line=data[:100])
                        continue
                    usage = chunk.get("usage", usage)
                    choice = chunk.get("choices", [{}])[0]
                    finish_reason = choice.get("finish_reason", finish_reason)
                    delta = choice.get("delta", {})

                    if "content" in delta and delta["content"]:
                        c = delta["content"]
                        text_parts.append(c)
                        log("bridge_chat_stream_text", chars=len(c), total=sum(len(t) for t in text_parts))
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
                                    log("bridge_chat_stream_tool_start", name=fn["name"])
                                    yield Chunk(type="tool_call_start", name=fn["name"])
                                buf["function"]["name"] = fn["name"]
                            if "arguments" in fn and fn["arguments"]:
                                buf["function"]["arguments"] += fn["arguments"]
                                log("bridge_chat_stream_tool_args", idx=idx, args_len=len(buf["function"]["arguments"]))

        _dbg("stream_tools", f"finish_reason={finish_reason!r} text_chars={sum(len(t) for t in text_parts)} tool_buffers={len(tool_buffers)} usage={usage}")
        log("bridge_chat_stream_complete", finish_reason=finish_reason, text_chars=sum(len(t) for t in text_parts),
            tool_count=len(tool_buffers), usage=usage)

        if tool_buffers:
            calls = [ToolCall(
                id=tool_buffers[k]["id"],
                name=tool_buffers[k]["function"]["name"],
                arguments=tool_buffers[k]["function"]["arguments"],
            ) for k in sorted(tool_buffers.keys())]
            log("bridge_chat_stream_yield_calls", calls=[tc.name for tc in calls])
            yield Chunk(type="tool_calls", tool_calls=calls)

        yield Chunk(
            type="done",
            usage={
                "sent": usage.get("prompt_tokens") or estimate_messages_tokens(messages),
                "received": usage.get("completion_tokens") or estimate_tokens("".join(text_parts)),
            },
            finish_reason=finish_reason,
        )
