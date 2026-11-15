import { beforeEach, describe, expect, it, vi } from "vitest"

let now = 0
vi.mock("./server-clock", () => ({ localNow: () => now }))

const {
    E4Connection,
    StreamTimeline,
    decodeAcc,
    decodeBvp,
    decodeButton,
    decodeEda,
    decodeTemp,
    e4Label,
    edaMicrosiemens,
    startCommand,
    tempCelsius,
} = await import("./e4-wristband")
const { RAW_MISSING_32 } = await import("./raw-blocks")

const dv = (hex) => new DataView(Uint8Array.from(hex.match(/../g).map((h) => parseInt(h, 16))).buffer)

// Real notifications from a band (multimodal-data-collection, session
// 2026-11-13_19-15-59), with what the Python recorder decoded from them.
const REAL = {
    bvp: "f807d75de289040be437c0ffc20811e3f01040e6",
    eda: "28ffa221dfa220bfa2207fa2207fa2206fa20001",
    temp: "4e3b4e3b4e3b4e3b4e3b4e3b4e3b4e3b00000001",
    acc: "f3e736f3ea38f5ec39f5eb3af8eb3cf9ec3d8901",
    button: "0188000000880000000a300a3000000000000000",
}

// BVP packets for the replay: 11 (green, red) differences, 7 bits each,
// then a 6-bit counter.
const bvpPacket = (pairs, counter) => {
    let bits = ""
    for (const [g, r] of pairs) bits += ((g + 128) & 0x7f).toString(2).padStart(7, "0") + ((r + 128) & 0x7f).toString(2).padStart(7, "0")
    bits += (counter & 0x3f).toString(2).padStart(6, "0")
    const bytes = new Uint8Array(20)
    for (let i = 0; i < 20; i++) bytes[i] = parseInt(bits.slice(i * 8, i * 8 + 8), 2)
    return new DataView(bytes.buffer)
}

const fromBase64 = (b64, width) => {
    const bin = atob(b64)
    const view = new DataView(Uint8Array.from(bin, (c) => c.charCodeAt(0)).buffer)
    const out = []
    for (let i = 0; i < bin.length; i += width) out.push(width === 4 ? view.getInt32(i, true) : view.getInt16(i, true))
    return out
}

describe("E4 decoders on real packets", () => {
    it("BVP: 11 green/red differences and a 6-bit counter", () => {
        const { seq, counts } = decodeBvp(dv(REAL.bvp))
        expect(seq).toBe(38)
        expect(counts).toEqual([[-4, 1], [-6, -11], [-17, 10], [18, 4], [5, -7], [6, -4], [7, -1], [4, 8], [8, -8], [-2, 1], [2, 3]])
    })

    it("EDA: six 24-bit counts and a uint16 counter", () => {
        const { seq, counts } = decodeEda(dv(REAL.eda))
        expect(seq).toBe(1)
        expect(counts.map((c) => c[0])).toEqual([2686882, 2219938, 2146210, 2129826, 2129826, 2125730])
        expect(edaMicrosiemens(2686882)).toBeCloseTo(0.372179, 6)
    })

    it("temperature: eight uint16 counts and a uint32 counter", () => {
        const { seq, counts } = decodeTemp(dv(REAL.temp))
        expect(seq).toBe(1)
        expect(counts).toEqual(Array(8).fill([15182]))
        expect(tempCelsius(15182)).toBeCloseTo(28.1, 6)
    })

    it("accelerometer: six int8 triples, the counter in the last byte", () => {
        const { seq, counts } = decodeAcc(dv(REAL.acc))
        expect(seq).toBe(1)
        expect(counts[0]).toEqual([-13, -25, 54])
        expect(counts[5]).toEqual([-7, -20, 61])
    })

    it("button: the press time in 1/64 s; anything else is not a press", () => {
        expect(decodeButton(dv(REAL.button))).toBe(136)
        expect(decodeButton(dv("02880000"))).toBeNull()
        expect(decodeBvp(dv("0102"))).toBeNull()
    })

    it("start command: 0x01 and the Unix time, little-endian", () => {
        expect(Array.from(startCommand(0x6a1b2c3d))).toEqual([1, 0x3d, 0x2c, 0x1b, 0x6a])
    })

    it("labels a band by name and a short id", () => {
        expect(e4Label({ name: "Empatica E4", id: "x/Ab+12cd==" })).toBe("Empatica E4 xAb1")
        expect(e4Label(null)).toBe("Empatica E4")
    })
})

