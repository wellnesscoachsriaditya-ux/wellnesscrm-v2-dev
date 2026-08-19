/**
 * The practice's tag vocabulary, for the list's tag filter — FR-M1-022.
 *
 * 🔒 The API layer (Arch §4.4). Separate from `useClientList` on purpose: the
 * vocabulary is per-practice, not per-query, so it must not re-fetch when a
 * filter changes. Folding it into the list hook would put a tag request behind
 * every keystroke.
 *
 * ⚠️ Reuses `fetchTags` from `collaborationApi` rather than adding a second
 * caller of `/tags`. Two functions hitting one endpoint is how the response shape
 * ends up mapped two different ways.
 */

import { useEffect, useState } from 'react'
import { fetchTags, type Tag } from './collaborationApi'

export interface TagVocabularyState {
  tags: Tag[]
  loading: boolean
}

export function useTagVocabulary(): TagVocabularyState {
  const [tags, setTags] = useState<Tag[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const controller = new AbortController()

    fetchTags(controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return
        setTags(value)
        setLoading(false)
      })
      .catch(() => {
        // ⚠️ Swallowed, like `useCurrentSession` does. Losing the vocabulary
        // costs the practitioner one filter on a list they can still read and
        // search; replacing the whole list with an error because a *filter's*
        // options failed to load would be a worse trade.
        if (controller.signal.aborted) return
        setTags([])
        setLoading(false)
      })

    return () => controller.abort()
  }, [])

  return { tags, loading }
}
