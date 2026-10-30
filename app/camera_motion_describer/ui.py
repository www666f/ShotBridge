"""ShotBridge 摄像机运镜描述器界面(Maya 2026 + PySide6)。

窗口结构(自上而下):
- 摄像机行:从 Maya 当前选择读取摄像机;
- 被摄主体组:可选,支持多个目标及增删操作;
- 分析范围行:显示 Maya 播放范围,逐帧采样;
- 参数行:异常判定容差(下次分析生效)与分段合并强度(即时重排提示词);
- 结果页签:“中文提示词”(可编辑、面向 AI 网站)/“结构化详情”(只读 JSON);
- 底栏:状态文字 + “复制提示词”按钮。

与 Maya 的衔接:
- Qt 与 Maya 模块全部在 _qt_modules() 中延迟导入,
  在普通 Python 环境 import 本模块不会报错;
- 窗口以 Maya 主窗口为 parent(见 maya_main_window);
- 算法警告进入状态栏和结构化详情；未识别出准确点的异常帧与复杂复合运动
  在中文提示词中以“【复检提示】”红字行标注,复制时自动剔除。
"""

from __future__ import annotations

from dataclasses import replace

import json
import math
import traceback

from .maya_adapter import (
    analyze_maya_camera,
    camera_from_selection,
    playback_frame_range,
    targets_from_selection,
)
from .prompt import (
    DISPLAY_WARNING_PREFIX,
    PROMPT_MERGE_THRESHOLD_DEFAULT,
    build_prompt,
    group_phases,
    strip_display_warnings,
)
from .models import AnalysisConfig


# 全局窗口单例:再次打开时先关闭旧窗口,保证屏幕上只有一个描述器
_WINDOW = None


def _qt_modules():
    """延迟导入 Qt 与 Maya UI 模块,避免在非 Maya 环境下 import 本模块报错。

    返回五元组:(QtCore, QtGui, QtWidgets, wrapInstance, omui)
    - shiboken6.wrapInstance:把 Maya 的 C++ 指针(如主窗口)包装成 Qt 对象;
    - maya.OpenMayaUI(MQtUtil):提供 Maya UI 侧的 C++ 指针入口。
    """

    from PySide6 import QtCore, QtGui, QtWidgets
    from shiboken6 import wrapInstance
    import maya.OpenMayaUI as omui

    return QtCore, QtGui, QtWidgets, wrapInstance, omui


def maya_main_window():
    """取得 Maya 主窗口对应的 Qt 对象,用作本对话框的父窗口。

    omui.MQtUtil.mainWindow() 返回主窗口的 C++ 指针(整数);
    wrapInstance 把该指针与 QWidget 绑定,得到 Qt 侧的窗口对象。
    以主窗口为 parent:Maya 最小化 / 关闭时本窗口随之处理,
    且不会在系统任务栏中多出一个独立图标。
    """

    _, _, QtWidgets, wrapInstance, omui = _qt_modules()
    pointer = omui.MQtUtil.mainWindow()
    return wrapInstance(int(pointer), QtWidgets.QWidget) if pointer else None


def _build_prompt_edit_class():
    """构造提示词编辑器类(类定义依赖 PySide6,必须延迟创建)。

    该编辑器让复检提示不进剪贴板:
    红色提示仍显示在文本框里,但复制内容会被剔除。
    """

    _, QtGui, QtWidgets, _, _ = _qt_modules()

    class PromptTextEdit(QtWidgets.QPlainTextEdit):
        """提示词编辑器:红色复检提示仅供展示,复制时自动剔除。"""

        def keyPressEvent(self, event):
            # 拦截标准“复制”快捷键(Ctrl+C / Cmd+C),改走剔除警告的复制
            if event.matches(QtGui.QKeySequence.StandardKey.Copy):
                self.copy_without_warnings()
                return
            super().keyPressEvent(event)

        def copy_without_warnings(self):
            """复制选区(无选区则整篇)到剪贴板,自动剔除红色复检提示。"""

            cursor = self.textCursor()
            # 有选区:复制选区;无选区:复制全文
            text = (
                cursor.selection().toPlainText()
                if cursor.hasSelection()
                else self.toPlainText()
            )
            QtWidgets.QApplication.clipboard().setText(strip_display_warnings(text))

    return PromptTextEdit


