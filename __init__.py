# -*- coding: utf-8 -*-
"""qqnotice_sum —— 读通知群消息、总结成待办清单。

框架按 `PLUGIN` 显式取入口类；包内一律相对导入。
"""
from .plugin import QQNoticeSumPlugin

PLUGIN = QQNoticeSumPlugin
__all__ = ["QQNoticeSumPlugin"]
