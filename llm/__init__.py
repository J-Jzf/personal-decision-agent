"""OpenAI 兼容模型边界与安全的确定性降级实现。"""

from .adapter import DeterministicReasoner, ModelAdapter

__all__ = ["DeterministicReasoner", "ModelAdapter"]
