import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { createPoller, maxId, mergeRows } from "./live-rows"

describe("mergeRows", () => {
    const row = (id, time_ms) => ({ id, time_ms })

    it("appends new rows and skips ones it already has", () => {
        const prev = [row(1, 1000), row(2, 2000)]
        expect(mergeRows(prev, [row(2, 2000), row(3, 3000)])).toEqual([row(1, 1000), row(2, 2000), row(3, 3000)])
        expect(mergeRows(prev, [row(1, 1000)])).toBe(prev)
    })

    it("puts a late backlog back in time order", () => {
        const merged = mergeRows([row(1, 5000), row(2, 6000)], [row(3, 1000), row(4, 7000)])
        expect(merged.map((r) => r.id)).toEqual([3, 1, 2, 4])
    })

    it("orders rows without time_ms by their whole second", () => {
        const merged = mergeRows([{ id: 1, time_stamp: 5 }], [{ id: 2, time_stamp: 3 }])
        expect(merged.map((r) => r.id)).toEqual([2, 1])
        expect(maxId(merged)).toBe(2)
    })
})

describe("createPoller", () => {
    beforeEach(() => vi.useFakeTimers())
    afterEach(() => vi.useRealTimers())

    it("asks for rows after the highest id it has, every interval", async () => {
        const asked = []
        const got = []
        const batches = [[{ id: 11 }, { id: 12 }], [], [{ id: 13 }]]
        const poller = createPoller({
            fetchRows: async (after) => {
                asked.push(after)
                return batches.shift() || []
            },
            onRows: (rows) => got.push(rows.map((r) => r.id)),
            afterId: 10,
            intervalMs: 5000,
        })
        await vi.advanceTimersByTimeAsync(15000)
        expect(asked).toEqual([10, 12, 12])
        expect(got).toEqual([[11, 12], [13]])
        poller.stop()
        await vi.advanceTimersByTimeAsync(10000)
        expect(asked).toHaveLength(3)
    })

    it("skips ticks while the tab is hidden or a request is still out", async () => {
        let hidden = true
        let calls = 0
        let release
        const poller = createPoller({
            fetchRows: () => {
                calls += 1
                return new Promise((r) => (release = r))
            },
            onRows: () => {},
            afterId: 0,
            intervalMs: 5000,
            isHidden: () => hidden,
        })
        await vi.advanceTimersByTimeAsync(10000)
        expect(calls).toBe(0)
        hidden = false
        await vi.advanceTimersByTimeAsync(15000)
        expect(calls).toBe(1)
        release([])
        await vi.advanceTimersByTimeAsync(5000)
        expect(calls).toBe(2)
        poller.stop()
    })

    it("keeps polling after a failed request", async () => {
        let calls = 0
        const poller = createPoller({
            fetchRows: async () => {
                calls += 1
                if (calls === 1) throw new Error("offline")
                return [{ id: 5 }]
            },
            onRows: () => {},
            afterId: 0,
            intervalMs: 1000,
        })
        await vi.advanceTimersByTimeAsync(2000)
        expect(calls).toBe(2)
        poller.stop()
    })
})