def _build_window_class():
    QtCore, QtGui, QtWidgets, _, _ = _qt_modules()

    class CameraMotionDescriberWindow(QtWidgets.QDialog):
        """摄像机运镜描述器主窗口(结构见模块说明)。"""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("ShotBridgeCameraMotionDescriber")  # 供 Maya 侧按名字定位控件
            self.setWindowTitle("ShotBridge 摄像机运镜描述器 1.0")
            self.resize(960, 780)
            self.setMinimumSize(820, 660)
            self._camera = ""   # 已选择的摄像机(完整 DAG 路径)
            self._targets = []  # 已添加的被摄主体(完整 DAG 路径列表)
            self._last_report = None        # 最近一次分析结果(分段滑杆重排用)
            self._last_has_targets = False  # 分析时是否选择了被摄主体
            self._running = False           # 分析进行中标记(防重入/防关窗)
            self._help_window = None
            self._build_ui()
            self._refresh_range()
            self._load_initial_selection()

        def _build_ui(self):
            """自上而下搭建界面:摄像机 → 主体 → 范围 → 分析 → 结果 → 底栏。"""

            root = QtWidgets.QVBoxLayout(self)

            self._build_header(root)
            self._build_camera(root)
            self._build_targets(root)
            self._build_range(root)
            self._build_results(root)

        def _build_header(self, root):
            header = QtWidgets.QHBoxLayout()
            title = QtWidgets.QLabel("摄像机运镜描述器")
            title.setStyleSheet("font-size: 18px; font-weight: 600;")
            header.addWidget(title)
            header.addStretch(1)
            help_button = QtWidgets.QPushButton("？")
            help_button.setObjectName("helpButton")
            help_button.setFixedWidth(36)
            help_button.setToolTip("打开完整使用方法、算法说明与已知限制")
            help_button.clicked.connect(self._show_help)
            header.addWidget(help_button)
            root.addLayout(header)

        def _build_camera(self, root):
            # 摄像机行:只读文本框显示短名;按钮从 Maya 当前选择读取摄像机
            camera_row = QtWidgets.QHBoxLayout()
            camera_row.addWidget(QtWidgets.QLabel("摄像机"))
            self.camera_field = QtWidgets.QLineEdit()
            self.camera_field.setReadOnly(True)
            self.camera_field.setPlaceholderText("请在 Maya 中选择摄像机")
            self.camera_field.setToolTip("显示当前用于逐帧分析的 Maya 摄像机")
            camera_row.addWidget(self.camera_field, 1)
            self.camera_button = QtWidgets.QPushButton("获取所选摄像机")
            self.camera_button.setToolTip("先在 Maya 中选择摄像机，再点击这里")
            self.camera_button.clicked.connect(self._pick_camera)
            camera_row.addWidget(self.camera_button)
            root.addLayout(camera_row)

        def _build_targets(self, root):
            # 被摄主体组:主界面保持简洁，详细解释放到悬停提示与帮助窗口。
            target_group = QtWidgets.QGroupBox("被摄主体（可选，可选择多个）")
            target_group.setToolTip(
                "用于计算相机与主体的距离、方位和构图。多个主体的中心等权平均，"
                "画面范围使用所有目标网格的整体包围盒。"
            )
            target_layout = QtWidgets.QVBoxLayout(target_group)
            self.target_list = QtWidgets.QListWidget()
            self.target_list.setMaximumHeight(110)
            target_layout.addWidget(self.target_list)
            target_buttons = QtWidgets.QHBoxLayout()
            self.add_button = QtWidgets.QPushButton("添加所选目标")
            self.add_button.clicked.connect(self._add_targets)
            self.remove_button = QtWidgets.QPushButton("移除选中目标")
            self.remove_button.clicked.connect(self._remove_targets)
            self.clear_button = QtWidgets.QPushButton("清空目标")
            self.clear_button.clicked.connect(self._clear_targets)
            target_buttons.addWidget(self.add_button)
            target_buttons.addWidget(self.remove_button)
            target_buttons.addWidget(self.clear_button)
            target_layout.addLayout(target_buttons)
            root.addWidget(target_group)

        def _build_range(self, root):
            # 分析范围行:显示播放范围(逐帧采样);“刷新范围”重读 Maya 时间滑块
            range_row = QtWidgets.QHBoxLayout()
            range_row.addWidget(QtWidgets.QLabel("分析范围"))
            self.range_label = QtWidgets.QLabel("-")
            range_row.addWidget(self.range_label)
            range_row.addStretch(1)
            refresh_button = QtWidgets.QPushButton("刷新范围")
            refresh_button.clicked.connect(self._refresh_range)
            range_row.addWidget(refresh_button)
            root.addLayout(range_row)

        def _build_options(self, root):
            self.filter_spikes = QtWidgets.QCheckBox("过滤疑似单帧突变（只影响分析）")
            self.filter_spikes.setChecked(True)
            self.filter_spikes.setToolTip(
                "只过滤突然偏离、下一帧又返回的候选异常；不会修改 Maya 场景。"
            )
            root.addWidget(self.filter_spikes)
            tolerance_row = QtWidgets.QHBoxLayout()
            tolerance_row.addWidget(QtWidgets.QLabel("异常判定容差倍数"))
            self.spike_tolerance = QtWidgets.QDoubleSpinBox()
            self.spike_tolerance.setRange(0.1, 1000.0)
            self.spike_tolerance.setDecimals(1)
            self.spike_tolerance.setValue(8.0)
            self.spike_tolerance.setToolTip("数值越大越宽容，越少把跳变判为异常")
            tolerance_row.addWidget(self.spike_tolerance)
            self.spike_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            self.spike_slider.setObjectName("spikeToleranceSlider")
            # 对数滑杆让 0.1–1000 的宽范围仍然好调；默认 8 位于滑杆中部。
            self.spike_slider.setRange(0, 400)
            self.spike_slider.setValue(round((math.log10(8.0) + 1.0) * 100.0))
            self.spike_slider.setToolTip("拖动调整异常容差；右侧数字可精确输入")
            self.spike_slider.valueChanged.connect(self._set_tolerance_from_slider)
            self.spike_tolerance.valueChanged.connect(self._set_slider_from_tolerance)
            tolerance_row.insertWidget(1, self.spike_slider, 1)
            root.addLayout(tolerance_row)

            # 分段行:控制中文提示词的段落数量;改变后不重新分析,只重排提示词
            segment_row = QtWidgets.QHBoxLayout()
            segment_row.addWidget(QtWidgets.QLabel("分段合并强度"))
            self.merge_strength = QtWidgets.QSpinBox()
            self.merge_strength.setRange(0, 100)
            self.merge_strength.setValue(round(PROMPT_MERGE_THRESHOLD_DEFAULT * 100))
            self.merge_strength.setToolTip("数值越高段落越少；越低段落越多(0–100)")
            segment_row.addWidget(self.merge_strength)
            self.merge_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            self.merge_slider.setObjectName("mergeStrengthSlider")
            self.merge_slider.setRange(0, 100)
            self.merge_slider.setValue(round(PROMPT_MERGE_THRESHOLD_DEFAULT * 100))
            self.merge_slider.setToolTip("拖动调整分段合并强度；改变后立即重排提示词段落")
            self.merge_slider.valueChanged.connect(self._on_merge_changed)
            self.merge_strength.valueChanged.connect(self._on_merge_changed)
            segment_row.insertWidget(1, self.merge_slider, 1)
            self.phase_count_label = QtWidgets.QLabel("当前 0 段")
            self.phase_count_label.setToolTip("当前合并强度下中文提示词的段落数量")
            segment_row.addWidget(self.phase_count_label)
            root.addLayout(segment_row)

        def _build_results(self, root):
            # 主操作:逐帧读取场景并生成运镜描述
            self.analyze_button = QtWidgets.QPushButton("自动描述运镜")
            self.analyze_button.setMinimumHeight(42)
            self.analyze_button.setToolTip(
                "逐帧读取当前播放范围，生成 AI 中文提示词和机器可读结构化详情"
            )
            self.analyze_button.clicked.connect(self._analyze)
            root.addWidget(self.analyze_button)

            # 进度条:0–100 对应 已采样帧 / 总帧数
            self.progress = QtWidgets.QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            root.addWidget(self.progress)

            # 结果页签:左“中文提示词”(可编辑、复检提示标红),右“结构化详情”(只读 JSON)
            tabs = QtWidgets.QTabWidget()
            self.prompt_edit = _build_prompt_edit_class()()
            self.prompt_edit.setPlaceholderText("分析完成后，这里会显示中文提示词。")
            self.prompt_edit.setToolTip("面向 AI 视频网站，可人工修改；每个时间段独立成段")
            # 文本变化时重扫复检提示并标红(textChanged 信号)
            self.prompt_edit.textChanged.connect(self._highlight_display_warnings)
            self.details_edit = QtWidgets.QPlainTextEdit()
            self.details_edit.setReadOnly(True)
            self.details_edit.setToolTip("面向后续 Maya 工具的完整通道、主体轨迹和警告数据")
            tabs.addTab(self.prompt_edit, "中文提示词")
            tabs.addTab(self.details_edit, "结构化详情")
            settings = QtWidgets.QWidget()
            settings_layout = QtWidgets.QVBoxLayout(settings)
            self._build_options(settings_layout)
            settings_layout.addStretch(1)
            tabs.addTab(settings, "高级设置")
            root.addWidget(tabs, 1)

            # 底栏:状态文字 + “复制提示词”(复制时剔除红色复检提示)
            bottom = QtWidgets.QHBoxLayout()
            self.status_label = QtWidgets.QLabel("等待分析")
            bottom.addWidget(self.status_label, 1)
            self.copy_button = QtWidgets.QPushButton("复制提示词")
            self.copy_button.setToolTip("复制可直接提交给 AI 视频网站的正文")
            self.copy_button.clicked.connect(self._copy_prompt)
            bottom.addWidget(self.copy_button)
            root.addLayout(bottom)

        def _set_tolerance_from_slider(self, value):
            """把对数滑杆位置换算成 0.1–1000 的容差数值。"""

            blocker = QtCore.QSignalBlocker(self.spike_tolerance)
            self.spike_tolerance.setValue(round(10 ** (-1.0 + value / 100.0), 1))
            del blocker

        def _set_slider_from_tolerance(self, value):
            """数字输入变化时同步滑杆，并阻止两个控件互相反复触发。"""

            blocker = QtCore.QSignalBlocker(self.spike_slider)
            self.spike_slider.setValue(round((math.log10(value) + 1.0) * 100.0))
            del blocker

        def _on_merge_changed(self, value):
            """分段合并强度变化:同步滑杆与数字框,并即时重排提示词段落。"""

            slider_blocker = QtCore.QSignalBlocker(self.merge_slider)
            spin_blocker = QtCore.QSignalBlocker(self.merge_strength)
            self.merge_slider.setValue(value)
            self.merge_strength.setValue(value)
            del slider_blocker, spin_blocker
            self._refresh_prompt_from_report()

        def _refresh_prompt_from_report(self):
            """用最近一次分析结果与当前合并强度重排提示词(不重新读取 Maya 场景)。

            返回当前提示词段落数;尚未分析过时不做改动并返回 0。
            提示词、段落数、结构化详情都按同一份报告重算,保证三个位置一致。
            """

            report = self._last_report
            if report is None:
                self.phase_count_label.setText("当前 0 段")
                return 0
            threshold = self.merge_strength.value() / 100.0
            text = build_prompt(
                segments=report.segments,
                metadata=report.metadata,
                fps=report.fps,
                has_targets=self._last_has_targets,
                anomalies=report.anomalies,
                merge_threshold=threshold,
            )
            phase_count = len(group_phases(report.segments, report.fps, threshold))
            # 报告是 frozen dataclass,用 replace 生成新实例;metadata 是可变字典,就地同步
            report.metadata["prompt_phase_count"] = phase_count
            report.metadata["prompt_merge_threshold"] = threshold
            self._last_report = replace(report, prompt=text)
            # 重新渲染会触发 textChanged,由 _highlight_display_warnings 重扫红字行
            self.prompt_edit.setPlainText(text)
            self.phase_count_label.setText(f"当前 {phase_count} 段")
            self.details_edit.setPlainText(
                json.dumps(self._last_report.to_dict(), ensure_ascii=False, indent=2)
            )
            return phase_count

        def _invalidate_report(self):
            """摄像机或主体变化后丢弃旧报告,避免滑杆继续按旧结果重排提示词。"""

            self._last_report = None
            self._last_has_targets = False
            self.phase_count_label.setText("当前 0 段")
            self.prompt_edit.clear()
            self.details_edit.clear()

        def _set_busy(self, busy):
            """分析期间禁用会改变输入或报告的控件,防止重入和过期状态。"""

            self._running = busy
            for widget in (
                self.analyze_button,
                self.camera_button,
                self.add_button,
                self.remove_button,
                self.clear_button,
                self.copy_button,
                self.merge_slider,
                self.merge_strength,
            ):
                widget.setEnabled(not busy)

        def closeEvent(self, event):
            """分析期间拒绝关闭窗口,避免回调访问已被 Qt 释放的控件。"""

            if self._running:
                self.status_label.setText("正在分析，请等待完成后再关闭窗口")
                event.ignore()
                return
            super().closeEvent(event)

        def _show_help(self):
            """打开非模态帮助窗口，主界面仍可继续查看和操作。"""

            if self._help_window is not None:
                self._help_window.raise_()
                self._help_window.activateWindow()
                return
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("运镜描述器：使用方法与算法说明")
            dialog.resize(780, 620)
            layout = QtWidgets.QVBoxLayout(dialog)
            tabs = QtWidgets.QTabWidget()
            pages = (
                ("快速开始", """
                <h2>三步完成</h2>
                <ol><li>在 Maya 选择摄像机，点击“获取所选摄像机”。</li>
                <li>可选：选择一个或多个被摄主体，点击“添加所选目标”。</li>
                <li>确认播放范围与异常容差，点击“自动描述运镜”。</li></ol>
                <p><b>中文提示词</b>用于 AI 视频网站，可直接编辑；<b>结构化详情</b>用于后续 Maya 工具。</p>
                """),
                ("计算内容", """
                <h2>算法读取什么</h2>
                <p>逐帧读取摄像机世界矩阵，再转换到摄像机局部坐标，分别计算推进、横移、升降、横摇、俯仰、滚转与变焦。</p>
                <p>提示词只突出一到两个主要空间运动；结构化详情会保留全部通道、每帧步长、速度趋势和单位。</p>
                <p>选择主体后，中心用于距离和环绕关系；所有网格的整体包围盒用于判断完整入画、部分出画或画面外。</p>
                """),
                ("容差与限制", """
                <h2>异常容差</h2>
                <p>滑杆只控制“突然偏离且下一帧返回”的单帧异常。数值越大越宽容；关闭过滤后，候选异常仍会记录，但参与分析。</p>
                <h2>分段合并强度</h2>
                <p>控制中文提示词的段落数量：数值越高，相邻运动越倾向合并（段落更少）；数值越低，越容易拆开（段落更多）。完全换成另一种运动（如静止变推进）在任意强度下都会拆开。调整后只重排提示词，不会重新分析场景。</p>
                <h2>当前限制</h2>
                <p>本版按整数帧采样；包围盒构图是透视近似，不判断物体遮挡、镜头畸变或画面边缘精确相交。复杂结果仍需人工复检。</p>
                """),
            )
            for name, html in pages:
                browser = QtWidgets.QTextBrowser()
                browser.setHtml(html)
                tabs.addTab(browser, name)
            layout.addWidget(tabs)
            close_button = QtWidgets.QPushButton("关闭")
            close_button.clicked.connect(dialog.close)
            layout.addWidget(close_button)
            dialog.finished.connect(lambda _result: setattr(self, "_help_window", None))
            self._help_window = dialog
            dialog.show()

        def _pick_camera(self):
            """“获取所选摄像机”:读取 Maya 当前选择中的摄像机并填入文本框。"""

            try:
                transform, _ = camera_from_selection()
                changed = transform != self._camera
                self._camera = transform
                self.camera_field.setText(transform.rsplit("|", 1)[-1])
                if changed:
                    self._invalidate_report()
                    self.status_label.setText("已获取摄像机，请重新分析")
                else:
                    self.status_label.setText("已获取摄像机")
            except Exception as error:
                self._show_error(str(error))

        def _load_initial_selection(self):
            """打开窗口时静默预填:若 Maya 中已有选择,直接读取摄像机与目标。"""

            try:
                transform, _ = camera_from_selection()
            except Exception:
                return
            self._camera = transform
            self.camera_field.setText(transform.rsplit("|", 1)[-1])
            try:
                for target in targets_from_selection(transform):
                    if target not in self._targets:
                        self._targets.append(target)
                        self.target_list.addItem(target.rsplit("|", 1)[-1])
            except Exception:
                pass
            self.status_label.setText(
                f"已获取摄像机；当前目标数量：{len(self._targets)}"
            )

        def _add_targets(self):
            """“添加所选目标”:把 Maya 当前选择中的非相机对象加入目标列表。"""

            try:
                before = len(self._targets)
                for target in targets_from_selection(self._camera or None):
                    if target not in self._targets:
                        self._targets.append(target)
                        self.target_list.addItem(target.rsplit("|", 1)[-1])
                if len(self._targets) != before:
                    self._invalidate_report()
                self.status_label.setText(f"当前目标数量：{len(self._targets)}")
            except Exception as error:
                self._show_error(str(error))

        def _remove_targets(self):
            """“移除选中目标”:删除列表中选中的行(行号从大到小删,避免下标偏移)。"""

            rows = sorted(
                {self.target_list.row(item) for item in self.target_list.selectedItems()},
                reverse=True,
            )
            for row in rows:
                self.target_list.takeItem(row)
                self._targets.pop(row)
            if rows:
                self._invalidate_report()
                self.status_label.setText(f"当前目标数量：{len(self._targets)}")

        def _clear_targets(self):
            """“清空目标”:清空列表控件与内部记录,并作废旧报告。"""

            had_targets = bool(self._targets)
            self._targets = []
            self.target_list.clear()
            if had_targets:
                self._invalidate_report()
                self.status_label.setText("目标已清空，请重新分析")

        def _refresh_range(self):
            """“刷新范围”:读取 Maya 播放范围并显示;失败时把错误文字显示在范围行。"""

            try:
                start, end = playback_frame_range()
                self.range_label.setText(f"第 {start}–{end} 帧（逐帧采样）")
            except Exception as error:
                self.range_label.setText(str(error))

        def _set_progress(self, current, total):
            """采样进度回调:刷新进度条;每 10 帧让出一次事件循环防界面卡死。"""

            try:
                self.progress.setValue(round(current / total * 100))
                # processEvents:让 Qt 处理重绘 / 点击事件,长任务期间界面不假死
                if current == total or current % 10 == 0:
                    QtWidgets.QApplication.processEvents()
            except RuntimeError:
                # 极端情况下窗口已被销毁:停止刷新界面,让分析继续跑完并返回结果
                return

        def _highlight_display_warnings(self):
            """把“【复检提示】”行整行标红(只改显示格式,不动文本内容)。

            用 ExtraSelection 在提示行上叠加红色前景;
            文本本身保持原样,复制时由 strip_display_warnings 整行剔除。
            """

            text = self.prompt_edit.toPlainText()
            selections = []
            position = 0
            for line in text.splitlines():
                line_start = position
                position += len(line) + 1  # 追加换行符的长度
                if not line.startswith(DISPLAY_WARNING_PREFIX):
                    continue
                # 为每一处提示行构造“额外选区”,从行首到行尾套上红色前景格式
                cursor = self.prompt_edit.textCursor()
                cursor.setPosition(line_start)
                cursor.setPosition(
                    line_start + len(line),
                    QtGui.QTextCursor.MoveMode.KeepAnchor,
                )
                selection = QtWidgets.QTextEdit.ExtraSelection()
                selection.cursor = cursor
                selection.format = QtGui.QTextCharFormat()
                selection.format.setForeground(QtGui.QColor("red"))
                selections.append(selection)
            self.prompt_edit.setExtraSelections(selections)

        def _analyze(self):
            """执行分析:逐帧采样 → 纯 Python 分析 → 回填提示词与结构化详情。"""

            if not self._camera:
                self._show_error("请先选择摄像机并点击“获取所选摄像机”。")
                return
            if self._running:
                return
            self._set_busy(True)  # 分析期间禁用会改变输入的控件,并挡住关窗
            self.progress.setValue(0)
            self.status_label.setText("正在逐帧读取 Maya 场景……")
            try:
                start, end = playback_frame_range()
                # 范围标签跟随本次实际分析范围,避免与 Maya 时间滑块脱节
                self.range_label.setText(f"第 {start}–{end} 帧（逐帧采样）")
                report = analyze_maya_camera(
                    camera=self._camera,
                    targets=self._targets,
                    start_frame=start,
                    end_frame=end,
                    progress_callback=self._set_progress,
                    config=AnalysisConfig(
                        filter_spikes=self.filter_spikes.isChecked(),
                        spike_multiplier=self.spike_tolerance.value(),
                    ),
                )
                # 记住本次结果:分段滑杆变化时据此即时重排提示词,不再读场景
                self._last_report = report
                self._last_has_targets = bool(self._targets)
                # 提示词与结构化 JSON 由同一份报告按当前合并强度渲染
                phase_count = self._refresh_prompt_from_report()
                warning_count = len(report.warnings)
                self.status_label.setText(
                    f"分析完成：{len(report.segments)} 个运动段、"
                    f"{phase_count} 个提示段落、{warning_count} 条警告"
                )
            except Exception as error:
                traceback.print_exc()
                self._show_error(str(error))
            finally:
                self._set_busy(False)

        def _copy_prompt(self):
            """“复制提示词”:剔除红色复检提示后写入系统剪贴板。"""

            text = strip_display_warnings(self.prompt_edit.toPlainText())
            QtWidgets.QApplication.clipboard().setText(text)
            self.status_label.setText("提示词已复制")

        def _show_error(self, message):
            """统一错误提示:状态栏更新 + 模态警告框。"""

            self.status_label.setText("操作失败")
            QtWidgets.QMessageBox.warning(self, "摄像机运镜描述器", message)

    return CameraMotionDescriberWindow


def show():
    """打开描述器窗口(包入口 camera_motion_describer.show() 调用此处)。

    单例处理:已有窗口则先关闭;新窗口以 Maya 主窗口为 parent,
    WA_DeleteOnClose 让关闭时自动释放,避免残留 C++ 对象。
    """

    global _WINDOW
    QtCore, _, _, _, _ = _qt_modules()
    if _WINDOW is not None:
        try:
            if getattr(_WINDOW, "_running", False):
                # 分析进行中:不重建窗口,直接把现有窗口提到前台
                _WINDOW.show()
                _WINDOW.raise_()
                _WINDOW.activateWindow()
                return _WINDOW
            _WINDOW.close()
            _WINDOW.deleteLater()  # 交给 Qt 事件循环销毁,防止残留 C++ 对象
        except RuntimeError:
            _WINDOW = None  # 旧窗口已被 Qt 释放,直接从新窗口开始
    window_class = _build_window_class()
    _WINDOW = window_class(parent=maya_main_window())
    _WINDOW.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
    _WINDOW.show()
    # 置顶并激活,确保窗口出现在 Maya 主窗口前面
    _WINDOW.raise_()
    _WINDOW.activateWindow()
    return _WINDOW
