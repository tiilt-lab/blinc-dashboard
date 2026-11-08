import { describe, it, expect } from "vitest"
import { TIMED_STEPS, advance, cancel, countdownNumber } from "./baseline-protocol"

describe("baseline protocol", () => {
    it("runs countdowns, both rests, the game and the discussion start in order", () => {
        let step = null
        const steps = []
        const markers = []
        do {
            const { next, markers: m } = advance(step)
            markers.push(...m)
            step = next
            if (step) steps.push(step)
        } while (step !== null)
        expect(steps).toEqual([
            "countdown_open",
            "eyes_open",
            "countdown_closed",
            "eyes_closed",
            "game_intro",
            "game",
            "discussion_ready",
        ])
        expect(markers).toEqual([
            "baseline_started",
            "eyes_open_start",
            "eyes_open_end",
            "eyes_closed_start",
            "eyes_closed_end",
            "game_start",
            "game_end",
            "discussion_start",
        ])
    })

    it("times the countdowns and rests, and waits for buttons in the game", () => {
        expect(TIMED_STEPS).toEqual({ countdown_open: 3, eyes_open: 30, countdown_closed: 3, eyes_closed: 30 })
        expect(TIMED_STEPS.game).toBeUndefined()
    })

    it("cancels from any step with one marker", () => {
        expect(cancel("eyes_closed")).toEqual({ next: null, markers: ["baseline_cancelled"] })
        expect(cancel(null)).toEqual({ next: null, markers: [] })
    })

    it("counts down 3, 2, 1", () => {
        expect([3000, 2999, 2000, 1500, 1000, 1, 0].map(countdownNumber)).toEqual([3, 3, 2, 2, 1, 1, 1])
    })
})
