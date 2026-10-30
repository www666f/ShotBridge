"""轻量向量 / 四元数工具(零第三方依赖,可在普通 Python 中使用)。

约定与 Maya 保持一致:
- 四元数顺序为 (x, y, z, w);
- 相机沿自身局部 -Z 方向观察;
- 世界向量转到相机局部 = 用相机旋转的共轭去旋转该向量。
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence, Tuple

from .models import Quaternion, Vec3


def add(a: Vec3, b: Vec3) -> Vec3:
    """逐分量相加。"""
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def subtract(a: Vec3, b: Vec3) -> Vec3:
    """逐分量相减(a - b)。"""
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(value: Vec3, amount: float) -> Vec3:
    """向量数乘。"""
    return (value[0] * amount, value[1] * amount, value[2] * amount)


def dot(a: Vec3, b: Vec3) -> float:
    """点积(用于投影长度、夹角余弦、方向同异判定)。"""
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec3, b: Vec3) -> Vec3:
    """叉积(本项目中用它的结果与世界上方向点积来判定环绕方向左右)。"""
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def length(value: Vec3) -> float:
    """向量长度(模)。"""
    return math.sqrt(dot(value, value))


def distance(a: Vec3, b: Vec3) -> float:
    """两点间欧氏距离。"""
    return length(subtract(a, b))


def normalize(value: Vec3) -> Vec3:
    """单位化;零向量守卫:长度过小时返回零向量,避免除零。"""
    magnitude = length(value)
    if magnitude <= 1.0e-12:
        return (0.0, 0.0, 0.0)
    return scale(value, 1.0 / magnitude)


def midpoint(a: Vec3, b: Vec3) -> Vec3:
    """两点中点(毛刺清洗时用作插值目标)。"""
    return scale(add(a, b), 0.5)


def clamp(value: float, low: float, high: float) -> float:
    """把数值限制在 [low, high] 区间(防浮点误差把 acos 入参推离 [-1,1])。"""
    return max(low, min(high, value))


def quaternion_normalize(value: Quaternion) -> Quaternion:
    """四元数单位化;零四元数守卫:退化为无旋转 (0,0,0,1)。"""
    magnitude = math.sqrt(sum(component * component for component in value))
    if magnitude <= 1.0e-12:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(component / magnitude for component in value)  # type: ignore[return-value]


def quaternion_conjugate(value: Quaternion) -> Quaternion:
    """四元数共轭(单位四元数下即逆旋转)。"""
    return (-value[0], -value[1], -value[2], value[3])


def quaternion_multiply(a: Quaternion, b: Quaternion) -> Quaternion:
    """四元数乘法(复合旋转:先 b 后 a,与 Maya 的旋转叠加规则一致)。"""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quaternion_dot(a: Quaternion, b: Quaternion) -> float:
    """四元数点积(判断两个旋转是否在同一半球用)。"""
    return sum(left * right for left, right in zip(a, b))


def quaternion_shortest(value: Quaternion, reference: Quaternion) -> Quaternion:
    """取与参考旋转夹角最小的等价表示(q 与 -q 旋转相同,选内积为正的那个,保证插值走短弧)。"""
    if quaternion_dot(value, reference) < 0.0:
        return tuple(-component for component in value)  # type: ignore[return-value]
    return value


def quaternion_slerp(a: Quaternion, b: Quaternion, amount: float) -> Quaternion:
    """球面线性插值:在 a 与 b 之间按比例取中间旋转(毛刺清洗时用 0.5 取中值)。

    cosine > 0.9995 时两者几乎重合,改用线性插值 + 归一化以避免除零。
    """
    a = quaternion_normalize(a)
    b = quaternion_shortest(quaternion_normalize(b), a)
    cosine = clamp(quaternion_dot(a, b), -1.0, 1.0)
    if cosine > 0.9995:
        blended = tuple(
            left + amount * (right - left) for left, right in zip(a, b)
        )
        return quaternion_normalize(blended)  # type: ignore[arg-type]
    angle = math.acos(cosine)
    sine = math.sin(angle)
    left_weight = math.sin((1.0 - amount) * angle) / sine
    right_weight = math.sin(amount * angle) / sine
    return tuple(
        left_weight * left + right_weight * right for left, right in zip(a, b)
    )  # type: ignore[return-value]


def quaternion_angle_degrees(a: Quaternion, b: Quaternion) -> float:
    """两个旋转之间的最短夹角(度)。abs(dot) 使 q 与 -q 得到同样结果。"""
    a = quaternion_normalize(a)
    b = quaternion_normalize(b)
    cosine = abs(clamp(quaternion_dot(a, b), -1.0, 1.0))
    return math.degrees(2.0 * math.acos(cosine))


def quaternion_rotate(value: Quaternion, vector: Vec3) -> Vec3:
    """用四元数旋转一个向量:q · v · q⁻¹(把向量当作实部为 0 的四元数)。"""
    q = quaternion_normalize(value)
    vector_quaternion = (vector[0], vector[1], vector[2], 0.0)
    result = quaternion_multiply(
        quaternion_multiply(q, vector_quaternion), quaternion_conjugate(q)
    )
    return (result[0], result[1], result[2])


def world_vector_to_camera_local(orientation: Quaternion, vector: Vec3) -> Vec3:
    """把世界向量转换到相机局部坐标系(用相机旋转的共轭去旋转)。"""
    return quaternion_rotate(quaternion_conjugate(orientation), vector)


def relative_rotation_vector_degrees(
    start: Quaternion, end: Quaternion
) -> Vec3:
    """求 start→end 的最短局部轴旋转,返回“轴 × 角度(度)”向量。

    分量含义(相机局部轴):X = tilt(俯仰), Y = pan(横摇), Z = roll(滚转)。
    相对旋转 = start⁻¹ · end;四元数虚部方向即旋转轴,模长对应半角正弦。
    """

    start = quaternion_normalize(start)
    end = quaternion_shortest(quaternion_normalize(end), start)
    relative = quaternion_normalize(
        quaternion_multiply(quaternion_conjugate(start), end)
    )
    if relative[3] < 0.0:
        relative = tuple(-component for component in relative)  # type: ignore[assignment]
    vector = (relative[0], relative[1], relative[2])
    sine_half = length(vector)
    if sine_half <= 1.0e-12:
        return (0.0, 0.0, 0.0)
    angle = 2.0 * math.atan2(sine_half, clamp(relative[3], -1.0, 1.0))
    axis = scale(vector, 1.0 / sine_half)
    return scale(axis, math.degrees(angle))


def angle_between_degrees(a: Vec3, b: Vec3) -> float:
    """两个向量的夹角(度);任一为零向量时返回 180(视为无法判断)。"""
    a_normal = normalize(a)
    b_normal = normalize(b)
    if length(a_normal) <= 1.0e-12 or length(b_normal) <= 1.0e-12:
        return 180.0
    return math.degrees(math.acos(clamp(dot(a_normal, b_normal), -1.0, 1.0)))


def bounding_diagonal(points: Iterable[Vec3]) -> float:
    """一组点轴对齐包围盒的对角线长度(用作“相对阈值”的尺度基准)。"""
    points = list(points)
    if not points:
        return 0.0
    minimum = [min(point[axis] for point in points) for axis in range(3)]
    maximum = [max(point[axis] for point in points) for axis in range(3)]
    return distance(tuple(minimum), tuple(maximum))  # type: ignore[arg-type]


def quaternion_from_axis_angle(axis: Vec3, degrees: float) -> Quaternion:
    """由轴 + 角度构造四元数(测试 / demo 辅助,布局与 Maya 相同)。"""

    axis = normalize(axis)
    half_angle = math.radians(degrees) * 0.5
    sine = math.sin(half_angle)
    return (
        axis[0] * sine,
        axis[1] * sine,
        axis[2] * sine,
        math.cos(half_angle),
    )
