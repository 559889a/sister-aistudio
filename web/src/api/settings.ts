import { request } from './client.ts'
import type { RuntimeSettingValue, RuntimeSettingsSchema } from '@/types/settings.ts'

export const settingsApi = {
  /** 读取运行期设置 schema + 当前值 + 落盘路径。 */
  getRuntime(): Promise<RuntimeSettingsSchema> {
    return request<RuntimeSettingsSchema>('/settings')
  },

  /** 局部更新运行期设置，只提交改动项；返回更新后的完整 schema。 */
  updateRuntime(patch: Record<string, RuntimeSettingValue>): Promise<RuntimeSettingsSchema> {
    return request<RuntimeSettingsSchema>('/settings', {
      method: 'PUT',
      body: JSON.stringify({ patch }),
    })
  },
}
