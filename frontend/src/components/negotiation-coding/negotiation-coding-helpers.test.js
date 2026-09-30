import { describe, it, expect, vi, afterEach } from "vitest"
import {
    rollupTable,
    fmtClock,
    fmtElapsed,
    describeRun,
    speakerTags,
    assignedTeams,
    sameTeams,
    teamOf,
    badgeCodes,
    matchesFilters,
    filterCodes,
    filterKey,
    startRunPolling,
} from "./negotiation-coding-helpers"

const summary = {
    by_team: {
        Pat: {
            escalating: 4,
            defusing: 1,
            neutral: 5,
            interest: 2,
            right: 3,
            power: 1,
            past_blame: 3,
            future_problem_solving: 2,
            listening: { open_question: 2, interrupt: 1 },
            utterances: 10,
        },
        Sandy: {
            escalating: 1,
            defusing: 3,
            neutral: 4,
            interest: 4,
            right: 0,
            power: 0,
            past_blame: 0,
            future_problem_solving: 5,
            listening: { paraphrase: 2 },
            utterances: 8,
        },
        unassigned: { utterances: 0 },
    },
}

describe("rollupTable", () => {
    it("has Pat and Sandy columns and hides an empty unassigned column", () => {
        const t = rollupTable(summary)
        expect(t.columns.map((c) => c.key)).toEqual(["Pat", "Sandy"])
        expect(t.columns[0].utterances).toBe(10)
    })
    it("shows unassigned when it has utterances", () => {
        const t = rollupTable({
            by_team: { ...summary.by_team, unassigned: { utterances: 3, escalating: 1 } },
        })
        expect(t.columns.map((c) => c.key)).toEqual(["Pat", "Sandy", "unassigned"])
    })
    it("has four sections with counts and percentages per code", () => {
        const t = rollupTable(summary)
        expect(t.sections.map((s) => s.key)).toEqual(["emotion", "rip", "frame", "listening"])
        const esc = t.sections[0].rows.find((r) => r.code === "escalating")
        expect(esc.cells).toEqual([
            { n: 4, pct: 40 },
            { n: 1, pct: 13 },
        ])
        const listen = t.sections[3].rows.find((r) => r.code === "open_question")
        expect(listen.cells[0]).toEqual({ n: 2, pct: 20 })
        expect(listen.cells[1]).toEqual({ n: 0, pct: 0 })
    })
    it("omits the uncounted none rows and tolerates a missing summary", () => {
        const t = rollupTable(summary)
        expect(t.sections[1].rows.map((r) => r.code)).toEqual(["interest", "right", "power"])
        const empty = rollupTable(null)
        expect(empty.columns.map((c) => c.utterances)).toEqual([0, 0])
        expect(empty.sections[0].rows[0].cells[0]).toEqual({ n: 0, pct: null })
    })
})

describe("clock and elapsed formatting", () => {
    it("formats seconds as mm:ss and hours past 60 min", () => {
        expect(fmtClock(0)).toBe("00:00")
        expect(fmtClock(65)).toBe("01:05")
        expect(fmtClock(754.9)).toBe("12:34")
        expect(fmtClock(3661)).toBe("01:01:01")
    })
    it("shows a dash for a missing timeline value", () => {
        expect(fmtClock(null)).toBe("—")
        expect(fmtClock(undefined)).toBe("—")
    })
    it("formats elapsed time", () => {
        expect(fmtElapsed(7)).toBe("7s")
        expect(fmtElapsed(65)).toBe("1m 05s")
        expect(fmtElapsed(-3)).toBe("0s")
    })
})

describe("describeRun", () => {
    it("covers every run status", () => {
        expect(describeRun(null).text).toBe("Never run")
        expect(describeRun({ status: "queued" })).toEqual({ tone: "orange", text: "Queued" })
        const now = Date.parse("2026-09-30T10:01:05Z")
        expect(describeRun({ status: "running", started_at: "2026-09-30T10:00:00Z" }, now)).toEqual({
            tone: "orange",
            text: "Running · 1m 05s",
        })
        expect(describeRun({ status: "done", finished_at: "2026-09-30T10:05:00Z" }).text).toMatch(
            /^Done · /,
        )
        expect(describeRun({ status: "error", error: "model timed out" })).toEqual({
            tone: "danger",
            text: "Error: model timed out",
        })
    })
})

describe("team helpers", () => {
    it("lists every speaker tag from transcript, codes and assignments, sorted", () => {
        const tags = speakerTags(
            { Zed: 3, Amy: 2 },
            [{ speaker_tag: "Bob" }, { speaker_tag: null }],
            { Cal: "Pat" },
        )
        expect(tags).toEqual(["Amy", "Bob", "Cal", "Zed"])
    })
    it("keeps only Pat/Sandy assignments", () => {
        expect(assignedTeams({ a: "Pat", b: "unassigned", c: "Sandy", d: "x" })).toEqual({
            a: "Pat",
            c: "Sandy",
        })
    })
    it("compares assignments ignoring unassigned entries", () => {
        expect(sameTeams({ a: "Pat", b: "unassigned" }, { a: "Pat" })).toBe(true)
        expect(sameTeams({ a: "Pat" }, { a: "Sandy" })).toBe(false)
        expect(sameTeams({ a: "Pat" }, { a: "Pat", b: "Sandy" })).toBe(false)
    })
    it("resolves an utterance's team, defaulting to unassigned", () => {
        expect(teamOf({ speaker_tag: "a" }, { a: "Sandy" })).toBe("Sandy")
        expect(teamOf({ speaker_tag: "b" }, { a: "Sandy" })).toBe("unassigned")
    })
})

