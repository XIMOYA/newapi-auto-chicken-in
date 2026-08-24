<!--
web/src/views/ProxyPoolSettingsView.vue
页面：代理池配置（纯配置编辑）
职责：编辑 proxy_pool 模块（enabled/test_url/timeout/max_workers/max_proxies/ip_swap_limit/sources/后台刷新/回传开关）
- sources 为动态列表（增删）
- 代理列表 / 刷新 / 测速 已迁移到「代理管理」页（/proxies）
数据来源：GET/PUT /api/config
-->
<template>
  <div class="page-container">
    <config-card
      title="代理池配置"
      description="配置代理池：定期抓取、测试并维护可用隧道(proxy)列表，供签到任务自动换隧道使用。抓取/测速/列表请在「代理管理」页操作。"
      :loading="!configStore.config"
      :saving="saving"
      :updated-at="configStore.updatedAt"
      :show-reset="true"
      compact
      :dirty="isDirty"
      @save="handleSave"
      @reset="handleReset"
    >
      <n-form label-placement="top" :show-require-mark="false" class="settings-form">
        <n-form-item label="启用代理池">
          <n-switch v-model:value="form.enabled" />
          <span class="switch-tip">{{ form.enabled ? '已启用' : '已停用' }}</span>
        </n-form-item>
        <n-form-item label="隧道连通性测试 URL">
          <n-input v-model:value="form.test_url" placeholder="例如 https://agentrouter.org/" />
        </n-form-item>
        <n-form-item label="测试超时（秒）">
          <n-input-number v-model:value="form.timeout" :min="1" :max="120" class="num-input" />
        </n-form-item>
        <n-form-item label="最大并发测试数">
          <n-input-number v-model:value="form.max_workers" :min="1" :max="500" class="num-input" />
        </n-form-item>
        <n-form-item label="代理池容量上限（已废弃）">
          <n-input-number v-model:value="form.max_proxies" :min="1" :max="5000" class="num-input" />
          <span class="switch-tip">已不再生效：测通不设数量上限，保留仅为兼容旧配置</span>
        </n-form-item>
        <n-form-item label="单账号换 IP 次数上限（已废弃）">
          <n-input-number v-model:value="form.ip_swap_limit" :min="0" :max="50" class="num-input" disabled />
          <span class="switch-tip">网络异常时不限次数换 IP；换不到新 IP 或遇到源站/不可恢复问题才跳过。此字段仅兼容旧配置</span>
        </n-form-item>
        <n-form-item label="代理来源 Sources">
          <dynamic-string-list
            v-model="form.sources"
            placeholder="例如 https://proxylist.example.com/list.txt"
            add-label="添加代理来源"
          />
        </n-form-item>

        <n-divider>服务器端代理池（预取）</n-divider>
        <n-alert type="info" :bordered="false" class="server-proxy-tip">
          <template #icon><n-icon><server-outline /></n-icon></template>
          配置网站服务会定期抓取以上代理源、测通并保存可用列表。GitHub Actions 签到前可
          <n-text code>GET /api/proxies/available</n-text> 直接预取现成列表，省去现场抓取测通。
          抓取/测速进度请到「代理管理」页查看。
        </n-alert>
        <n-form-item label="后台刷新间隔（分钟）">
          <n-input-number v-model:value="form.refresh_minutes" :min="0" :max="1440" class="num-input" />
          <span class="switch-tip">0 = 关闭后台自动刷新（仍可在「代理管理」页手动刷新）</span>
        </n-form-item>
        <n-form-item label="可用代理保存数量">
          <n-input-number v-model:value="form.save_limit" :min="0" :max="100000" class="num-input" />
          <span class="switch-tip">0 = 不限制（上游抓到多少就全测、测通多少就全存）；填正数则达标即提前停止测通</span>
        </n-form-item>
        <n-form-item label="后台刷新时测通">
          <n-switch v-model:value="form.auto_test" />
          <span class="switch-tip">{{ form.auto_test ? '测通 + 测延迟' : '仅抓取不测通' }}</span>
        </n-form-item>
        <n-form-item label="Actions 预取地址">
          <n-input v-model:value="form.remote_url" placeholder="https://你的域名/api/proxies/available（Actions 预取用）" />
        </n-form-item>
        <n-form-item label="Actions 预取 Token">
          <masked-input
            v-model="form.remote_token"
            :original-value="originalRemoteToken"
            type="password"
            placeholder="与 config_sync 同一个 API Key"
            custom-tip="已设置（接口不回传明文），留空保持不变，输入新值可修改"
          />
        </n-form-item>
        <n-form-item label="回传代理实测表现">
          <n-switch v-model:value="form.report_feedback" />
          <span class="switch-tip">
            {{ form.report_feedback ? '跑完回传成败计数，下次预取优先给实测能用的' : '不回传（优选只能靠服务器自测的延迟/测速）' }}
          </span>
        </n-form-item>
        <n-form-item label="开跑前自筛">
          <n-switch v-model:value="form.preflight_check" />
          <span class="switch-tip">
            {{ form.preflight_check ? '签到前在 Actions 本机快测一遍，当场剔掉连不上的' : '直接用平台给的列表' }}
          </span>
        </n-form-item>
        <n-form-item v-if="form.preflight_check" label="自筛测试条数">
          <n-input-number v-model:value="form.preflight_limit" :min="0" :max="500" class="num-input" />
          <span class="switch-tip">只测列表最前面的这些条；0 = 不自筛</span>
        </n-form-item>
        <n-form-item v-if="form.preflight_check" label="自筛时间上限（秒）">
          <n-input-number v-model:value="form.preflight_seconds" :min="1" :max="120" class="num-input" />
          <span class="switch-tip">到点就用已有结论，没测到的照旧保留，不会因为超时误删</span>
        </n-form-item>
        <n-form-item label="单 IP 账号数上限">
          <n-input-number v-model:value="form.max_accounts_per_ip" :min="0" :max="64" class="num-input" />
          <span class="switch-tip">
            几个账号可以共用一个出口；0 = 不限。客户端按它折算要预取多少代理，
            调小更安全但要抓更多 IP
          </span>
        </n-form-item>
      </n-form>
    </config-card>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import {
  NForm, NFormItem, NInput, NInputNumber, NSwitch, NDivider, NAlert, NIcon, NText,
  useMessage
} from 'naive-ui'
import { ServerOutline } from '@vicons/ionicons5'
import ConfigCard from '@/components/ConfigCard.vue'
import DynamicStringList from '@/components/DynamicStringList.vue'
import MaskedInput from '@/components/MaskedInput.vue'
import { useConfigStore } from '@/stores/config'
import { useDirtyGuard } from '@/composables/useDirtyGuard'
import { deepClone } from '@/utils/clone'
import { extractErrorMessage } from '@/utils/error'
import type { AppConfig, ProxyPoolConfig } from '@/types'

