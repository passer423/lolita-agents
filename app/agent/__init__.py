"""agent 包：多路召回 + LangGraph 编排 + 轨迹。

分工：
* ``channels.py``  六召回通道（own / real / lolibrary / xhs / weibo / web），一源一通道
* ``planner.py``   单次规划：核心召回后判断要不要追加实时通道（JSON 协议）
* ``graph.py``     LangGraph 编排层：并行扇出 + 条件补查 + 护栏（recursion_limit / verify）
* ``writer.py``    答案写作（流式），只在证据范围内措辞
* ``trace.py``     一次 run 一行 JSONL，用于回答"它为什么没找到"
"""
