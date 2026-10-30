"""ShotBridge 摄像机运镜描述器(包入口)。

设计原则:分析内核(analysis / models / math3d / prompt)完全不依赖 Maya,
Maya 与 PySide6 都采用“用到才导入”(懒加载),
因此核心逻辑可以在普通 Python 解释器中运行和测试,不必启动 Maya。
"""

from .analysis import analyze_samples
from .models import AnalysisConfig, CameraSample, MotionReport

# 包的对外接口:除 show 外都是纯 Python 的分析对象与函数
__all__ = ["AnalysisConfig", "CameraSample", "MotionReport", "analyze_samples", "show"]
__version__ = "1.0.0"


def show():
    """打开 Maya 窗口(进入 Maya 环境的唯一入口)。"""

    # 延迟导入:在普通 Python 中 import 本包时不会触发 PySide6 / Maya 界面模块的加载
    from .ui import show as show_window

    return show_window()
