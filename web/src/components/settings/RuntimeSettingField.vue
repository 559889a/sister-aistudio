<script setup lang="ts">
import { computed } from 'vue'
import { Info, TriangleAlert, X } from 'lucide-vue-next'
import type { RuntimeSettingField } from '@/types/settings.ts'

interface Props {
  field: RuntimeSettingField
  modelValue: string | boolean
  changed?: boolean
  disabled?: boolean
  disabledHint?: string
  error?: string
}

const props = withDefaults(defineProps<Props>(), {
  changed: false,
  disabled: false,
  disabledHint: '',
  error: '',
})

const emit = defineEmits<{
  'update:modelValue': [val: string | boolean]
}>()

const isMultiline = computed(
  () => props.field.type === 'text' && (props.field.max_length ?? 0) >= 200,
)

const textLength = computed(() => String(props.modelValue ?? '').length)


const enumOptions = computed(() => {
  const values = props.field.values ?? []
  return values.map((value) => ({
    value,
    label: props.field.value_labels?.[value] ?? (value === '' ? '（空）' : value),
  }))
})

function onInput(event: Event) {
  const target = event.target as HTMLInputElement | HTMLTextAreaElement
  emit('update:modelValue', target.value)
}

function onSelect(event: Event) {
  emit('update:modelValue', (event.target as HTMLSelectElement).value)
}

function toggleBool() {
  if (!props.disabled) {
    emit('update:modelValue', !props.modelValue)
  }
}

function clearNumber() {
  if (!props.disabled) {
    emit('update:modelValue', '')
  }
}
</script>

<template>
  <div
    class="rounded-xl border px-3.5 py-3 transition-colors"
    :class="error
      ? 'bg-rose-50/40 border-rose-200'
      : disabled
        ? 'bg-gray-50/60 border-gray-200/60'
        : 'bg-white border-gray-200/80 hover:border-gray-300'"
  >
    <div class="flex flex-col gap-2.5 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center gap-x-1.5 gap-y-1">
          <span
            class="text-xs font-semibold"
            :class="disabled ? 'text-gray-500' : 'text-gray-900'"
          >
            {{ field.label }}
          </span>
          <code class="font-mono text-[10px] text-gray-400">{{ field.key }}</code>
          <span
            v-if="changed"
            class="inline-flex items-center rounded-full bg-brand-50 px-1.5 py-0.5 text-[10px] font-semibold text-brand-700 border border-brand-200/70"
          >
            已修改
          </span>
          <span
            v-if="field.advanced"
            class="inline-flex items-center rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-500 border border-gray-200/70"
          >
            高级
          </span>
        </div>

        <p
          v-if="field.desc"
          class="mt-1 text-[11px] leading-relaxed text-gray-500"
        >
          {{ field.desc }}
        </p>

        <p
          v-if="disabled && disabledHint"
          class="mt-1.5 flex items-start gap-1 text-[11px] leading-relaxed text-amber-700"
        >
          <Info class="w-3.5 h-3.5 shrink-0 mt-px" />
          <span>{{ disabledHint }}</span>
        </p>

        <p
          v-if="error"
          class="mt-1.5 flex items-start gap-1 text-[11px] font-medium leading-relaxed text-rose-700"
        >
          <TriangleAlert class="w-3.5 h-3.5 shrink-0 mt-px" />
          <span>{{ error }}</span>
        </p>
      </div>

      <!-- bool: 开关 -->
      <div
        v-if="field.type === 'bool'"
        class="shrink-0 sm:pt-0.5"
      >
        <button
          type="button"
          role="switch"
          :aria-checked="Boolean(modelValue)"
          :disabled="disabled"
          class="relative inline-flex h-5 w-9 shrink-0 rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-brand-400/40 disabled:cursor-not-allowed"
          :class="modelValue ? 'bg-brand-500' : 'bg-gray-300'"
          @click="toggleBool"
        >
          <span
            class="pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-md ring-0 transition duration-200 ease-in-out"
            :class="modelValue ? 'translate-x-4' : 'translate-x-0'"
          />
        </button>
      </div>

      <!-- enum: 下拉 -->
      <div
        v-else-if="field.type === 'enum'"
        class="w-full sm:w-60 sm:shrink-0"
      >
        <select
          :value="String(modelValue ?? '')"
          :disabled="disabled"
          class="w-full px-3 py-2 bg-gray-50 border border-gray-200 rounded-xl text-xs outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all cursor-pointer disabled:cursor-not-allowed disabled:opacity-60"
          @change="onSelect"
        >
          <option
            v-for="option in enumOptions"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </option>
        </select>
      </div>

      <!-- int? / float?: 数字输入，清空 = 未设置 -->
      <div
        v-else-if="field.type === 'int?' || field.type === 'float?'"
        class="w-full sm:w-44 sm:shrink-0"
      >
        <div class="relative">
          <input
            :value="String(modelValue ?? '')"
            type="text"
            inputmode="decimal"
            :disabled="disabled"
            placeholder="未设置"
            class="w-full px-3 py-2 pr-8 bg-gray-50 border border-gray-200 rounded-xl text-xs font-mono outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all disabled:cursor-not-allowed disabled:opacity-60"
            @input="onInput"
          >
          <button
            v-if="String(modelValue ?? '') !== ''"
            type="button"
            :disabled="disabled"
            title="清空（表示未设置，跟随客户端）"
            class="absolute right-1.5 top-1/2 -translate-y-1/2 rounded-md p-0.5 text-gray-400 hover:text-gray-700 hover:bg-gray-200 transition-colors disabled:cursor-not-allowed"
            @click="clearNumber"
          >
            <X class="w-3.5 h-3.5" />
          </button>
        </div>
        <div class="mt-1 text-[10px] text-gray-400 sm:text-right">
          留空 = 未设置
        </div>
      </div>
    </div>

    <!-- text: 单行 / 多行 -->
    <div
      v-if="field.type === 'text'"
      class="mt-2.5"
    >
      <textarea
        v-if="isMultiline"
        :value="String(modelValue ?? '')"
        rows="3"
        :maxlength="field.max_length"
        :disabled="disabled"
        placeholder="留空 = 使用内置默认"
        class="w-full px-3 py-2 bg-gray-50 border border-gray-200 rounded-xl text-xs leading-relaxed outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all resize-y disabled:cursor-not-allowed disabled:opacity-60"
        @input="onInput"
      />
      <input
        v-else
        :value="String(modelValue ?? '')"
        type="text"
        :maxlength="field.max_length"
        :disabled="disabled"
        placeholder="留空 = 使用内置默认"
        class="w-full px-3 py-2 bg-gray-50 border border-gray-200 rounded-xl text-xs font-mono outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all disabled:cursor-not-allowed disabled:opacity-60"
        @input="onInput"
      >
      <div
        v-if="isMultiline && field.max_length"
        class="mt-1 text-[10px] text-gray-400 text-right"
      >
        {{ textLength }} / {{ field.max_length }}
      </div>
    </div>
  </div>
</template>
