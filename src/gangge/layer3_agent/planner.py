"""Plan & Execute planner — for complex multi-step tasks."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from gangge.layer3_agent.loop import AgenticLoop, LoopConfig, LoopResult
from gangge.layer5_llm.base import BaseLLM, Message, Role, ContentBlock, ContentType

logger = logging.getLogger(__name__)


@dataclass
class PlanStep:
    """A single step in an execution plan."""

    index: int
    description: str
    files: list[str] = field(default_factory=list)
    completed: bool = False
    result: str = ""


class Planner:
    """Plan & Execute orchestrator.

    For complex tasks:
    1. Generate a plan (list of steps)
    2. Show plan to user for confirmation
    3. Execute steps one by one
    4. Update plan status after each step
    """

    def __init__(self, loop: AgenticLoop, llm: BaseLLM):
        self.loop = loop
        self.llm = llm

    async def generate_plan(
        self,
        task: str,
        project_context: str = "",
    ) -> list[PlanStep]:
        """Ask the LLM to generate an execution plan for the given task."""
        plan_prompt = f"""请为以下任务制定一个执行计划。输出 JSON 数组格式，每个元素包含:
- "step": 步骤编号
- "description": 步骤描述
- "files": 涉及的文件路径列表

任务: {task}

{f"项目信息:\n{project_context}" if project_context else ""}

只输出 JSON 数组，不要其他内容。例如:
[{{"step": 1, "description": "xxx", "files": ["src/a.py"]}}]"""

        messages = [Message(role=Role.USER, content=[ContentBlock(type=ContentType.TEXT, text=plan_prompt)])]
        response = await self.llm.chat(messages=messages)

        text = response.text.strip()
        # Try to extract JSON from the response
        import json
        try:
            # Find JSON array in response
            start = text.find("[")
            end = text.rfind("]") + 1
            if start != -1 and end > start:
                json_str = text[start:end]
                steps_data = json.loads(json_str)
                return [
                    PlanStep(
                        index=s["step"],
                        description=s["description"],
                        files=s.get("files", []),
                    )
                    for s in steps_data
                ]
        except (json.JSONDecodeError, KeyError, IndexError):
            pass

        # Fallback: treat the entire response as a single step
        logger.warning("Failed to parse plan JSON, creating single-step plan")
        return [PlanStep(index=1, description=text)]

    async def execute_plan(
        self,
        plan: list[PlanStep],
        messages: list[Message],
        on_step_complete: Any = None,
    ) -> LoopResult:
        """Execute a plan step by step."""
        all_executions = []
        total_tokens = {"input": 0, "output": 0}

        for step in plan:
            logger.info(f"Executing plan step {step.index}: {step.description}")

            # Add step instruction to messages
            messages.append(Message(
                role=Role.USER,
                content=[ContentBlock(
                    type=ContentType.TEXT,
                    text=f"请执行计划的第 {step.index} 步: {step.description}",
                )],
            ))

            # Run agentic loop for this step
            result = await self.loop.run(messages)
            all_executions.extend(result.tool_executions)
            total_tokens["input"] += result.total_tokens.get("input", 0)
            total_tokens["output"] += result.total_tokens.get("output", 0)

            step.completed = True
            step.result = result.final_response[:200]

            if on_step_complete:
                await on_step_complete(step, result)

        return LoopResult(
            final_response="\n".join(
                f"Step {s.index}: {'✓' if s.completed else '✗'} {s.description}"
                for s in plan
            ),
            tool_executions=all_executions,
            total_rounds=sum(r.total_rounds for r in all_executions),
            total_tokens=total_tokens,
        )
