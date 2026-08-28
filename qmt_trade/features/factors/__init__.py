"""因子库。导入即完成注册。

新增因子只需在对应模块里用 ``@registry.register(...)`` 装饰，
再在下面 import 一次即可被引擎发现。
"""

from . import fundamental, momentum, moneyflow, sentiment  # noqa: F401

__all__ = ["momentum", "moneyflow", "fundamental", "sentiment"]
