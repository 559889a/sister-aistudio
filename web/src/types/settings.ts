/** 运行期设置（GET /settings）的类型定义，与后端 runtime_settings.schema() 对齐。 */

/** 字段类型：bool 开关 / enum 下拉 / int? 整数（空=未设置）/ float? 小数 / text 文本。 */
export type RuntimeSettingType = 'bool' | 'enum' | 'int?' | 'float?' | 'text'

export type RuntimeSettingValue = boolean | string | number | null

export interface RuntimeSettingField {
  key: string
  type: RuntimeSettingType
  default: RuntimeSettingValue
  group: string
  label: string
  desc?: string
  /** enum 的可选值（可能包含空字符串，表示「用默认」）。 */
  values?: string[]
  /** enum 选项的展示文案。 */
  value_labels?: Record<string, string>
  /** 依赖关系：父项取值命中列表时该项才生效；父项不满足时应禁用并说明。 */
  depends_on?: Record<string, RuntimeSettingValue[]>
  /** 高级选项，默认折叠。 */
  advanced?: boolean
  min?: number
  max?: number
  max_length?: number
}

export interface RuntimeSettingsGroup {
  key: string
  label: string
}

export interface RuntimeSettingsSchema {
  groups: RuntimeSettingsGroup[]
  fields: RuntimeSettingField[]
  values: Record<string, RuntimeSettingValue>
  /** data/settings.json 的落盘路径。 */
  path: string
}