describe("StreamTimeline", () => {
    const bvp = () => new StreamTimeline({ rate: 64, perPacket: 11, counterMax: 63 })
    const gap = (11 * 1000) / 64

    it("counts samples from the first packet and wraps 63 -> 1 without a loss", () => {
        const tl = bvp()
        const firsts = []
        for (let p = 0; p < 70; p++) firsts.push(tl.push((p % 63) + 1, p * gap))
        expect(firsts[0]).toBe(0)
        expect(firsts[62]).toBe(62 * 11)
        expect(firsts[63]).toBe(63 * 11)
        expect(tl.lost).toBe(0)
    })

    it("counts lost packets, including whole cycles from arrival time", () => {
        const tl = bvp()
        tl.push(1, 0)
        expect(tl.push(4, 3 * gap)).toBe(3 * 11)
        expect(tl.lost).toBe(2)
        // 70 packets later (a ~12 s stall): the counter alone says 7.
        expect(tl.push(((3 + 70) % 63) + 1, 73 * gap)).toBe(73 * 11)
        expect(tl.lost).toBe(2 + 69)
        expect(tl.push(((3 + 70) % 63) + 1, 73 * gap + 5)).toBeNull()
    })
})

describe("E4Connection replay", () => {
    beforeEach(() => {
        now = 1_000_000
    })

    it("sends raw blocks per second and places a button press on the BVP timeline", () => {
        const raw = []
        const presses = []
        const conn = new E4Connection({ onRaw: (b) => raw.push(b), onButton: (b) => presses.push(b) })
        const gap = (11 * 1000) / 64
        const start = now
        for (let p = 0; p < 130; p++) {
            now = start + (p + 1) * gap + (p % 5) * 7 // late by 0-28 ms
            const pairs = Array.from({ length: 11 }, (_, k) => [((p * 11 + k) % 5) - 2, 1])
            conn._packet("bvp", bvpPacket(pairs, (p % 63) + 1))
        }
        const bvpBlocks = raw.filter((b) => b.stream === "bvp")
        expect(bvpBlocks.map((b) => b.index)).toEqual([0, 64, 128, 192, 256, 320, 384, 448, 512, 576, 640, 704, 768, 832, 896, 960, 1024, 1088, 1152, 1216, 1280, 1344])
        const counts = fromBase64(bvpBlocks[1].data, 2)
        expect(counts.slice(0, 4)).toEqual([((64 % 5) - 2), 1, ((65 % 5) - 2), 1])
        // Sample 0 was taken one packet before the first arrival.
        expect(bvpBlocks[0].t).toBeCloseTo(start + gap - (10 * 1000) / 64, 0)
        expect(conn.lost).toBe(0)

        now += 40
        conn._button(dv("0180020000" + "00".repeat(15))) // tick 640: BVP sample 640
        expect(presses).toHaveLength(1)
        expect(presses[0].tick).toBe(640)
        expect(presses[0].t).toBeCloseTo(bvpBlocks[10].t, 0)
    })

    it("keeps EDA as 32-bit counts with lost samples marked", () => {
        const raw = []
        const conn = new E4Connection({ onRaw: (b) => raw.push(b) })
        const packet = (base, counter) => {
            const bytes = new Uint8Array(20)
            for (let i = 0; i < 6; i++) {
                const v = base + i
                bytes.set([(v >> 16) & 0xff, (v >> 8) & 0xff, v & 0xff], i * 3)
            }
            bytes[18] = counter >> 8
            bytes[19] = counter & 0xff
            return new DataView(bytes.buffer)
        }
        now = 0
        conn._packet("eda", packet(2_000_000, 1))
        now = 3000 // packet 2 lost
        conn._packet("eda", packet(3_000_000, 3))
        now = 4500
        conn._packet("eda", packet(4_000_000, 4))
        // Blocks go as they complete; the second the lost packet covered
        // entirely (samples 8-11) has no data and is not sent.
        const eda = Object.fromEntries(raw.filter((b) => b.stream === "eda").map((b) => [b.index, b.data]))
        expect(Object.keys(eda).map(Number).sort((a, b) => a - b)).toEqual([0, 4, 12, 16, 20])
        expect(fromBase64(eda[0], 4)).toEqual([2_000_000, 2_000_001, 2_000_002, 2_000_003])
        expect(fromBase64(eda[4], 4)).toEqual([2_000_004, 2_000_005, RAW_MISSING_32, RAW_MISSING_32])
        expect(fromBase64(eda[16], 4)).toEqual([3_000_004, 3_000_005, 4_000_000, 4_000_001])
        expect(conn.lost).toBe(1)
        expect(conn.latest.eda).toBeCloseTo(1e6 / 4_000_005, 9)
    })
})
