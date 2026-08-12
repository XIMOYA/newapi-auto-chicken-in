"""三套 AI 视觉 prompt，全部强制 JSON 输出。

分工很明确：
  - 状态分类：替代脆弱的硬编码选择器，告诉流程「现在到哪一步了」
  - 坐标定位：Turnstile 是跨域 iframe，勾选框位置会变，用局部图定位精度最高
  - 字符识别：站点自带的图形验证码
"""

from __future__ import annotations

# 页面状态枚举（代码与 prompt 共用同一份定义，避免两边漂移）
PASSED = "passed"
CF_WAITING = "cf_waiting"
TURNSTILE_CHECKBOX = "turnstile_checkbox"
IMAGE_CAPTCHA = "image_captcha"
GRID_CAPTCHA = "grid_captcha"
LOGIN_REQUIRED = "login_required"
RATE_LIMITED = "rate_limited"
ERROR_PAGE = "error_page"
UNKNOWN = "unknown"

ALL_STATES = (
    PASSED, CF_WAITING, TURNSTILE_CHECKBOX, IMAGE_CAPTCHA, GRID_CAPTCHA,
    LOGIN_REQUIRED, RATE_LIMITED, ERROR_PAGE, UNKNOWN,
)

STATE_PROMPT = """你是网页状态判别器。观察这张网页截图，判断它当前处于哪一种状态。

状态枚举及判定要点：
- passed: 正常的站点页面（有导航栏、内容区、按钮等真实业务界面），没有任何验证拦截
- cf_waiting: Cloudflare 正在自动校验，画面上是 "Just a moment"、"正在验证您是否是真人"、
  转圈动画或空白过渡页，没有需要人点的控件
- turnstile_checkbox: 画面上有一个 Cloudflare Turnstile / "确认您是真人" 的复选框需要点击
- image_captcha: 有一张字符验证码图片和一个输入框，需要识别字符后填入
- grid_captcha: 需要在多张小图里点选符合描述的图块
- login_required: 停在登录/注册表单，需要账号密码
- rate_limited: 显示被封禁、限流、Access denied、Error 1015/1020 之类
- error_page: 其它错误页（502、404、站点维护等）
- unknown: 截图空白或无法判断

只输出一个 JSON 对象，不要任何解释、不要 markdown 围栏：
{"state": "<上面某个枚举值>", "confidence": <0 到 1 的小数>, "reason": "<30 字以内依据>"}"""

LOCATE_PROMPT = """这是网页上某个区域的截图，宽 {width} 像素，高 {height} 像素，左上角是 (0,0)。

请找出 {target} 的中心点位置。

只输出一个 JSON 对象，不要任何解释、不要 markdown 围栏：
{{"found": true, "x": <0 到 1 的小数>, "y": <0 到 1 的小数>, "reason": "<20 字以内>"}}

x 是横向位置占图片宽度的比例，y 是纵向位置占图片高度的比例，都必须是归一化比例而不是像素值。
如果图中确实没有这个目标，输出 {{"found": false}}"""

GRID_PROMPT = """这是一个点选式验证码的截图，宽 {width} 像素，高 {height} 像素，左上角是 (0,0)。

题目要求：{target}

请依次给出需要点击的每个位置的中心点。

只输出一个 JSON 对象，不要任何解释、不要 markdown 围栏：
{{"found": true, "points": [{{"x": <0-1 小数>, "y": <0-1 小数>}}]}}

x/y 均为占图片宽高的归一化比例。找不到目标时输出 {{"found": false}}"""

OCR_PROMPT = """这是一张字符验证码图片。识别图中的字符。

规则：
- 严格区分大小写，保留原样
- 不要输出空格、标点或任何解释文字
- 如果是算式（例如 "3+5="），输出计算结果

只输出一个 JSON 对象，不要 markdown 围栏：
{"text": "<识别结果>"}"""

# 各任务的目标描述，供 LOCATE_PROMPT / GRID_PROMPT 填空
TARGET_TURNSTILE = "Cloudflare 人机验证的方形复选框（通常在文字左侧，是一个待勾选的小方框）"
TARGET_CAPTCHA_INPUT = "验证码输入框"
