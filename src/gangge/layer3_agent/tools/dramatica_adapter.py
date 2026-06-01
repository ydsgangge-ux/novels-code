"""
LLM 适配器：将 Gangge Code 的 BaseLLM 适配为 Dramatica-Flow 的 LLMProvider。

Gangge Code 使用 async BaseLLM.chat()，
Dramatica-Flow 使用 sync LLMProvider.complete() / stream()。
本适配器在两者之间做桥接。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from gangge.dramatica.llm import LLMProvider, LLMMessage, LLMResponse as DFLMResponse

logger = logging.getLogger(__name__)


class DramaticaLLMAdapter(LLMProvider):
    """
    将 Gangge Code 的 BaseLLM 适配为 Dramatica-Flow 的 LLMProvider。

    Dramatica-Flow 的 Agent 全部使用同步调用（complete / stream），
    而 Gangge Code 的 BaseLLM 是异步的（async chat）。
    本适配器通过 asyncio.run() 桥接。
    """

    def __init__(self, gangge_llm):
        self._llm = gangge_llm

    def complete(self, messages: list[LLMMessage]) -> DFLMResponse:
        from gangge.layer5_llm.base import (
            Message,
            Role,
            ContentBlock,
            ContentType,
        )

        gm_messages = []
        for m in messages:
            role_map = {
                "system": Role.SYSTEM,
                "user": Role.USER,
                "assistant": Role.ASSISTANT,
            }
            role = role_map.get(m.role, Role.USER)
            msg = Message(role=role)
            msg.add_text(m.content)
            gm_messages.append(msg)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, self._async_complete(gm_messages))
            resp = future.result()
        else:
            resp = asyncio.run(self._async_complete(gm_messages))

        return resp

    async def _async_complete(self, gm_messages):
        from gangge.layer5_llm.base import LLMResponse

        response: LLMResponse = await self._llm.chat(
            messages=gm_messages,
            tools=None,
            system="",
        )

        text = response.text
        usage = response.usage or {}

        return DFLMResponse(
            content=text,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )

    def stream(
        self,
        messages: list[LLMMessage],
        on_chunk: Callable[[str], None],
    ) -> DFLMResponse:
        full_content = ""

        def _collect_chunk(chunk_text: str):
            nonlocal full_content
            full_content += chunk_text
            if on_chunk:
                on_chunk(chunk_text)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    self._async_stream(gm_messages := self._convert_messages(messages), _collect_chunk),
                )
            resp = future.result()
        else:
            resp = asyncio.run(
                self._async_stream(self._convert_messages(messages), _collect_chunk)
            )

        return DFLMResponse(content=full_content)

    def _convert_messages(self, messages: list[LLMMessage]):
        from gangge.layer5_llm.base import Message, Role

        gm_messages = []
        for m in messages:
            role_map = {
                "system": Role.SYSTEM,
                "user": Role.USER,
                "assistant": Role.ASSISTANT,
            }
            role = role_map.get(m.role, Role.USER)
            msg = Message(role=role)
            msg.add_text(m.content)
            gm_messages.append(msg)
        return gm_messages

    async def _async_stream(self, gm_messages, on_text):
        from gangge.layer5_llm.base import ContentType

        async for block in self._llm.stream(
            messages=gm_messages,
            tools=None,
            system="",
        ):
            if block.type == ContentType.TEXT and block.text:
                on_text(block.text)
