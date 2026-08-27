<!--
站点Cookie签到原理.md
文档：New API 系站点「站点 Cookie」登录方式（login_method: newapi_cookie）的签到技术原理
职责：
- 记录这条链路的凭据形态（完整 Cookie 头）与它和 TaBiAI 单值凭据的本质差别
- 记录 New API 的两个鉴权硬要求：先取 user id、之后每请求都带 New-Api-User
- 记录签到路径在各 fork 间不统一、只能候选探测的事实与探测规则
- 记录判定为什么只能读响应体的 success，以及各状态的归类依据
- 给出 S0~S5 策略链在这条路上的实际流转，作为写脚本与排查的依据
说明：
- 本文依据是仓库内的实现（newapi_checkin/client.py、config.py、runner.py）
  与其中记录的既往验证结论，**不是新一轮抓包实测**。
- 需要精确到响应片段的实测证据请看 docs/签到原理.md（那份针对 TaBiAI）。
  两份文档定位不同：这一份讲「一类站点的通用协议」，那一份讲「一个站点的具体协议」。
-->

# New API 站点 Cookie 签到原理

## 一、适用范围

`login_method: newapi_cookie`，覆盖绝大多数 New API / One API 系的中转站。

| 项 | 说明 |
|---|---|
| 凭据 | 浏览器里那一整条 Cookie 头（`session=...; other=...`） |
| 鉴权模型 | Cookie 会话 + `New-Api-User` 头指明操作哪个用户 |
| 签到接口 | 路径**不统一**，按候选列表探测 |
| 人机验证 | 多数站点没有；个别 fork 开了 Turnstile，走盾类重试 |
| 与 TaBiAI 的关系 | 同一个上游的两种鉴权形态，见第九节对照 |

判断一个站点该用哪种：**凭据里有 `new_api_refresh` 就是 `tabiai`，否则是
`newapi_cookie`**。平台侧的 Cookie 检测也是按这个特征分流的
（`server/cookie_checker.go` 里 `strings.Contains(account.Cookie, "new_api_refresh=")`）。

## 二、凭据形态

这条路的凭据是**整条 Cookie 头**，不是单个 token：

```
session=MTc2ODk0...; _ga=GA1.1...; other=xxx
```

从浏览器 F12 → Network → 任意 `/api/` 请求 → Request Headers → Cookie 整条复制。

解析规则（`client.parse_cookie_header`）：按 `;` 切分，每段按**第一个** `=` 分割成
名/值，两侧空白剔掉，不含 `=` 的段直接丢弃。所以多复制一个分号、少一个空格都无所谓，
但**值里如果本身带 `=`（base64 常见）不会被截断** —— 用的是 `partition` 而不是 `split`。

### 为什么整条而不是只要 session

站点可能同时校验多个 cookie（会话本体 + CSRF + 反爬标记），而哪些是必需的因 fork
而异。整条带上是最省心的做法，代价只是多几百字节。

### 一个已经踩过的坑：非 latin-1 字符

拼 Cookie 头时每个名和值都要过 `sanitize_header_value` 清洗。原因是 `curl_cffi` 在
`headers.update` 时会按 latin-1 编码，cookie 里混进一个中文或全角字符就直接抛
`UnicodeEncodeError` —— 而且抛在构造阶段，报错信息跟 cookie 毫无关系，极难定位。
清洗后该字符被丢弃，请求照常发出（可能鉴权失败，但那是能看懂的失败）。

## 三、两个鉴权硬要求

New API 的接口不是「带上 cookie 就能用」。有两步，顺序不能颠倒：

```
1. GET /api/user/self          → 从 data.id 拿到用户数字 id
2. 之后每个请求都带 New-Api-User: <id> 头
```

漏掉第 2 步的表现是**接口返回未登录**，即使 cookie 完全有效 —— 这是最常见的
「凭据明明是对的却签不上」的原因。

`/api/user/self` 的成功响应形状：

```json
{
  "success": true,
  "data": { "id": 12345, "username": "kiq", "quota": 2500000 }
}
```

拿不到 `data.id` 时这一轮直接判失败，不会继续往下打签到接口 —— 没有 id 就构造不出
必需的头，硬发只会得到一个看不懂的未登录错误。

### user_id 会被缓存

首次探到的 id 写进 `sessions.json`，下次直接复用，省掉一次 `self` 请求。配置里也可以
手填 `user_id` 预置。但缓存只是加速：id 对不上时接口会拒，下一轮重新探。

## 四、签到接口

### 4.1 路径不统一，只能探

各 fork 改过这个路径，没有统一约定。按候选列表顺序试：

```
/api/user/checkin      ← 最常见
/api/user/check_in
/api/user/sign_in
```

