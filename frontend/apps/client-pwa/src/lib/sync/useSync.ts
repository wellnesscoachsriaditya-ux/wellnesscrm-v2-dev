import { useState, useEffect, useCallback } from 'react'
import { peekQueue } from './db'
import { syncQueue } from './replay'

export function useSync() {
  const [isOnline, setIsOnline] = useState(
    typeof navigator !== 'undefined' ? navigator.onLine : true
  )
  const [pendingCount, setPendingCount] = useState(0)
  const [isSyncing, setIsSyncing] = useState(false)

  const updateCount = useCallback(async () => {
    try {
      const queue = await peekQueue()
      setPendingCount(queue.length)
    } catch {
      // Ignore if DB isn't ready
    }
  }, [])

  const triggerSync = useCallback(async () => {
    if (!isOnline) return
    setIsSyncing(true)
    try {
      const { planChanged } = await syncQueue()
      await updateCount()
      if (planChanged) {
        // Trigger a re-fetch of /today or reload.
        // A simple way is to dispatch an event or window.location.reload()
        window.dispatchEvent(new CustomEvent('wellnesscrm:plan_changed'))
      }
    } finally {
      setIsSyncing(false)
    }
  }, [isOnline, updateCount])

  useEffect(() => {
    // Initial load
    void updateCount()
    if (navigator.onLine) {
      void triggerSync()
    }
  }, [updateCount, triggerSync])

  useEffect(() => {
    const handleOnline = () => {
      setIsOnline(true)
      void triggerSync()
    }
    const handleOffline = () => setIsOnline(false)

    window.addEventListener('online', handleOnline)
    window.addEventListener('offline', handleOffline)

    // Listen to local queue additions to update count and sync if online
    const handleQueueChange = () => {
      void updateCount()
      if (navigator.onLine) {
        void triggerSync()
      }
    }
    window.addEventListener('wellnesscrm:queue_changed', handleQueueChange)

    return () => {
      window.removeEventListener('online', handleOnline)
      window.removeEventListener('offline', handleOffline)
      window.removeEventListener('wellnesscrm:queue_changed', handleQueueChange)
    }
  }, [triggerSync, updateCount])

  return { isOnline, pendingCount, isSyncing, triggerSync }
}
