"""摄像机分析内核使用的纯数据定义(不导入 Maya,可在普通 Python 中使用)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

Vec3 = Tuple[float, float, float]
Quaternion = Tuple[float, float, float, float]


@dataclass(frozen=True)
class CameraSample:
    """相机在某一帧的求值状态(位置、朝向与镜头参数)。

    orientation 是与 Maya 兼容的四元数,顺序为 (x, y, z, w)。
    Maya 相机沿自身局部 -Z 方向观察(即“前方”是 -Z),
    因此世界位移转到相机局部坐标后:局部 X = 右、Y = 上、-Z = 前。
    """

    frame: float                    # 帧号(采样时间,可能含小数帧)
    position: Vec3                  # 世界空间位置
    orientation: Quaternion         # 世界空间旋转(四元数,(x, y, z, w))
    focal_length: float             # 焦距(mm)
    horizontal_fov: Optional[float] = None   # 水平视场角(度)
    vertical_fov: Optional[float] = None     # 垂直视场角(度)
    f_stop: Optional[float] = None           # 光圈值
    focus_distance: Optional[float] = None   # 对焦距离
    shutter_angle: Optional[float] = None    # 快门角度(度)
    depth_of_field: bool = False             # 是否开启景深
    target_center: Optional[Vec3] = None     # 被摄主体整体中心(世界坐标,可选)
    target_bounds_min: Optional[Vec3] = None # 所有主体网格的整体包围盒最小点
    target_bounds_max: Optional[Vec3] = None # 所有主体网格的整体包围盒最大点


@dataclass(frozen=True)
class AnalysisConfig:
    """运动分析的判定阈值(用户可调)。

    所有阈值集中在这里便于统一调整灵敏度,
    分析代码只从这里读,不散落魔法数字。
    """

    fps: float = 24.0                          # 帧率,仅用于提示词中的秒数换算
    position_epsilon: float = 1.0e-5           # 位置判定的绝对下限(世界单位)
    relative_position_epsilon: float = 1.0e-5  # 相对下限 = 相机路径包围盒对角线 × 该系数
    rotation_epsilon_degrees: float = 0.02     # 旋转判定的最小角度(度)
    focal_epsilon_mm: float = 0.005            # 焦距判定的最小变化(mm)
    spike_multiplier: float = 8.0              # 单帧毛刺阈值 = 稳健中心 + 倍数 × 噪声
    filter_spikes: bool = True                 # False: 保留突变数据,只提示候选异常
    spike_return_ratio: float = 0.30           # “去而复返”毛刺的恢复比例上限
    bridge_gap_frames: int = 2                 # 可被前后相同标签“桥接”的最长抖动帧数
    minimum_segment_frames: int = 4            # 有效段最短帧数,短于此尝试并入邻居
    maximum_spatial_motions: int = 2           # 每个时间段最多保留的空间运动数量
    target_aim_tolerance_degrees: float = 8.0  # 判定“持续对准主体”的角度容差
    screen_center_threshold: float = 0.18      # 判定主体位于画面“中央”的归一化阈值
    orbit_minimum_degrees: float = 3.0         # 判定环绕所需的最小观察角变化(度)
    world_up: Vec3 = (0.0, 1.0, 0.0)           # 世界上方向(定环绕左右用;Z 轴场景传 (0,0,1))


@dataclass(frozen=True)
class MotionAnomaly:
    """分析时被忽略的单帧异常(只提示人工复检,不会改写原始采样)。"""

    frame: float        # 异常发生帧号
    kind: str           # 异常类型:position_spike / rotation_spike / focal_spike
    magnitude: float    # 偏离程度(位置距离 / 角度 / 焦距差)
    message: str        # 中文说明,直接展示给用户


@dataclass(frozen=True)
class MotionSegment:
    """一段连续运动的分析结果(帧区间 + 运动标签 + 速度 + 度量)。"""

    start_frame: float          # 段起始帧
    end_frame: float            # 段结束帧
    motions: Tuple[str, ...]    # 运动标签元组,如 ("dolly_in", "pan_left", "zoom_in")
    speed_profile: str          # 速度曲线:steady / accelerating / decelerating / ease_in_out / variable
    confidence: float           # 置信度 0~1(复杂运动段会降低)
    target_aimed: bool = False  # 本段是否持续朝向被摄主体
    complex_motion: bool = False  # 是否包含未完全展开的复杂复合运动
    metrics: Dict[str, object] = field(default_factory=dict)       # 各轴累计、参数起止/极值等
    target_relation: Dict[str, object] = field(default_factory=dict)  # 与主体的关系(距离/视角/构图等)


@dataclass(frozen=True)
class MotionReport:
    """一次完整分析的产物:时间段、异常、警告与中文提示词。"""

    camera_name: str                # 摄像机短名
    start_frame: float              # 分析起始帧
    end_frame: float                # 分析结束帧
    fps: float                      # 分析使用的帧率
    target_names: Tuple[str, ...]   # 被摄主体短名
    segments: Tuple[MotionSegment, ...]   # 运动时间段
    anomalies: Tuple[MotionAnomaly, ...]  # 被忽略的单帧异常
    warnings: Tuple[str, ...]       # 需要人工复检的警告
    prompt: str                     # 中文提示词(含显示用红字“【复检提示】”行,复制时剔除)
    metadata: Dict[str, object]     # 采样数量、阈值、全程参数等元数据

    def to_dict(self) -> Dict[str, object]:
        """转成 JSON 友好的字典(供界面“结构化详情”页签展示)。"""

        return {
            "camera_name": self.camera_name,
            "frame_range": [self.start_frame, self.end_frame],
            "fps": self.fps,
            "target_names": list(self.target_names),
            "segments": [
                {
                    "frame_range": [segment.start_frame, segment.end_frame],
                    "motions": list(segment.motions),
                    "speed_profile": segment.speed_profile,
                    "confidence": segment.confidence,
                    "target_aimed": segment.target_aimed,
                    "complex_motion": segment.complex_motion,
                    "metrics": dict(segment.metrics),
                    "target_relation": dict(segment.target_relation),
                }
                for segment in self.segments
            ],
            "anomalies": [
                {
                    "frame": item.frame,
                    "kind": item.kind,
                    "magnitude": item.magnitude,
                    "message": item.message,
                }
                for item in self.anomalies
            ],
            "warnings": list(self.warnings),
            "prompt": self.prompt,
            "metadata": dict(self.metadata),
        }