**HTTP 404 / 405 / 501 一律视为「路径不对」，继续试下一个**，不当成失败。405 也算是
因为有的 fork 那个路径存在但只接受 GET，说明签到不在这儿。

账号配了 `checkin_path` 时**只试这一个**，不再遍历 —— 显式配置就是明确意图，
继续探反而会在错误路径上留下无意义的请求记录。探到可用路径后由上层写回缓存，
下一轮直接命中。

### 4.2 判定读响应体，不读状态码

这是必须守住的一条：**签到成功与业务失败都可能返回 HTTP 200**。判定一律看
响应体的 `success` 字段。

只看状态码的后果是把「今日已签到」当成成功、把「凭据失效」当成成功，
最后汇总邮件全绿而实际一个都没签上。

## 五、状态归类

`success: true` 之外的情况按响应体 `message` 的关键词归类。顺序有讲究 ——
Turnstile 判定压在「已签到」之前，因为有的站点会在同一句里同时提到两者。

| 归类 | 触发条件 | 后续动作 |
|---|---|---|
| SUCCESS | `success: true` | 结束，记录奖励额度 |
| TURNSTILE_REQUIRED | message 含 `turnstile token` / `turnstile 校验失败` 等 | 走盾类重试：换 IP + 开浏览器 |
| ALREADY_DONE | message 含 `已签到` / `重复签到` / `already` / `checked in` 等 | 结束，算成功 |
| AUTH_FAILED | 状态码 401/403，或 message 含 `未登录` / `登录已过期` / `unauthorized` 等 | **直接跳过该账号**，不重试 |
| FAILED | 其他 `success: false` | 有限换 IP 重试（最多 5 次） |
| UNKNOWN | 响应不是 JSON | 计次重试，可能是临时网关页 |

关键词表是中英双语的（`_ALREADY_MARKERS` / `_AUTH_MARKERS` / `_TURNSTILE_MARKERS`），
因为各 fork 的提示语言不一致，同一个站点还可能随 `Accept-Language` 变。

### 为什么 AUTH_FAILED 不重试

凭据失效换多少个 IP、重开多少次浏览器都是同样结果，重试纯属浪费额度和时间。
这类账号直接标记跳过，让人去更新凭据 —— 汇总邮件里会带上原因。

## 六、额度：两个 quota 别混

| 来源 | 字段 | 含义 |
|---|---|---|
| `GET /api/user/self` | `data.quota` | **账户剩余额度** |
| 签到接口响应 | `quota` / `data.quota` 等 | **本次签到奖励** |

两者都是站点内部的整数单位，不是美元。换算率从 `GET /api/status` 的
`data.quota_per_unit` 取，探不到时退回 `500000`（New API 上游默认值）。

`/api/status` 是**公开接口，不需要鉴权**。探它失败一律当「没探到」处理 ——
被盾拦、非 JSON、字段缺失都不该冒泡成签到错误，那只影响金额显示。

不同 fork 的换算率**不一定相同**，所以不能写死。展示成 `$` 时按
`quota / quota_per_unit` 算。

## 七、Cloudflare 与 cookie 合并

多数中转站挂着 Cloudflare。过盾拿到的 `cf_clearance` 等 cookie 要和账号自己的
cookie 合起来发：

```
账号 cookie 为底  →  过盾得到的 cookie 覆盖同名项
```

覆盖方向是刻意的：`cf_clearance` / `__cf_bm` 有时效，浏览器刚拿到的那份一定比账号
配置里那份新。反过来用旧值覆盖新值会立刻被盾拦回去。

### cf_clearance 和 UA 是绑定的

Cloudflare 签发 `cf_clearance` 时把 User-Agent 一起算进去了。所以缓存会话时
**必须把当时的 UA 一起存下来**，之后带这个 cookie 发请求时原样带回那个 UA。
换了 UA 等于换了指纹，cookie 当场失效。

出口 IP 同理 —— 这就是缓存判定里要比对 IP 与代理的原因。

## 八、完整链路（S0~S5）

```
S0  用缓存的 cf_clearance + UA + 出口 IP 直发 HTTP
    命中且没过期 → 一次请求签完，几乎零成本
      ↓ 缓存不可用 / 被拒
S1  curl_cffi 伪装 TLS/JA3 指纹裸发
    多数没盾或盾很轻的站点到这里就成了
      ↓ 命中 Cloudflare
S2  起浏览器等自解，收割 cookie 回来重发
      ↓ 自解不过
S3  先按预设位置点复选框，没生效才让 AI 看截图代为点选
      ↓ 拿到 cookie 但搬回 HTTP 不灵
S4  不搬 cookie，直接在浏览器页面里发签到请求
      ↓ 全都不行
S5  人工兜底：有头浏览器等你手动过，过完存 profile
```

