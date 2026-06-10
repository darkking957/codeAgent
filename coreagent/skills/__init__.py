"""技能系统（#0012）：把可复用 AI 流程封装为「YAML 元信息 + Markdown SOP」的单文件 / 目录技能。

子模块职责（自下而上）：

  constants     —— 固定值（三级目录 / frontmatter 字段 / 占位符 / 默认值 / loader 名 / 注入标签）
  types         —— SkillMode 枚举 + SkillSpec 数据结构 + SkillParseError
  parser        —— frontmatter + 正文解析 + $ARGUMENTS 占位符替换
  dedtools      —— 目录型技能专属工具的进程内 importlib 加载 + schema 校验
  discovery     —— 三级发现（项目 > 用户 > 内置）+ 同名覆盖 + 单文件/目录型识别 + 解析失败跳过
  store         —— SkillStore：发现映射 + 激活态 + 渲染块 + 白名单并集/校验 + 激活/注销/清空
  loader_tool   —— 系统级 load_skill 工具（模型按需激活、恒在、免白名单）
  independent   —— 独立模式 runner（子对话执行 + 模型自产摘要回流 + token 预算带入近段历史）
  commands      —— 技能作为命令次级来源的纯助手（补全 / 触发提示词 / 遮蔽检测）
  builtins/     —— 内置三样板技能：commit / review / test

边界：注入复用 #0006、工具/registry 复用 #0008、独立模式带入历史借鉴 #0009、命令分发并入 #0011、
白名单与 #0007 权限门正交（前者管可见、后者管可否执行）。包级保持轻量、不做重导出（避免底层
模块 import 触发重链路）。
"""
