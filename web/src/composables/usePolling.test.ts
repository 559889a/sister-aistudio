import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createRenderer, defineComponent, h, type App } from 'vue'
import { usePolling, type PollingHandle } from './usePolling.ts'

interface Node { children: Node[] }

const renderer = createRenderer<Node, Node>({
  patchProp: () => {},
  insert: (child, parent) => { parent.children.push(child) },
  remove: () => {},
  createElement: () => ({ children: [] }),
  createText: () => ({ children: [] }),
  createComment: () => ({ children: [] }),
  setText: () => {},
  setElementText: () => {},
  parentNode: () => null,
  nextSibling: () => null,
})

test('polling stops disposed work, shares keyed ownership and waits for visibility callbacks', async () => {
  const windowDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const documentDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'document')
  const clearTimeoutDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'clearTimeout')
  const timers = new Map<number, { fn: () => void; delay: number }>()
  let nextTimer = 0
  let visibilityChanged = () => {}
  const fakeDocument = {
    hidden: false,
    addEventListener: (_event: string, fn: () => void) => { visibilityChanged = fn },
  }
  Object.defineProperty(globalThis, 'document', { configurable: true, value: fakeDocument })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      location: { protocol: 'http:' },
      setTimeout: (fn: () => void, delay: number) => {
        const id = ++nextTimer
        timers.set(id, { fn, delay })
        return id
      },
    },
  })
  Object.defineProperty(globalThis, 'clearTimeout', {
    configurable: true, value: (id: number) => timers.delete(id),
  })
  const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve() }
  const mounted = new Set<App>()
  const mount = (fn: () => Promise<void> | void, key: string, immediate = true) => {
    let handle!: PollingHandle
    const app = renderer.createApp(defineComponent({
      setup() {
        handle = usePolling(fn, 100, { key, immediate })
        return () => h('div')
      },
    }))
    app.mount({ children: [] })
    mounted.add(app)
    return { app, handle }
  }
  try {
    let finish!: () => void
    const disposed = mount(() => new Promise<void>((resolve) => { finish = resolve }), 'disposed')
    await flush()
    disposed.app.unmount()
    mounted.delete(disposed.app)
    finish()
    await flush()
    assert.equal(timers.size, 0, 'an in-flight callback cannot resurrect an unmounted poll')

    let firstRuns = 0
    let secondRuns = 0
    const first = mount(() => { firstRuns++ }, 'shared', false)
    const second = mount(() => { secondRuns++ }, 'shared', false)
    assert.equal(timers.size, 1)
    first.app.unmount()
    mounted.delete(first.app)
    second.handle.run()
    await flush()
    assert.equal(firstRuns, 0)
    assert.equal(secondRuns, 1, 'disposing one keyed owner must retain the other')
    second.app.unmount()
    mounted.delete(second.app)
    assert.equal(timers.size, 0)

    fakeDocument.hidden = true
    const visible = mount(() => new Promise<void>((resolve) => { finish = resolve }), 'visible')
    assert.equal(timers.size, 0)
    fakeDocument.hidden = false
    visibilityChanged()
    await flush()
    assert.equal(timers.size, 0, 'visibility resume must not schedule while callback runs')
    finish()
    await flush()
    assert.equal(timers.size, 1)
    assert.equal(Array.from(timers.values())[0].delay, 100)
    visible.app.unmount()
    mounted.delete(visible.app)

    const failing = mount(() => { throw new Error('offline') }, 'backoff', false)
    failing.handle.run()
    await flush()
    failing.handle.run()
    await flush()
    const pending = Array.from(timers.entries())[0]
    timers.delete(pending[0])
    pending[1].fn()
    await flush()
    assert.equal(Array.from(timers.values())[0].delay, 225)
    failing.app.unmount()
    mounted.delete(failing.app)
  } finally {
    for (const app of mounted) app.unmount()
    for (const [key, descriptor] of [
      ['window', windowDescriptor],
      ['document', documentDescriptor],
      ['clearTimeout', clearTimeoutDescriptor],
    ] as const) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else Reflect.deleteProperty(globalThis, key)
    }
  }
})
