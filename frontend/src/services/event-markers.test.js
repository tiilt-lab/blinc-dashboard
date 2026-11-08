import { describe, it, expect } from "vitest"
import { MarkerQueue, describeSensors } from "./event-markers"

const manualTimers = () => {
    let pending = []
    let nextId = 1
    return {
        setTimer: (fn, ms) => {
            pending.push({ id: nextId, fn, ms })
            return nextId++
        },
        clearTimer: (id) => {
            pending = pending.filter((t) => t.id !== id)
        },
        fire: () => pending.splice(0).forEach((t) => t.fn()),
        count: () => pending.length,
    }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

describe("MarkerQueue", () => {
    it("times a marker when pressed and sends it at once", async () => {
        const sent = []
        let clock = 1000
        const q = new MarkerQueue({ send: async (b) => sent.push(b) && true, now: () => clock })
        q.mark("session_joined", { sensors: "polar:Polar H10 A1 (Ana)" })
        clock = 5000
        await settle()
        expect(sent).toHaveLength(1)
        expect(sent[0][0]).toMatchObject({ label: "session_joined", t: 1000, sensors: "polar:Polar H10 A1 (Ana)" })
        expect(sent[0][0].marker_id).toBeTruthy()
        expect(q.pending).toEqual([])
    })

    it("keeps markers through failures and resends them with the same ids", async () => {
        const timers = manualTimers()
        const attempts = []
        let up = false
        const q = new MarkerQueue({
            send: async (b) => {
                attempts.push(b.map((m) => m.marker_id))
                if (!up) throw new Error("offline")
                return true
            },
            ...timers,
        })
        const a = q.mark("eyes_open_start")
        await settle()
        const b = q.mark("eyes_open_end")
        await settle()
        expect(q.pending.map((m) => m.label)).toEqual(["eyes_open_start", "eyes_open_end"])
        expect(timers.count()).toBe(1)
        up = true
        timers.fire()
        await settle()
        expect(attempts[attempts.length - 1]).toEqual([a.marker_id, b.marker_id])
        expect(q.pending).toEqual([])
    })

    it("treats anything but true as not stored", async () => {
        const timers = manualTimers()
        const q = new MarkerQueue({ send: async () => false, ...timers })
        q.mark("game_start")
        await settle()
        expect(q.pending).toHaveLength(1)
        expect(timers.count()).toBe(1)
    })

    it("adds server time from a clock that synced after the press", async () => {
        const sent = []
        let synced = false
        let up = false
        const timers = manualTimers()
        const q = new MarkerQueue({
            send: async (b) => {
                sent.push(b)
                return up
            },
            now: () => 100,
            toServer: (t) => (synced ? t + 40 : null),
            ...timers,
        })
        q.mark("recording_started")
        await settle()
        expect(sent[0][0].server_t).toBeUndefined()
        synced = true
        up = true
        timers.fire()
        await settle()
        expect(sent[1][0]).toMatchObject({ t: 100, server_t: 140 })
    })
})

describe("describeSensors", () => {
    it("lists each connected wearable with its wearer", () => {
        expect(
            describeSensors([
                { kind: "polar", name: "Polar H10 A1", alias: "Ana" },
                { kind: "muse", name: "Muse-12AB" },
                { kind: "polar", name: "" },
            ]),
        ).toBe("polar:Polar H10 A1 (Ana); muse:Muse-12AB")
        expect(describeSensors(null)).toBe("")
    })
})
