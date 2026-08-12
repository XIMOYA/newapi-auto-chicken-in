/*
web/src/composables/useDirtyGuard.ts
组合式函数：表单脏检测 + 路由离开确认

职责：
- isDirty：对比当前表单与「已保存快照」的 JSON 序列化结果，判断是否有未保存修改
- useDirtyGuard：路由离开时若有未保存修改，弹出确认框（保留/放弃）

用法：
  const saved = ref('')                      // 已保存快照（initForm/save 成功后写入）
  const isDirty = computed(() => JSON.stringify(form) !== saved.value)
  useDirtyGuard(() => isDirty.value)
*/
import { onBeforeRouteLeave } from 'vue-router'
import { useDialog } from 'naive-ui'

/** 注册路由离开守卫：isDirty() 为 true 时弹确认框。 */
export function useDirtyGuard(isDirty: () => boolean) {
  const dialog = useDialog()

  onBeforeRouteLeave(() => {
    if (!isDirty()) return true
    return new Promise<boolean>((resolve) => {
      dialog.warning({
        title: '未保存的修改',
        content: '当前页面有未保存的修改，确定要离开吗？离开后将丢失本次修改。',
        positiveText: '离开',
        negativeText: '留下',
        onPositiveClick: () => resolve(true),
        onNegativeClick: () => resolve(false),
        onClose: () => resolve(false)
      })
    })
  })
}
