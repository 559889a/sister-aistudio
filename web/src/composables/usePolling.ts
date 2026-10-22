import { onUnmounted } from 'vue'

export interface PollingOptions {
  /** 任务唯一标识（可选，用于跨组件去重） */
  key?: string
  /** 是否在挂载或恢复可见时立即执行一次（默认 true） */
  immediate?: boolean
  /** 最大错误退避间隔（毫秒，默认 60000ms） */
  maxBackoffMs?: number
}

export interface PollingHandle {
  /** 立即执行一次回调 */
  run: () => void
  /** 暂停轮询（可恢复） */
  pause: () => void
  /** 恢复轮询 */
  resume: () => void
}

interface ActiveTask {
  callbacks: Set<() => void | Promise<void>>
  intervalMs: number
  maxBackoffMs: number
  timer?: number
  paused: boolean
  running: boolean
  failureCount: number
}

const activeTasks = new Map<string, ActiveTask>()
let visibilityListenerRegistered = false

function setupGlobalVisibilityListener() {
  if (visibilityListenerRegistered || typeof window === 'undefined') return
  visibilityListenerRegistered = true

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      for (const task of activeTasks.values()) {
        stopTaskTimer(task)
      }
    } else {
      for (const task of activeTasks.values()) {
        if (!task.paused) {
          task.failureCount = 0
          void runTask(task)
        }
      }
    }
  })
}

async function runTask(task: ActiveTask) {
  if (task.running || (typeof document !== 'undefined' && document.hidden)) return
  task.running = true
  try {
    const results = await Promise.allSettled(Array.from(task.callbacks, (fn) => Promise.resolve().then(fn)))
    const failed = results.find((result) => result.status === 'rejected')
    if (failed?.status === 'rejected') throw failed.reason
    task.failureCount = 0
  } catch (err) {
    task.failureCount++
    console.debug('[usePolling] task execution error (consecutive failures: %d):', task.failureCount, err)
  } finally {
    task.running = false
    scheduleNext(task)
  }
}

function scheduleNext(task: ActiveTask) {
  if (typeof window === 'undefined' || !window.location.protocol.startsWith('http')) return
  if (task.timer || task.paused || task.running || document.hidden) return

  let delay = task.intervalMs
  if (task.failureCount > 1) {
    delay = Math.min(task.intervalMs * Math.pow(1.5, task.failureCount - 1), task.maxBackoffMs)
  }

  task.timer = window.setTimeout(() => {
    task.timer = undefined
    void runTask(task)
  }, delay)
}

function stopTaskTimer(task: ActiveTask) {
  if (task.timer) {
    clearTimeout(task.timer)
    task.timer = undefined
  }
}

let autoKeyCounter = 0

/**
 * 页面可见时周期性调度的统一轮询管理。
 * 支持页面隐藏自动冻结、失败指数退避与销毁自动释放。
 */
export function usePolling(
  fn: () => void | Promise<void>,
  intervalMs: number,
  options: PollingOptions = {}
): PollingHandle {
  setupGlobalVisibilityListener()

  const taskId = options.key || `poll_${++autoKeyCounter}`
  const immediate = options.immediate !== false
  const maxBackoffMs = options.maxBackoffMs || 60000

  const existing = activeTasks.get(taskId)
  const task: ActiveTask = existing ?? {
    callbacks: new Set(),
    intervalMs,
    maxBackoffMs,
    paused: false,
    running: false,
    failureCount: 0,
  }
  task.callbacks.add(fn)
  activeTasks.set(taskId, task)

  if (!existing && typeof window !== 'undefined' && window.location.protocol.startsWith('http')) {
    if (!document.hidden) {
      if (immediate) {
        void runTask(task)
      } else {
        scheduleNext(task)
      }
    }
  }

  const handle: PollingHandle = {
    run: () => {
      void runTask(task)
    },
    pause: () => {
      task.paused = true
      stopTaskTimer(task)
    },
    resume: () => {
      task.paused = false
      task.failureCount = 0
      scheduleNext(task)
    },
  }

  onUnmounted(() => {
    task.callbacks.delete(fn)
    if (task.callbacks.size === 0) {
      task.paused = true
      stopTaskTimer(task)
      activeTasks.delete(taskId)
    }
  })

  return handle
}
