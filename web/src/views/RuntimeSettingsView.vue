<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { settingsApi } from '@/api/settings.ts'
import { useToastStore } from '@/stores/toast.ts'
import type {
  RuntimeSettingField,
  RuntimeSettingValue,
  RuntimeSettingsSchema,
} from '@/types/settings.ts'
import RuntimeSettingFieldItem from '@/components/settings/RuntimeSettingField.vue'
import Button from '@/components/ui/Button.vue'
import {
  Activity,
  AlertCircle,
  ArrowRight,
  ChevronDown,
  FileCode,
  FolderOpen,
  Info,
  RefreshCw,
  RotateCcw,
  Save,
  Settings2,
  Zap,
} from 'lucide-vue-next'

type FormValue = string | boolean

interface SettingGroupBlock {
  key: string
  label: string
  basic: RuntimeSettingField[]
  advanced: RuntimeSettingField[]
  changed: number
}

interface DependencyState {
  disabled: boolean
  hint: string
}

const toast = useToastStore()

const schema = ref<RuntimeSettingsSchema | null>(null)
const form = ref<Record<string, FormValue>>({})
const baseline = ref<Record<string, RuntimeSettingValue>>({})
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)
const saveError = ref('')
const fieldErrors = ref<Record<string, string>>({})
const advancedOpen = ref<Record<string, boolean>>({})

const fields = computed<RuntimeSettingField[]>(() => schema.value?.fields ?? [])

const fieldsByKey = computed(() => {
  const map: Record<string, RuntimeSettingField> = {}
  for (const field of fields.value) {
    map[field.key] = field
  }
  return map
})

const settingsPath = computed(() => schema.value?.path || 'data/settings.json')

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

/** 线上值 → 表单里可编辑的原始值（数字用字符串承载，空串表示未设置）。 */
function rawFromWire(field: RuntimeSettingField, value: RuntimeSettingValue | undefined): FormValue {
  if (field.type === 'bool') {
    return Boolean(value ?? field.default)
  }
  if (value === null || value === undefined) {
    return ''
  }
  return String(value)
}

/** 表单原始值 → 提交给 PUT /settings 的线上值。 */
function wireValue(field: RuntimeSettingField, raw: FormValue | undefined): RuntimeSettingValue {
  if (field.type === 'bool') {
    return Boolean(raw)
  }
  const text = String(raw ?? '')
  if (field.type === 'int?') {
    const trimmed = text.trim()
    return trimmed === '' ? null : Number(trimmed)
  }
  if (field.type === 'float?') {
    const trimmed = text.trim()
    return trimmed === '' ? null : Number(trimmed)
  }
  return text
}

function baselineValue(field: RuntimeSettingField): RuntimeSettingValue {
  const stored = baseline.value[field.key]
  return stored === undefined ? field.default : stored
}

function isFieldDirty(field: RuntimeSettingField): boolean {
  const error = validateField(field)
  if (error) {
    return form.value[field.key] !== rawFromWire(field, baselineValue(field))
  }
  return JSON.stringify(wireValue(field, form.value[field.key]))
    !== JSON.stringify(baselineValue(field))
}

const changedCount = computed(() => fields.value.filter((field) => isFieldDirty(field)).length)

const groupBlocks = computed<SettingGroupBlock[]>(() => {
  const current = schema.value
  if (!current) {
    return []
  }
  return current.groups
    .map((group) => {
      const inGroup = current.fields.filter((field) => field.group === group.key)
      return {
        key: group.key,
        label: group.label,
        basic: inGroup.filter((field) => !field.advanced),
        advanced: inGroup.filter((field) => Boolean(field.advanced)),
        changed: inGroup.filter((field) => isFieldDirty(field)).length,
      }
    })
    .filter((group) => group.basic.length > 0 || group.advanced.length > 0)
})

function formatAllowed(parent: RuntimeSettingField | undefined, allowed: RuntimeSettingValue[]): string {
  return allowed
    .map((value) => {
      if (typeof value === 'boolean') {
        return value ? '开启' : '关闭'
      }
      if (parent?.type === 'enum') {
        return parent.value_labels?.[String(value)] ?? (value === '' ? '（空）' : String(value))
      }
      return String(value)
    })
    .join(' / ')
}