const rows = [
    { transcript_id: 1, emotion: "escalating", rip: "power", frame: "past_blame", listening: [] },
    { transcript_id: 2, emotion: "escalating", rip: "interest", frame: "none", listening: ["open_question"] },
    { transcript_id: 3, emotion: "neutral", rip: "none", frame: "none", listening: ["paraphrase", "acknowledge"] },
    { transcript_id: 4, emotion: "defusing", rip: "none", frame: "future_problem_solving", listening: null },
]

describe("filters", () => {
    it("matches everything with no selection", () => {
        expect(filterCodes(rows, new Set())).toHaveLength(4)
        expect(filterCodes(rows, null)).toHaveLength(4)
    })
    it("ORs chips within a dimension", () => {
        const sel = new Set([filterKey("emotion", "escalating"), filterKey("emotion", "defusing")])
        expect(filterCodes(rows, sel).map((r) => r.transcript_id)).toEqual([1, 2, 4])
    })
    it("ANDs across dimensions", () => {
        const sel = new Set([filterKey("emotion", "escalating"), filterKey("rip", "power")])
        expect(filterCodes(rows, sel).map((r) => r.transcript_id)).toEqual([1])
    })
    it("filters multi-label listening codes and the quiet neutral/none values", () => {
        expect(filterCodes(rows, new Set(["listening:acknowledge"])).map((r) => r.transcript_id)).toEqual([3])
        expect(filterCodes(rows, new Set(["rip:none"])).map((r) => r.transcript_id)).toEqual([3, 4])
        expect(matchesFilters(rows[3], new Set(["listening:interrupt"]))).toBe(false)
    })
    it("badges skip neutral/none but keep every real code", () => {
        expect(badgeCodes(rows[2]).map((c) => c.code)).toEqual(["paraphrase", "acknowledge"])
        expect(badgeCodes(rows[0]).map((c) => `${c.dim}:${c.code}`)).toEqual([
            "emotion:escalating",
            "rip:power",
            "frame:past_blame",
        ])
        expect(badgeCodes(rows[0])[2].label).toBe("Past / blame")
    })
})

describe("startRunPolling", () => {
    afterEach(() => vi.useRealTimers())

    it("polls every interval while the run is active and stops when it settles", async () => {
        vi.useFakeTimers()
        const results = [{ status: "running" }, { status: "running" }, { status: "done" }]
        const poll = vi.fn(() => Promise.resolve(results.shift()))
        const seen = []
        startRunPolling({
            poll,
            intervalMs: 5000,
            onResult: (r) => {
                seen.push(r.status)
                return r.status !== "done"
            },
        })
        expect(poll).not.toHaveBeenCalled()
        await vi.advanceTimersByTimeAsync(5000)
        expect(poll).toHaveBeenCalledTimes(1)
        await vi.advanceTimersByTimeAsync(10000)
        expect(poll).toHaveBeenCalledTimes(3)
        expect(seen).toEqual(["running", "running", "done"])
        await vi.advanceTimersByTimeAsync(60000)
        expect(poll).toHaveBeenCalledTimes(3)
    })

    it("stop() (the effect cleanup on unmount) ends polling, even mid-request", async () => {
        vi.useFakeTimers()
        let resolveInFlight
        const poll = vi.fn(
            () =>
                new Promise((resolve) => {
                    resolveInFlight = resolve
                }),
        )
        const onResult = vi.fn(() => true)
        const stop = startRunPolling({ poll, onResult, intervalMs: 5000 })
        await vi.advanceTimersByTimeAsync(5000)
        expect(poll).toHaveBeenCalledTimes(1)
        stop()
        resolveInFlight({ status: "running" })
        await vi.advanceTimersByTimeAsync(30000)
        expect(onResult).not.toHaveBeenCalled()
        expect(poll).toHaveBeenCalledTimes(1)
    })

    it("stop() before the first tick issues no request at all", async () => {
        vi.useFakeTimers()
        const poll = vi.fn(() => Promise.resolve({ status: "running" }))
        const stop = startRunPolling({ poll, onResult: () => true, intervalMs: 5000 })
        stop()
        await vi.advanceTimersByTimeAsync(30000)
        expect(poll).not.toHaveBeenCalled()
    })

    it("keeps polling after a failed request", async () => {
        vi.useFakeTimers()
        const poll = vi
            .fn()
            .mockRejectedValueOnce(new Error("network"))
            .mockResolvedValue({ status: "done" })
        const onResult = vi.fn(() => false)
        startRunPolling({ poll, onResult, intervalMs: 5000 })
        await vi.advanceTimersByTimeAsync(10000)
        expect(poll).toHaveBeenCalledTimes(2)
        expect(onResult).toHaveBeenCalledTimes(1)
    })
})