const configStore = useConfigStore()
const message = useMessage()

const saving = ref(false)
const initialized = ref(false)
// 服务端返回的原始 token（可能是 "***"），用于打码判断与「留空保持不变」
const originalRemoteToken = ref('')

// 脏检测：表单与已保存快照不一致时显示「未保存修改」，离开前弹确认
const savedSnapshot = ref('')
const isDirty = computed(() => JSON.stringify(form) !== savedSnapshot.value)
useDirtyGuard(() => isDirty.value)

const form = reactive<ProxyPoolConfig>({
  enabled: false,
  test_url: 'https://agentrouter.org/',
  timeout: 8,
  max_workers: 25,
  max_proxies: 250,
  ip_swap_limit: 10,
  sources: [],
  refresh_minutes: 30,
  save_limit: 0,
  auto_test: true,
  remote_url: '',
  remote_token: '',
  remote_token_header: 'Authorization',
  remote_token_prefix: 'Bearer',
  report_feedback: true,
  preflight_check: true,
  preflight_limit: 60,
  preflight_seconds: 15,
  max_accounts_per_ip: 4
})

function initForm(cfg: AppConfig) {
  const p = cfg.proxy_pool
  form.enabled = p.enabled
  form.test_url = p.test_url
  form.timeout = p.timeout
  form.max_workers = p.max_workers
  form.max_proxies = p.max_proxies
  form.ip_swap_limit = p.ip_swap_limit
  form.sources = [...(p.sources ?? [])]
  form.refresh_minutes = p.refresh_minutes ?? 30
  form.save_limit = p.save_limit ?? 0
  form.auto_test = p.auto_test ?? true
  form.remote_url = p.remote_url ?? ''
  form.remote_token = p.remote_token ?? ''
  originalRemoteToken.value = p.remote_token ?? ''
  form.remote_token_header = p.remote_token_header ?? 'Authorization'
  form.remote_token_prefix = p.remote_token_prefix ?? 'Bearer'
  // 老配置里没这个键，缺省按开：表单不带上它的话，保存一次就会被 Go 解析成 false
  form.report_feedback = p.report_feedback ?? true
  form.preflight_check = p.preflight_check ?? true
  form.preflight_limit = p.preflight_limit ?? 60
  form.preflight_seconds = p.preflight_seconds ?? 15
  form.max_accounts_per_ip = p.max_accounts_per_ip ?? 4
  savedSnapshot.value = JSON.stringify(form)
}

watch(
  () => configStore.config,
  (cfg) => {
    if (cfg && !initialized.value) {
      initForm(cfg)
      initialized.value = true
    }
  },
  { immediate: true }
)

function handleReset() {
  if (configStore.config) initForm(configStore.config)
}

async function handleSave() {
  if (!configStore.config) return
  saving.value = true
  try {
    const next = deepClone(configStore.config)
    next.proxy_pool = {
      ...form,
      sources: form.sources.map((s) => s.trim()).filter((s) => s !== ''),
      // 打码字段：留空表示不修改，回退为原始值（"***" 或原值）
      remote_token: form.remote_token === '' ? originalRemoteToken.value : form.remote_token
    }
    await configStore.save(next)
    savedSnapshot.value = JSON.stringify(form)
    message.success('代理池配置已保存')
  } catch (e) {
    message.error(extractErrorMessage(e, '代理池配置保存失败'))
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.switch-tip {
  margin-left: 10px;
  font-size: 13px;
  color: #8492a6;
}

.settings-form {
  max-width: 560px;
}

.server-proxy-tip {
  margin-bottom: 16px;
}

.num-input {
  width: 200px;
}
</style>
