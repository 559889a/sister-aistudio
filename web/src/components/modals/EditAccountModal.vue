<script setup lang="ts">
import { ref, watch } from 'vue'
import type { AccountWithStats } from '@/types'
import { useAccountsStore } from '@/stores/accounts.ts'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'
import FormInput from '@/components/ui/FormInput.vue'

const props = defineProps<{
  modelValue: boolean
  account: AccountWithStats | null
}>()

const emit = defineEmits<{
  'update:modelValue': [val: boolean]
}>()

const accountsStore = useAccountsStore()
const inputName = ref('')
const saving = ref(false)

watch(
  () => [props.modelValue, props.account] as const,
  ([open, acc]) => {
    if (open && acc) {
      inputName.value = acc.name || ''
    }
  },
  { immediate: true }
)

async function handleSave() {
  if (!props.account || saving.value) return
  const trimmed = inputName.value.trim()
  if (!trimmed) return

  saving.value = true
  try {
    if (await accountsStore.updateAccountName(props.account.id, trimmed)) {
      emit('update:modelValue', false)
    }
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="修改账号名称"
    @update:model-value="!saving && emit('update:modelValue', $event)"
  >
    <div class="space-y-4">
      <FormInput
        :model-value="inputName"
        label="账号名称 / 备注"
        placeholder="例如: 主账号 (Pro)、备用账号 1 等"
        :disabled="saving"
        @update:model-value="inputName = String($event ?? '')"
      />
    </div>

    <template #footer>
      <Button
        variant="ghost"
        size="sm"
        :disabled="saving"
        @click="emit('update:modelValue', false)"
      >
        取消
      </Button>
      <Button
        variant="primary"
        size="sm"
        :loading="saving"
        :disabled="!inputName.trim()"
        @click="handleSave"
      >
        保存修改
      </Button>
    </template>
  </Modal>
</template>