S4 值得单说：S2/S3 过盾成功后，浏览器拿到的 cookie 搬回 `curl_cffi` 还灵不灵是
**不确定**的 —— 指纹环境变了，Cloudflare 完全可能不认。S4 绕开这个不确定性，
既然浏览器已经过了盾，就在同一个上下文里把签到发掉。浏览器内直发同样要带
`New-Api-User` 头，判定复用同一套归类函数，不会出现两条路结论不一致。

### 各状态怎么进重试

| 结果 | 重试策略 |
|---|---|
| NETWORK_ERROR | 只要池子还能给新 IP 就无限换，换不到就跳过 |
| CF_BLOCKED / TURNSTILE_REQUIRED | 换 IP + 重开浏览器，次数不限（有时间盒兜底） |
| FAILED / WAF_BLOCKED | 最多额外换 5 个 IP，每次等 5 秒 |
| AUTH_FAILED / LOGIN_REQUIRED / UNKNOWN | 直接跳过，不浪费重试 |
| SUCCESS / ALREADY_DONE | 结束 |

一个账号累计换满 10 个 IP 后，换 IP 会优先复用本轮已签到成功过的出口 ——
盾拦的往往是某个具体 IP 的声誉，换成已证明可用的地址往往直接就过。

## 九、与 TaBiAI 的差异对照

同一个上游的两种鉴权形态，写脚本时最容易混的几处：

| | newapi_cookie | tabiai |
|---|---|---|
| 凭据 | 整条 Cookie 头 | 单值 `new_api_refresh=sid.secret` |
| 凭据寿命 | 会话有效期内不变 | **每次 refresh 就换一代** |
| 鉴权头 | `Cookie` + `New-Api-User` | `Authorization: Bearer <短期 token>` |
| 取 token | 不需要 | 必须先 `POST /api/user/auth/refresh` 换 |
| cookie 作用域 | 全站 `/api/*` | **只在 `/api/user/auth` 路径下生效** |
| 签到路径 | 三个候选轮探 | 固定 `/api/user/checkin` |
| Turnstile | 多数不需要 | **强校验，必须有真实浏览器 token** |
| 凭据要落盘吗 | 不用 | **必须**，漏存下轮就被判重放、整条会话撤销 |
| 失效能自救吗 | 不能，要人工更新 | 能，填了 `github_user_session` 可自动重新签发 |

最危险的混淆是**代次轮转**：`newapi_cookie` 的 cookie 可以反复用，而 `tabiai` 的
凭据用一次就换一代，旧代超过分钟级宽限窗口再用会被判重放、**整条会话被撤销**。
把 tabiai 账号当 newapi_cookie 处理（不落盘轮转值）会在第二天直接打死账号。

## 十、写脚本必须守住的几条

1. **先 `self` 再签到，之后每请求都带 `New-Api-User`** —— 漏了就是「凭据对却未登录」
2. **判定读 `success` 不读状态码** —— 业务失败也返回 200
3. **404/405/501 是路径不对不是失败** —— 继续试下一个候选
4. **cookie 头每个名值都要过 latin-1 清洗** —— 否则一个中文字符炸在构造阶段
5. **cf_clearance 必须和当时的 UA、出口 IP 一起缓存** —— 三者绑定，换一个就失效
6. **过盾拿到的 cookie 覆盖账号 cookie 的同名项** —— 方向反了会立刻被盾拦回
7. **凭据失效不要重试** —— 换 IP 换浏览器都是同样结果，直接报出来让人更新
8. **`quota_per_unit` 不能写死** —— 各 fork 不一样，从 `/api/status` 探

## 十一、本文依据

| 内容 | 依据 |
|---|---|
| 鉴权两步、候选路径、状态归类 | `newapi_checkin/client.py` |
| 路径常量、换算率默认值、登录方式别名 | `newapi_checkin/config.py` |
| 策略链流转与重试分级 | `newapi_checkin/runner.py` |
| cookie 合并与 latin-1 清洗 | `newapi_checkin/client.py` 的 `merge_cookies` / `build_cookie_header` |
| 平台侧按凭据特征分流 | `server/cookie_checker.go` |
| TaBiAI 一侧的对照 | `docs/签到原理.md`（那份有 2026-08-17 的实测响应片段） |

本文**没有**新一轮抓包记录：站点 Cookie 这条路覆盖的是一类站点而不是某一个，
逐个抓包既不现实也没意义。上表里的每条结论都能在代码里找到对应实现，
其中「鉴权两步」这一条在 `client.py` 顶部注释里被标注为沿用原 JS 脚本已验证的结论。
