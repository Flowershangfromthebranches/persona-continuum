from __future__ import annotations

from persona_continuum.agent.adapter import AgentAdapter, AgentSession
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor, RuntimeSessionBinding
from persona_continuum.application._utils import new_id


class HostAgent:
    """Neutral LLM-backed facilitator for opening, steering, and summarizing a room."""

    def __init__(self, runtime_executor: AgentRuntimeExecutor | None = None) -> None:
        self.runtime_executor = runtime_executor or AgentRuntimeExecutor()

    SYSTEM_PROMPT = (
        "你是中立的讨论主持人。你负责提出清晰问题、保持主题、邀请最相关的人回应，"
        "并在结束时总结共识、分歧与未解决问题。不要替参与者表达观点。"
    )

    async def generate(
        self,
        *,
        adapter: AgentAdapter,
        session: AgentSession,
        topic: str,
        transcript: list[dict[str, object]],
        phase: str,
        runtime_binding: RuntimeSessionBinding | None = None,
    ) -> str:
        recent = "\n".join(
            f"{item.get('speaker_name') or item.get('participant_id')}: {item.get('content')}"
            for item in transcript
        )
        instructions = {
            "open": f"提出讨论议题并邀请一位参与者先阐述：{topic}",
            "steer": f"检查是否跑题并提出一个推进讨论的问题：{topic}",
            "summary": f"总结关于 {topic} 的共识、分歧、依据与待验证问题。",
        }
        binding = runtime_binding or await self.runtime_executor.bind_existing_session(
            adapter, session
        )
        call_id = new_id("host_turn")
        result = await self.runtime_executor.execute_text(
            binding,
            system_prompt=self.SYSTEM_PROMPT,
            user_message=f"{instructions[phase]}\n最近对话：\n{recent}",
            phase=f"host_{phase}",
            stream=False,
            metadata={"call_id": call_id},
        )
        session.session_data["last_completion_metadata"] = {
            "usage": result.response.usage,
            "audit": result.response.audit(
                call_id=call_id,
                job_id=None,
                phase=f"host_{phase}",
            ),
        }
        return result.text
