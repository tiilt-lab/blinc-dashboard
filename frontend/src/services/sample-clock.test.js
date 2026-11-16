import { describe, expect, it } from "vitest"
import { DeviceClock, SequenceCounter } from "./sample-clock"
import { ServerClock, withServerTimes } from "./server-clock"
import { BeatClock, parseHeartRateMeasurement } from "./polar-hr"

// A deterministic "random" delay in [0, max).
const jitter = (i, max) => ((Math.sin(i * 12.9898) * 43758.5453) % 1 + 1) % 1 * max

describe("DeviceClock", () => {
    it("recovers the offset from the lower envelope of delayed arrivals", () => {
        const c = new DeviceClock()
        const offset = 1_000_000
        for (let i = 0; i < 600; i++) {
            const d = i * 47 // packets every 47 ms of device time
            c.add(d, d + offset + jitter(i, 80))
        }
        // Mapped device time lands within a few ms of the true instant,
        // where stamping by arrival would be off by 40 ms on average.
        expect(Math.abs(c.toLocal(20_000) - (20_000 + offset))).toBeLessThan(5)
    })

    it("follows a sensor crystal running 100 ppm fast", () => {
        const c = new DeviceClock()
        for (let i = 0; i < 2000; i++) {
            const real = i * 50
            const d = real * 1.0001
            c.add(d, real + 500 + jitter(i, 30))
        }
        const d = 99_950 * 1.0001
        expect(Math.abs(c.toLocal(d) - (99_950 + 500))).toBeLessThan(5)
    })

    it("starts over when the device timeline jumps", () => {
        const c = new DeviceClock({ resetMs: 1000, maxDelayMs: 300 })
        for (let i = 0; i < 100; i++) c.add(i * 100, i * 100 + 5000)
        // The counter restarts at 0 ten seconds later.
        c.add(0, 20_000 + 5000)
        expect(c.toLocal(0)).toBe(25_000)
    })
})

describe("SequenceCounter", () => {
    it("unwraps a uint16 counter and counts lost packets", () => {
        const s = new SequenceCounter()
        expect(s.next(65534)).toBe(65534)
        expect(s.next(65535)).toBe(65535)
        expect(s.next(1)).toBe(65537) // packet 0 was lost
        expect(s.next(1)).toBeNull() // duplicate
        expect(s.next(0)).toBeNull() // late
    })
})

describe("ServerClock", () => {
    it("keeps the probe with the shortest round trip", async () => {
        let now = 1000
        const trips = [[80, 40], [10, 5], [60, 10]] // [rtt, where in the trip the server read its clock]
        let i = 0
        const clock = new ServerClock({
            now: () => now,
            probes: 3,
            fetchServerMs: async () => {
                const [rtt, at] = trips[i++]
                const server = now + at + 5000 // server runs 5 s ahead
                now += rtt
                return server
            },
        })
        const best = await clock.sync()
        expect(best.rtt).toBe(10)
        expect(Math.abs(clock.toServer(2000) - 7000)).toBeLessThanOrEqual(5)
    })

    it("is unsynced until a probe succeeds, and stamps server_ fields once synced", async () => {
        const clock = new ServerClock({ now: () => 0, probes: 1, fetchServerMs: async () => { throw new Error("offline") } })
        await clock.sync()
        expect(clock.synced).toBe(false)
        expect(withServerTimes(clock, { t: 5 })).toEqual({ t: 5 })
        clock.fetchServerMs = async () => 100
        await clock.sync()
        expect(withServerTimes(clock, { t: 5, beat: 3 }, ["t", "beat"])).toEqual({ t: 5, beat: 3, server_t: 105, server_beat: 103 })
    })
})

describe("Polar beat timing", () => {
    const packet = (hr, rr1024) => {
        const buf = new DataView(new ArrayBuffer(2 + rr1024.length * 2))
        buf.setUint8(0, 0x10)
        buf.setUint8(1, hr)
        rr1024.forEach((v, i) => buf.setUint16(2 + i * 2, v, true))
        return buf
    }

    it("keeps the raw 1/1024 s intervals beside the rounded ms", () => {
        expect(parseHeartRateMeasurement(packet(70, [820, 1000]))).toEqual({ hr: 70, rr: [801, 977], rr1024: [820, 1000] })
    })

    it("places each notification's last beat by the RR sum, not the arrival", () => {
        // Beats every 800 ms; the strap sends once a second with whatever
        // beats ended since, arriving 20-60 ms later.
        const bc = new BeatClock()
        const beatsAt = Array.from({ length: 200 }, (_, k) => 3000 + k * 800)
        let errs = []
        for (let s = 1; s < 150; s++) {
            const sendAt = 3000 + s * 1000
            const inPacket = beatsAt.filter((b) => b <= sendAt && b > sendAt - 1000)
            const beat = bc.add(inPacket.map(() => 819.2), sendAt + 20 + jitter(s, 40))
            if (inPacket.length && s > 30) errs.push(Math.abs(beat - inPacket[inPacket.length - 1]))
        }
        // Arrival stamps would be up to a second off; the fit holds the
        // beat to within the delay floor.
        expect(Math.max(...errs)).toBeLessThan(40)
    })
})
