"""上下文管理包（#0009）：两层压缩（卸载 + 摘要）+ 实时 token 统计。

分层（下层纯逻辑，不依赖上层）：
  estimate   —— token 近似估算（锚定服务端 usage + 增量字符；无锚点全量近似）
  offload    —— 第一层预防：大工具结果卸载到稳定目录、原位留预览 + 路径
  summarize  —— 第二层兜底：摘要 prompt / 调 LLM / 解析 / 熔断 / keep-recent 切分 / 边界消息
  manager    —— 编排：每请求前「先卸载、后判摘要」；实时统计；锚点；备份 + 持久化；手动触发
对外主入口：``ContextManager``。
"""

from coreagent.context.estimate import estimate_input_tokens
from coreagent.context.manager import ContextManager, format_token_stat

__all__ = ["ContextManager", "estimate_input_tokens", "format_token_stat"]