function dependencyOf(field: RuntimeSettingField): DependencyState {
  const deps = field.depends_on
  if (!deps) {
    return { disabled: false, hint: '' }
  }
  const unmet: string[] = []
  for (const [parentKey, allowed] of Object.entries(deps)) {
    const parentField = fieldsByKey.value[parentKey]
    const parentRaw = form.value[parentKey]
    const parentValue = parentField ? wireValue(parentField, parentRaw) : parentRaw
    const satisfied = allowed.some(
      (candidate) => JSON.stringify(candidate) === JSON.stringify(parentValue),
    )
    if (!satisfied) {
      const parentLabel = parentField?.label ?? parentKey
      unmet.push(`「${parentLabel}」为「${formatAllowed(parentField, allowed)}」`)
    }
  }
  if (unmet.length === 0) {
    return { disabled: false, hint: '' }
  }
  return {
    disabled: true,
    hint: `当前不生效：需${unmet.join('，且')}。`,
  }
}

const dependencyStates = computed<Record<string, DependencyState>>(() => {
  const map: Record<string, DependencyState> = {}
  for (const field of fields.value) {
    map[field.key] = dependencyOf(field)
  }
  return map
})

function validateField(field: RuntimeSettingField): string {
  if (field.type === 'int?' || field.type === 'float?') {
    const raw = String(form.value[field.key] ?? '').trim()
    if (raw === '') {
      return ''
    }
    if (field.type === 'int?' && !/^-?\d+$/.test(raw)) {
      return '需要整数（或留空表示不设置）'
    }
    const num = Number(raw)
    if (!Number.isFinite(num)) {
      return '需要有效数字（或留空表示不设置）'
    }
    if (field.type === 'int?' && !Number.isInteger(num)) {
      return '需要整数（或留空表示不设置）'
    }
    if (field.min !== undefined && num < field.min) {
      return `不能小于 ${field.min}`
    }
    if (field.max !== undefined && num > field.max) {
      return `不能大于 ${field.max}`
    }
  }
  if (field.type === 'enum') {
    const value = String(form.value[field.key] ?? '')
    if (field.values && !field.values.includes(value)) {
      return '请选择合法选项'
    }
  }
  if (field.type === 'text') {
    const value = String(form.value[field.key] ?? '')
    if (field.max_length !== undefined && value.length > field.max_length) {
      return `最多 ${field.max_length} 个字符（当前 ${value.length}）`
    }
  }
  return ''
}

function applySchema(next: RuntimeSettingsSchema) {
  schema.value = next
  baseline.value = { ...next.values }
  const nextForm: Record<string, FormValue> = {}
  for (const field of next.fields) {
    nextForm[field.key] = rawFromWire(field, next.values[field.key])
  }
  form.value = nextForm
  fieldErrors.value = {}
}

async function loadSchema() {
  loading.value = true
  loadError.value = ''
  saveError.value = ''
  try {
    applySchema(await settingsApi.getRuntime())
  } catch (err) {
    loadError.value = errorMessage(err, '加载运行期设置失败')
    toast.error(loadError.value)
  } finally {
    loading.value = false
  }
}

function setFieldValue(key: string, value: FormValue) {
  form.value = { ...form.value, [key]: value }
  if (fieldErrors.value[key]) {
    const nextErrors = { ...fieldErrors.value }
    delete nextErrors[key]
    fieldErrors.value = nextErrors
  }
}

function isAdvancedOpen(groupKey: string): boolean {
  return advancedOpen.value[groupKey] === true
}

function toggleAdvanced(groupKey: string) {
  advancedOpen.value = { ...advancedOpen.value, [groupKey]: !isAdvancedOpen(groupKey) }
}

function advancedChangedCount(group: SettingGroupBlock): number {
  return group.advanced.filter((field) => isFieldDirty(field)).length
}

async function handleSave() {
  if (saving.value || loading.value) {
    return
  }
  saveError.value = ''
  const errors: Record<string, string> = {}
  for (const field of fields.value) {
    if (!isFieldDirty(field)) continue
    const message = validateField(field)
    if (message) {
      errors[field.key] = message
    }
  }
  fieldErrors.value = errors
  if (Object.keys(errors).length > 0) {
    toast.warning(`有 ${Object.keys(errors).length} 项填写不合法，请先修正`)
    return
  }

  const patch: Record<string, RuntimeSettingValue> = {}
  for (const field of fields.value) {
    if (isFieldDirty(field)) {
      patch[field.key] = wireValue(field, form.value[field.key])
    }
  }
  const count = Object.keys(patch).length
  if (count === 0) {
    toast.info('没有需要保存的改动')
    return
  }

  saving.value = true
  try {
    const next = await settingsApi.updateRuntime(patch)
    applySchema(next)
    toast.success(`已保存 ${count} 项运行期设置，立即生效`)
  } catch (err) {
    const message = errorMessage(err, '保存运行期设置失败')
    saveError.value = message
    toast.error(message)
  } finally {
    saving.value = false
  }
}

