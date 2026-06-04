"""领域异常。

仅用于分类，让入口层据此给出干净的中文退出信息，而不是向用户抛裸栈。
"""


class CoreAgentError(Exception):
    """CoreAgent 所有领域异常的基类。"""


class ConfigError(CoreAgentError):
    """配置非法：取值不合法、必填项缺失、或 ${ENV} 解析后为空等。"""
