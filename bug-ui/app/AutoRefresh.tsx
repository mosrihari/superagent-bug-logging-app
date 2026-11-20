"use client"
import { useEffect } from "react"
import { useRouter } from "next/navigation"

/** Re-renders the board's server data every few seconds so the brain's updates show up without a manual reload.
 *  Skips ticks while the tab is hidden, and refreshes straight away when it becomes visible again. */
export default function AutoRefresh({ seconds = 5 }: { seconds?: number }) {
  const router = useRouter()
  useEffect(() => {
    const tick = () => { if (document.visibilityState === "visible") router.refresh() }
    const id = setInterval(tick, seconds * 1000)
    document.addEventListener("visibilitychange", tick)
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", tick) }
  }, [router, seconds])
  return null
}
