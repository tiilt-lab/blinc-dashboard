// Live wearable rows on the pod page: heart rate and EEG band power are
// loaded once, then, while the session is recording, polled every few
// seconds for rows stored since the last one the page has (the endpoints'
// ?after_id=). Each poll is a few dozen rows whatever the session length,
// and nothing is fetched while the tab is hidden.
import { useEffect, useState } from "react"

// The join page flushes wearable data every 5 s; polling faster only finds
// nothing new.
export const LIVE_POLL_MS = 5000

const rowTime = (r) => (r.time_ms != null ? r.time_ms : (r.time_stamp || 0) * 1000)
const byTime = (a, b) => rowTime(a) - rowTime(b) || a.id - b.id

export const maxId = (rows) => rows.reduce((m, r) => (r.id > m ? r.id : m), 0)

// prev plus the rows of incoming it doesn't have, in time order. A laptop
// that was offline flushes a backlog stamped earlier than rows already
// shown, so the result is re-sorted only when that happens.
export function mergeRows(prev, incoming) {
    const have = new Set(prev.map((r) => r.id))
    const fresh = incoming.filter((r) => !have.has(r.id))
    if (!fresh.length) return prev
    const out = prev.concat(fresh)
    const ordered = out.every((r, i) => i === 0 || byTime(out[i - 1], r) <= 0)
    return ordered ? out : out.sort(byTime)
}

// Framework-free core of useLiveRows: calls fetchRows(afterId) every
// intervalMs while the page is visible, at most one request at a time, and
// hands new rows to onRows. poll() runs one round now.
export function createPoller({ fetchRows, onRows, afterId, intervalMs = LIVE_POLL_MS, isHidden = () => false }) {
    let last = afterId
    let busy = false
    let stopped = false
    const poll = async () => {
        if (busy || stopped || isHidden()) return
        busy = true
        try {
            const rows = await fetchRows(last)
            if (!stopped && rows.length) {
                last = Math.max(last, maxId(rows))
                onRows(rows)
            }
        } catch {
            // A failed poll is retried at the next tick.
        } finally {
            busy = false
        }
    }
    const timer = setInterval(poll, intervalMs)
    return {
        poll,
        stop() {
            stopped = true
            clearInterval(timer)
        },
    }
}

// rows for path (relative to the API root, without query string), from
// get(path) -> Promise<array>. key changes reload from scratch; live turns
// polling on and off.
export function useLiveRows(get, path, enabled, live) {
    const [rows, setRows] = useState([])
    const [loadedMax, setLoadedMax] = useState(null)
    useEffect(() => {
        setRows([])
        setLoadedMax(null)
        if (!enabled) return undefined
        let cancelled = false
        get(path).then((d) => {
            if (cancelled) return
            setRows(d)
            setLoadedMax(maxId(d))
        })
        return () => {
            cancelled = true
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [path, enabled])
    useEffect(() => {
        if (!enabled || !live || loadedMax == null) return undefined
        const hidden = () => typeof document !== "undefined" && document.hidden
        const poller = createPoller({
            fetchRows: (after) => get(`${path}?after_id=${after}`),
            onRows: (fresh) => setRows((prev) => mergeRows(prev, fresh)),
            afterId: loadedMax,
            isHidden: hidden,
        })
        // Back on the tab: catch up at once rather than at the next tick.
        const onVisible = () => !hidden() && poller.poll()
        document.addEventListener("visibilitychange", onVisible)
        return () => {
            poller.stop()
            document.removeEventListener("visibilitychange", onVisible)
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [path, enabled, live, loadedMax])
    return rows
}
