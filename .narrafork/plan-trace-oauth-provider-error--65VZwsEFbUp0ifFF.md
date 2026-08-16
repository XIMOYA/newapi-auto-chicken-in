# 修复“未知的 OAuth 提供商”启动旧二进制问题

## 诊断结论

源码当前已经使用参考流程：`GET /api/oauth/state?mode=login`，随后请求 GitHub authorize；Python/Go 测试也覆盖了该协议。

真正的问题是启动了旧的默认二进制 `server/newapi-config-server`：
- 旧文件包含 `flow_token`、`provider":"github`、`/api/status` 等已删除协议特征；
- 新构建的 `server/newapi-config-server-linux-amd64` 不包含这些特征。

因此“未知的 OAuth 提供商”来自旧二进制仍发送旧 OAuth 请求，不是当前源码逻辑。

## 实施步骤

1. 使用当前 `main` 源码重新执行统一构建脚本，重新生成前端并嵌入 Go 服务。
2. 同时生成两个输出名：
   - `server/newapi-config-server`
   - `server/newapi-config-server-linux-amd64`
3. 用 `file`、哈希及二进制特征检查确认两个文件均为 Linux amd64，且不再包含旧 OAuth 协议字符串。
4. 复跑必要的 Go/Python/Web 验证，确认构建产物与源码协议一致。
5. 不提交构建产物，不 push；保留现有未跟踪文件状态。
