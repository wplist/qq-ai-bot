"""GLM-5.3 调用封装（OpenAI 兼容协议）。

通过智谱开放平台的 OpenAI 兼容端点调用 GLM-5.3，
thinking 始终开启（模型要求），reasoning_effort 可配置。
"""

import logging

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

VALID_EFFORTS = {"low", "high", "max", "auto"}


class GLMError(Exception):
    """调用 GLM 失败时抛出，message 为可直接展示给用户的中文提示。"""


class GLMClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        reasoning_effort: str = "low",
        max_tokens: int = 4096,
        timeout: float = 120,
    ):
        if reasoning_effort not in VALID_EFFORTS:
            raise ValueError(
                f"reasoning_effort 只能是 {'/'.join(sorted(VALID_EFFORTS))}，当前为 {reasoning_effort!r}"
            )
        self.model = model
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=1,
        )

    async def chat(self, messages: list[dict]) -> str:
        """传入 OpenAI 格式的 messages，返回模型回复文本。失败抛 GLMError。"""
        params: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        if "glm" in self.model.lower():
            # thinking/reasoning_effort 是 GLM 专属参数；
            # 其它 OpenAI 兼容端点可能因未知字段报错，仅在 GLM 系模型时携带
            effort = "low" if self.reasoning_effort == "auto" else self.reasoning_effort
            params["extra_body"] = {
                "thinking": {"type": "enabled"},
                "reasoning_effort": effort,
            }
        try:
            resp = await self._client.chat.completions.create(**params)
        except Exception as e:
            logger.warning("模型调用异常: %s", e)
            # 带原因的提示便于控制台测试排查；聊天侧不会直接展示（_ask_glm 有固定文案）
            raise GLMError(f"AI 调用失败：{str(e)[:150]}") from e

        content = ""
        if resp.choices:
            content = resp.choices[0].message.content or ""
        content = content.strip()
        if not content:
            raise GLMError("AI 返回了空回复，请稍后再试")
        return content

    async def ping(self) -> str:
        """自检：发送一个极简问题，返回模型回复。用于 --check-glm。"""
        reply = await self.chat(
            [{"role": "user", "content": "请只回复两个字：正常"}]
        )
        return reply
