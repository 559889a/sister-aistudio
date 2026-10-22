import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { useAccountsStore } from './accounts.ts'

const first = {
  id: 'first', name: 'Renamed', email: 'current@example.com', tier: 'Pro',
  created_at: '2026-10-22', auth_user: '0', cookie_id: 'cookie_first',
}

test('account store keeps current metadata, empty active state and failed edit drafts', async () => {
  const fetchDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'fetch')
  const storageDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  let activeExists = true
  let updateFails = false
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true, value: { getItem: () => '', setItem: () => {}, removeItem: () => {} },
  })
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (url: string, options: RequestInit = {}) => {
      if (options.method === 'PUT') {
        return Response.json(updateFails ? { detail: 'write failed' } : first, { status: updateFails ? 500 : 200 })
      }
      if (url === '/api/accounts') return Response.json([first])
      if (url === '/api/accounts/active') {
        return Response.json(activeExists ? first : { detail: '没有活跃账号' }, { status: activeExists ? 200 : 404 })
      }
      if (url === '/rotation') {
        return Response.json({
          accounts: {
            first: { name: 'Stale name', email: 'stale@example.com', tier: 'Free', requests: 7 },
          },
        })
      }
      throw new Error(`Unexpected URL: ${url}`)
    },
  })
  try {
    setActivePinia(createPinia())
    const store = useAccountsStore()
    assert.equal(await store.fetchAll(), true)
    assert.equal(store.accountRows[0].name, 'Renamed')
    assert.equal(store.accountRows[0].email, 'current@example.com')
    assert.equal(store.accountRows[0].tier, 'Pro')
    assert.equal(store.accountRows[0].requests, 7)
    activeExists = false
    assert.equal(await store.fetchAll(), true)
    assert.equal(store.activeAccount, null, '404 must not fabricate first account as active')
    updateFails = true
    assert.equal(await store.updateAccountName(first.id, 'Draft'), false)
  } finally {
    for (const [key, descriptor] of [['fetch', fetchDescriptor], ['localStorage', storageDescriptor]] as const) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else Reflect.deleteProperty(globalThis, key)
    }
  }
})
