"""人性化鼠标轨迹。

Camoufox 开 humanize=True 时浏览器层已经做了这件事，这里主要服务 Patchright 分支：
三次贝塞尔曲线 + 分步移动 + 随机停顿，避免出现「瞬移直线到目标」这种机器特征。
"""

from __future__ import annotations

import random
import time
from typing import List, Tuple


def bezier_path(start: Tuple[float, float], end: Tuple[float, float],
                steps: int = 20) -> List[Tuple[float, float]]:
    """生成一条带随机弯曲的三次贝塞尔路径。"""
    x0, y0 = start
    x3, y3 = end
    dx, dy = x3 - x0, y3 - y0
    distance = max(1.0, (dx * dx + dy * dy) ** 0.5)
    # 控制点在直线两侧随机偏移，偏移量随距离放大但有上限
    spread = min(120.0, distance * 0.35)
    x1 = x0 + dx * random.uniform(0.2, 0.4) + random.uniform(-spread, spread)
    y1 = y0 + dy * random.uniform(0.2, 0.4) + random.uniform(-spread, spread)
    x2 = x0 + dx * random.uniform(0.6, 0.8) + random.uniform(-spread, spread)
    y2 = y0 + dy * random.uniform(0.6, 0.8) + random.uniform(-spread, spread)

    points: List[Tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1 - t
        x = (mt ** 3) * x0 + 3 * (mt ** 2) * t * x1 + 3 * mt * (t ** 2) * x2 + (t ** 3) * x3
        y = (mt ** 3) * y0 + 3 * (mt ** 2) * t * y1 + 3 * mt * (t ** 2) * y2 + (t ** 3) * y3
        points.append((x, y))
    return points


def move_mouse_human(page, x: float, y: float, steps: int = None) -> None:
    """从当前位置沿贝塞尔曲线移动到目标点，每步之间随机停顿。"""
    steps = steps or random.randint(15, 25)
    try:
        start = page.evaluate("() => [window.innerWidth * 0.5, window.innerHeight * 0.85]")
        origin = (float(start[0]), float(start[1]))
    except Exception:  # noqa: BLE001
        origin = (x - 200.0, y + 200.0)

    for px, py in bezier_path(origin, (x, y), steps):
        try:
            page.mouse.move(px, py)
        except Exception:  # noqa: BLE001 - 页面可能正在跳转
            return
        time.sleep(random.uniform(0.008, 0.025))
    # 点击前的悬停停顿
    time.sleep(random.uniform(0.08, 0.20))


def human_pause(low: float = 0.4, high: float = 1.2) -> float:
    delay = random.uniform(low, high)
    time.sleep(delay)
    return delay
