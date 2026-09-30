import { describe, it, expect, vi, afterEach } from "vitest"
import { backoffDelay, mergeById, maxId, startPolling } from "./globals"

describe("backoffDelay (exponential, full jitter, capped)", () => {
    it("stays within [base/2, min(max, base*2^attempt)] for every attempt", () => {
        for (let attempt = 0; attempt < 12; attempt++) {
            const cap = Math.min(30000, 1000 * 2 ** attempt)
            for (const r of [0, 0.25, 0.5, 0.999, 1]) {
                const d = backoffDelay(attempt, 1000, 30000, () => r)
                expect(d).toBeGreaterThanOrEqual(500)
                expect(d).toBeLessThanOrEqual(cap)
            }
        }
    })
    it("reaches the 30 s cap by the 5th attempt and never exceeds it", () => {
        expect(backoffDelay(5, 1000, 30000, () => 1)).toBe(30000)
        expect(backoffDelay(40, 1000, 30000, () => 1)).toBe(30000)
    })
    it("jitters: different random draws give different delays", () => {
        const a = backoffDelay(3, 1000, 30000, () => 0.1)
        const b = backoffDelay(3, 1000, 30000, () => 0.9)
        expect(a).toBeLessThan(b)
    })
    it("treats a bad attempt count as the first attempt", () => {
        expect(backoffDelay(undefined, 1000, 30000, () => 1)).toBe(1000)
        expect(backoffDelay(-3, 1000, 30000, () => 1)).toBe(1000)
    })
    it("uses Math.random by default (in range)", () => {
        const d = backoffDelay(2, 2000, 30000)
        expect(d).toBeGreaterThanOrEqual(1000)
        expect(d).toBeLessThanOrEqual(8000)
    })
})

describe("mergeById", () => {
    it("appends unknown ids in order and replaces known ids in place", () => {
        const cur = [{ id: 1, v: "a" }, { id: 2, v: "b" }]
        const out = mergeById(cur, [{ id: 2, v: "B" }, { id: 3, v: "c" }])
        expect(out).toEqual([{ id: 1, v: "a" }, { id: 2, v: "B" }, { id: 3, v: "c" }])
    })
    it("never mutates its inputs", () => {
        const cur = [{ id: 1 }]
        const inc = [{ id: 1, x: 1 }, { id: 5 }]
        mergeById(cur, inc)
        expect(cur).toEqual([{ id: 1 }])
        expect(inc).toHaveLength(2)
    })
    it("returns a copy of current for an empty or missing batch", () => {
        const cur = [{ id: 1 }]
        expect(mergeById(cur, [])).toEqual(cur)
        expect(mergeById(cur, null)).toEqual(cur)
        expect(mergeById(null, null)).toEqual([])
    })
    it("appends rows without an id instead of collapsing them", () => {
        const out = mergeById([{ v: 1 }], [{ v: 2 }, { v: 3 }])
        expect(out).toHaveLength(3)
    })
    it("de-duplicates repeats inside one batch", () => {
        const out = mergeById([], [{ id: 7, v: 1 }, { id: 7, v: 2 }])
        expect(out).toEqual([{ id: 7, v: 2 }])
    })
})

describe("maxId (after_id cursor)", () => {
    it("is the highest numeric id, never below the floor", () => {
        expect(maxId([{ id: 3 }, { id: 9 }, { id: 5 }])).toBe(9)
        expect(maxId([{ id: 3 }], 10)).toBe(10)
        expect(maxId([], 4)).toBe(4)
        expect(maxId(null)).toBe(0)
    })
    it("ignores rows with missing or non-numeric ids", () => {
        expect(maxId([{ id: "12" }, {}, { id: 2 }])).toBe(2)
    })
})

describe("startPolling", () => {
    let doc
    const stubDoc = (hidden = false) => {
        const listeners = {}
        doc = {
            hidden,
            addEventListener: (ev, fn) => { listeners[ev] = fn },
            removeEventListener: (ev, fn) => { if (listeners[ev] === fn) delete listeners[ev] },
            fire: (ev) => listeners[ev] && listeners[ev](),
            listeners,
        }
        vi.stubGlobal("document", doc)
    }
    afterEach(() => {
        vi.unstubAllGlobals()
        vi.useRealTimers()
    })

    it("chains after each response and never overlaps", async () => {
        vi.useFakeTimers()
        stubDoc()
        let active = 0
        let maxActive = 0
        let calls = 0
        const stop = startPolling(async () => {
            calls++
            active++
            maxActive = Math.max(maxActive, active)
            await new Promise((r) => setTimeout(r, 500)) // slow request
            active--
            return true
        }, 2000)
        await vi.advanceTimersByTimeAsync(9999)
        stop()
        expect(maxActive).toBe(1)
        // 500 ms request + 2000 ms gap = one call per 2.5 s: t = 0, 2.5, 5, 7.5
        expect(calls).toBe(4)
    })

    it("backs off on failure (up to 30 s) and recovers on success", async () => {
        vi.useFakeTimers()
        stubDoc()
        vi.spyOn(Math, "random").mockReturnValue(1) // deterministic: max of the range
        const stamps = []
        let fail = true
        const stop = startPolling(async () => {
            stamps.push(Date.now())
            return !fail
        }, 2000)
        // failures: 4 s, 8 s, 16 s, 30 s, 30 s
        await vi.advanceTimersByTimeAsync(4000 + 8000 + 16000 + 30000 + 30000)
        const gaps = stamps.slice(1).map((t, i) => t - stamps[i])
        expect(gaps).toEqual([4000, 8000, 16000, 30000, 30000])
        fail = false
        await vi.advanceTimersByTimeAsync(30000 + 2000 + 2000)
        const after = stamps.slice(-3)
        expect(after[2] - after[1]).toBe(2000)
        stop()
    })

    it("treats a thrown error as a failure", async () => {
        vi.useFakeTimers()
        stubDoc()
        vi.spyOn(Math, "random").mockReturnValue(1)
        const stamps = []
        const stop = startPolling(async () => {
            stamps.push(Date.now())
            throw new Error("boom")
        }, 2000)
        await vi.advanceTimersByTimeAsync(4000)
        expect(stamps).toHaveLength(2)
        stop()
    })

    it("pauses while hidden and resumes on visibilitychange", async () => {
        vi.useFakeTimers()
        stubDoc()
        let calls = 0
        const stop = startPolling(async () => { calls++; return true }, 2000)
        await vi.advanceTimersByTimeAsync(0)
        expect(calls).toBe(1)
        doc.hidden = true
        await vi.advanceTimersByTimeAsync(20000)
        expect(calls).toBe(1) // the next tick saw hidden and did not schedule
        doc.hidden = false
        doc.fire("visibilitychange")
        await vi.advanceTimersByTimeAsync(0)
        expect(calls).toBe(2)
        await vi.advanceTimersByTimeAsync(2000)
        expect(calls).toBe(3)
        stop()
    })

    it("stop() aborts the in-flight request, drops its result and unhooks the listener", async () => {
        vi.useFakeTimers()
        stubDoc()
        let signal = null
        let calls = 0
        const stop = startPolling(async (s) => {
            calls++
            signal = s
            await new Promise((r) => setTimeout(r, 1000))
            return true
        }, 2000)
        await vi.advanceTimersByTimeAsync(100)
        expect(signal.aborted).toBe(false)
        stop()
        expect(signal.aborted).toBe(true)
        expect(doc.listeners.visibilitychange).toBeUndefined()
        await vi.advanceTimersByTimeAsync(10000)
        expect(calls).toBe(1) // nothing rescheduled after stop
    })
})