function handleReset() {
  if (!schema.value) {
    return
  }
  applySchema(schema.value)
  saveError.value = ''
  toast.info('已还原为上次保存的值')
}

function handleReload() {
  if (changedCount.value > 0 && !confirm(`有 ${changedCount.value} 项未保存的改动，重新加载会丢弃它们，继续？`)) {
    return
  }
  void loadSchema()
}

onMounted(() => {
  void loadSchema()
})

onBeforeRouteLeave((_to, _from, next) => {
  if (saving.value) {
    next(false)
    return
  }
  if (changedCount.value > 0 && !confirm(`有 ${changedCount.value} 项运行期设置尚未保存，确定离开？`)) {
    next(false)
    return
  }
  next()
})
</script>

<template>
  <div
    class="space-y-6"
    :class="{ 'pb-24': schema && changedCount > 0 }"
  >
    <!-- Header / 与启动期配置的区别 -->
    <div class="bg-white border border-gray-200/80 rounded-2xl p-5 sm:p-6 shadow-xs space-y-4">
      <div class="flex flex-col lg:flex-row lg:items-start justify-between gap-4">
        <div class="min-w-0">
          <div class="flex items-center gap-2.5">
            <div class="p-2 rounded-xl bg-brand-50 text-brand-600 border border-brand-100">
              <Settings2 class="w-5 h-5" />
            </div>
            <div class="min-w-0">
              <h2 class="text-lg font-bold text-gray-900 tracking-tight">
                运行期设置
              </h2>
              <p class="text-[11px] text-gray-500">
                决定每一次请求怎么改写上游请求、怎么翻译回来。
              </p>
            </div>
          </div>

          <div class="mt-3 flex flex-wrap items-center gap-2">
            <span class="inline-flex items-center gap-1 rounded-full bg-emerald-50 border border-emerald-200/70 px-2 py-0.5 text-[10px] font-semibold text-emerald-800">
              <Zap class="w-3 h-3 text-emerald-600" />
              改完立即生效，不用重启
            </span>
            <span class="inline-flex items-center gap-1 rounded-full bg-gray-100 border border-gray-200/70 px-2 py-0.5 text-[10px] font-medium text-gray-600">
              <Activity class="w-3 h-3 text-gray-500" />
              {{ changedCount > 0 ? `${changedCount} 项待保存` : '与已保存值一致' }}
            </span>
          </div>

          <div
            v-if="schema"
            class="mt-2 flex items-start gap-1.5 text-[11px] text-gray-500"
          >
            <FolderOpen class="w-3.5 h-3.5 text-gray-400 shrink-0 mt-px" />
            <span class="min-w-0">
              落盘路径：
              <code
                class="break-all rounded bg-gray-100 px-1 py-0.5 font-mono text-[10px] text-gray-700"
                :title="settingsPath"
              >{{ settingsPath }}</code>
            </span>
          </div>
        </div>

        <div class="flex w-full items-center gap-2 lg:w-auto lg:shrink-0">
          <Button
            variant="secondary"
            size="sm"
            :disabled="changedCount === 0 || saving"
            title="放弃未保存的改动，恢复为服务端当前值"
            @click="handleReset"
          >
            <RotateCcw class="w-3.5 h-3.5" />
            <span>还原</span>
          </Button>
          <Button
            variant="primary"
            size="sm"
            :loading="saving"
            :disabled="changedCount === 0"
            title="只提交改动过的项，保存后立即生效"
            @click="handleSave"
          >
            <Save class="w-3.5 h-3.5" />
            <span>保存改动<template v-if="changedCount > 0">（{{ changedCount }}）</template></span>
          </Button>
          <Button
            variant="ghost"
            size="sm"
            :disabled="saving || loading"
            title="重新从服务端拉取当前值"
            @click="handleReload"
          >
            <RefreshCw class="w-3.5 h-3.5" />
          </Button>
        </div>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div class="rounded-xl border border-brand-200/70 bg-brand-50/50 p-3.5">
          <div class="flex items-center gap-1.5 text-[11px] font-bold text-brand-900">
            <Zap class="w-3.5 h-3.5 text-brand-600" />
            <span>运行期设置（本页）</span>
          </div>
          <p class="mt-1 text-[11px] leading-relaxed text-brand-900/80">
            参数覆写 / 预填充兼容 / 工具调用 bypass。保存在 data/settings.json，保存后<strong>立即生效、无需重启</strong>。
          </p>
        </div>
        <router-link
          to="/settings"
          class="group rounded-xl border border-gray-200 bg-white p-3.5 transition-colors hover:border-brand-300 hover:bg-brand-50/30"
        >
          <div class="flex items-center justify-between gap-2">
            <div class="flex items-center gap-1.5 text-[11px] font-bold text-gray-800">
              <FileCode class="w-3.5 h-3.5 text-gray-500" />
              <span>系统配置（服务参数）</span>
            </div>
            <ArrowRight class="w-3.5 h-3.5 text-gray-400 transition-transform group-hover:translate-x-0.5 group-hover:text-brand-600" />
          </div>
          <p class="mt-1 text-[11px] leading-relaxed text-gray-500">
            监听端口、鉴权、浏览器、日志与 API Key。在「系统配置」页管理，启动项改完需要重启进程。
          </p>
        </router-link>
      </div>
    </div>

    <!-- 加载中骨架 -->
    <div
      v-if="loading"
      class="space-y-4"
    >
      <div
        v-for="i in 3"
        :key="i"
        class="bg-white border border-gray-200/80 rounded-2xl p-5 shadow-xs space-y-3 animate-pulse"
      >
        <div class="h-4 w-32 rounded bg-gray-200" />
        <div class="h-14 rounded-xl bg-gray-100" />
        <div class="h-14 rounded-xl bg-gray-100" />
      </div>
    </div>

    <!-- 加载失败 -->
    <div
      v-else-if="loadError"
      class="bg-white border border-rose-200 rounded-2xl p-6 shadow-xs"
    >
      <div class="flex items-start gap-3">
        <div class="p-2 rounded-xl bg-rose-50 text-rose-600 border border-rose-100">
          <AlertCircle class="w-5 h-5" />
        </div>
        <div class="min-w-0 flex-1">
          <h3 class="text-sm font-bold text-gray-900">
            加载运行期设置失败
          </h3>
          <p class="mt-1 text-xs leading-relaxed text-rose-700 break-words">
            {{ loadError }}
          </p>
          <p class="mt-1.5 text-[11px] leading-relaxed text-gray-500">
            接口：<code class="rounded bg-gray-100 px-1 py-0.5 font-mono text-[10px]">GET /settings</code>。请确认服务已启动、控制台鉴权 token 有效；免密模式下直接重试即可。
          </p>
          <div class="mt-3">
            <Button
              variant="primary"
              size="sm"
              :loading="loading"
              @click="loadSchema"
            >
              <RefreshCw class="w-3.5 h-3.5" />
              <span>重试</span>
            </Button>
          </div>
        </div>
      </div>
    </div>

    <!-- 设置内容 -->
    <template v-else-if="schema">
      <!-- 保存失败 -->
      <div
        v-if="saveError"
        class="flex items-start gap-2.5 rounded-2xl border border-rose-200 bg-rose-50/70 px-4 py-3 text-xs text-rose-800"
      >
        <AlertCircle class="w-4 h-4 shrink-0 mt-0.5 text-rose-600" />
        <div class="min-w-0">
          <div class="font-semibold">
            保存失败，改动没有写入
          </div>
          <div class="mt-0.5 break-words">
            {{ saveError }}
          </div>
        </div>
      </div>

      <!-- 三组设置 -->
      <div
        v-for="group in groupBlocks"
        :key="group.key"
        class="bg-white border border-gray-200/80 rounded-2xl p-4 sm:p-5 shadow-xs space-y-3"
      >
        <div class="flex flex-wrap items-center justify-between gap-2 border-b border-gray-100 pb-3">
          <div class="flex items-center gap-2">
            <span class="w-1.5 h-1.5 rounded-full bg-brand-500" />
            <h3 class="text-sm font-bold text-gray-900">
              {{ group.label }}
            </h3>
            <span class="text-[11px] text-gray-400">
              {{ group.basic.length + group.advanced.length }} 项
            </span>
          </div>
          <span
            v-if="group.changed > 0"
            class="inline-flex items-center rounded-full bg-amber-50 border border-amber-200/70 px-2 py-0.5 text-[10px] font-semibold text-amber-800"
          >
            已改动 {{ group.changed }} 项
          </span>
        </div>

        <div
          v-if="group.basic.length > 0"
          class="space-y-2.5"
        >
          <RuntimeSettingFieldItem
            v-for="field in group.basic"
            :key="field.key"
            :field="field"
            :model-value="form[field.key] ?? ''"
            :changed="isFieldDirty(field)"
            :disabled="saving || ((dependencyStates[field.key]?.disabled ?? false) && !validateField(field))"
            :disabled-hint="dependencyStates[field.key]?.hint ?? ''"
            :error="fieldErrors[field.key] ?? ''"
            @update:model-value="setFieldValue(field.key, $event)"
          />
        </div>

        <!-- 高级选项（默认折叠） -->
        <div v-if="group.advanced.length > 0">
          <button
            type="button"
            class="flex w-full items-center justify-between gap-2 rounded-xl border border-dashed border-gray-200 px-3 py-2 text-[11px] font-medium text-gray-500 transition-colors hover:bg-gray-50 hover:text-gray-700"
            @click="toggleAdvanced(group.key)"
          >
            <span class="flex items-center gap-1.5">
              <ChevronDown
                class="w-3.5 h-3.5 transition-transform"
                :class="{ 'rotate-180': isAdvancedOpen(group.key) }"
              />
              <span>高级选项（{{ group.advanced.length }}）</span>
            </span>
            <span
              v-if="advancedChangedCount(group) > 0"
              class="font-semibold text-brand-600"
            >
              {{ advancedChangedCount(group) }} 项已改动
            </span>
            <span
              v-else
              class="text-gray-400"
            >
              {{ isAdvancedOpen(group.key) ? '收起' : '展开' }}
            </span>
          </button>

          <div
            v-if="isAdvancedOpen(group.key)"
            class="mt-2.5 space-y-2.5"
          >
            <RuntimeSettingFieldItem
              v-for="field in group.advanced"
              :key="field.key"
              :field="field"
              :model-value="form[field.key] ?? ''"
              :changed="isFieldDirty(field)"
              :disabled="saving || ((dependencyStates[field.key]?.disabled ?? false) && !validateField(field))"
              :disabled-hint="dependencyStates[field.key]?.hint ?? ''"
              :error="fieldErrors[field.key] ?? ''"
              @update:model-value="setFieldValue(field.key, $event)"
            />
          </div>
        </div>
      </div>

      <!-- 底部说明 -->
      <div class="flex items-start gap-2 rounded-2xl border border-gray-200/70 bg-gray-50/60 p-4 text-[11px] leading-relaxed text-gray-500">
        <Info class="w-4 h-4 text-gray-400 shrink-0 mt-px" />
        <div class="min-w-0">
          保存时只提交<strong class="text-gray-700">实际改动过的项</strong>；任何一项不合法都会被服务端整体拒绝（400），不会写半套。这些设置与启动期配置
          <code class="break-all rounded bg-white px-1 py-0.5 font-mono text-[10px] text-gray-700">config.yaml</code>
          （手工编辑）是两套东西，互不覆盖。
        </div>
      </div>

      <!-- 未保存提示（移动端常驻底部） -->
      <div
        v-if="changedCount > 0"
        class="fixed bottom-0 left-0 right-0 z-30 lg:left-64"
      >
        <div class="mx-auto max-w-7xl w-full px-4 sm:px-6 lg:px-8 pb-4">
          <div class="flex flex-col gap-2 rounded-2xl border border-brand-200 bg-white/95 px-4 py-3 shadow-lg backdrop-blur sm:flex-row sm:items-center sm:justify-between">
            <div class="flex items-center gap-2 text-xs text-gray-700">
              <AlertCircle class="w-4 h-4 text-amber-500 shrink-0" />
              <span><span class="font-bold">{{ changedCount }}</span> 项改动尚未保存 · 保存后立即生效</span>
            </div>
            <div class="flex items-center gap-2">
              <Button
                variant="secondary"
                size="sm"
                :disabled="saving"
                @click="handleReset"
              >
                还原
              </Button>
              <Button
                variant="primary"
                size="sm"
                :loading="saving"
                class="flex-1 sm:flex-none"
                @click="handleSave"
              >
                <Save class="w-3.5 h-3.5" />
                <span>保存</span>
              </Button>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
