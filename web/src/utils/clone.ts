/*
web/src/utils/clone.ts
工具函数：深拷贝
职责：配置对象均为纯 JSON 数据，使用 JSON 序列化深拷贝即可
*/
export function deepClone<T>(value: T): T {
  if (value === null || value === undefined) return value
  return JSON.parse(JSON.stringify(value)) as T
}
