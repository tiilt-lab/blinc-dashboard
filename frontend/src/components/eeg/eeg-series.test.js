import { describe, expect, it } from "vitest"
import { eegSeconds, smooth, syncCorrection } from "./eeg-series"
import { rrSequence } from "../heart-rate/heart-rate-panel"

const row = (time_ms, channel, std_uv, alpha = 1) => ({
    time_ms, channel, std_uv, delta: 1, theta: 1, alpha, beta: 1, gamma: 0,
})

describe("sync correction", () => {
    const sync = (taps, offset_ms, status = "matched") => ({ status, offset_ms, tap_ms: taps })

    it("is zero without matched taps", () => {
        expect(syncCorrection([sync([1000, 1500], 80, "no_match")])(50)).toBe(0)
    })

    it("holds one offset and interpolates the drift between two", () => {
        const one = syncCorrection([sync([10_000, 10_400], 120)])
        expect(one(500)).toBeCloseTo(0.12)
        const two = syncCorrection([sync([10_000, 10_000], 100), sync([110_000, 110_000], 300)])
        expect(two(60)).toBeCloseTo(0.2)
        expect(two(5)).toBeCloseTo(0.1)
        expect(two(500)).toBeCloseTo(0.3)
    })
})

describe("EEG seconds", () => {
    it("averages good electrodes only and moves the second by the correction", () => {
        const rows = [
            row(5_000, "TP9", 20, 2), // alpha 100 of 100+10+10+10+1
            row(5_000, "AF7", 500, 9), // off the skin: ignored
            row(6_000, "TP9", 0.1),
        ]
        const { points, seconds, goodSeconds } = eegSeconds(rows, () => 0.25)
        expect(seconds).toBe(2)
        expect(goodSeconds).toBe(0)
        expect(points).toHaveLength(1)
        expect(points[0].t).toBeCloseTo(5.25)
        expect(points[0].relAlpha).toBeCloseTo(100 / 131)
        expect(points[0].engagement).toBeCloseTo(10 / 110)
    })

    it("smooths into 10-second buckets", () => {
        const pts = [0, 1, 2, 11].map((t) => ({ t, relAlpha: t }))
        expect(smooth(pts, "relAlpha")).toEqual([{ x: 1, y: 1 }, { x: 11, y: 11 }])
    })
})

describe("heart-rate beats", () => {
    it("places beats back from beat_ms when the row has it", () => {
        const seq = rrSequence([{ time_stamp: 10, beat_ms: 10_400, rr_ms: "800,820" }])
        expect(seq.map((p) => p.t)).toEqual([9.58, 10.4])
    })

    it("keeps the old forward spacing for rows from before the clock sync", () => {
        const seq = rrSequence([{ time_stamp: 10, rr_ms: "800,820" }])
        expect(seq.map((p) => p.t)).toEqual([10, 10.8])
    })
})
