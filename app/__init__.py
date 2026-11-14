"""lolita-agents：Lolita 领域问答助手（多路召回 → 生成回答 → 关注项通知）。

应用层只做：单页聊天前端、多路召回编排（LangGraph）、把知识问答转发给上游管线。
检索与生成的核心（清洗/切分/BGE/混合检索/闸门/verify）刻意不在这里实现——
那是 lolita-rag 的职责，见 app/bridge.py。
"""

__version__ = "0.2.0"
