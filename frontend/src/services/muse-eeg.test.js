import { describe, expect, it } from "vitest"
import {
    EpochAssembler,
    RAW_MISSING,
    RawBlockAssembler,
    TapDetector,
    bandPower,
    contactQuality,
    decodeAccelerometer,
    decodeEeg,
    encodeBlock,
    encodeCommand,
} from "./muse-eeg"

describe("Muse protocol", () => {
    it("length-prefixes commands", () => {
        expect(Array.from(encodeCommand("p21"))).toEqual([4, 112, 50, 49, 10])
    })

    it("unpacks twelve 12-bit samples around the 0x800 midpoint", () => {
        const dv = new DataView(new ArrayBuffer(20))
        dv.setUint16(0, 513)
        // Pairs packed as 0xABC, 0xDEF -> bytes AB CD EF; 0x800 is 0 µV.
        const pairs = [[0x800, 0x801], [0x000, 0xfff], [0x800, 0x800], [0x800, 0x800], [0x800, 0x800], [0x800, 0x800]]
        pairs.forEach(([a, b], i) => {
            dv.setUint8(2 + i * 3, a >> 4)
            dv.setUint8(3 + i * 3, ((a & 0xf) << 4) | (b >> 8))
            dv.setUint8(4 + i * 3, b & 0xff)
        })
        const { seq, counts, samples } = decodeEeg(dv)
        expect(seq).toBe(513)
        expect(counts.slice(0, 4)).toEqual([0, 1, -2048, 2047])
        expect(samples.length).toBe(12)
        expect(samples[0]).toBe(0)
        expect(samples[1]).toBeCloseTo(0.48828125)
        expect(samples[2]).toBeCloseTo(-1000)
        expect(samples[3]).toBeCloseTo(999.51171875)
    })

    it("reads big-endian accelerometer triples in g", () => {
        const dv = new DataView(new ArrayBuffer(20))
        dv.setUint16(0, 7)
        dv.setInt16(2, 16384) // 1 g on x of the first sample
        dv.setInt16(18, -16384) // -1 g on z of the last
        const { seq, counts, samples } = decodeAccelerometer(dv)
        expect(seq).toBe(7)
        expect(counts[0][0]).toBe(16384)
        expect(samples[0][0]).toBeCloseTo(1, 3)
        expect(samples[2][2]).toBeCloseTo(-1, 3)
    })
})

describe("band power", () => {
    const sine = (hz, amp, n = 256, rate = 256) =>
        Array.from({ length: n }, (_, i) => amp * Math.sin((2 * Math.PI * hz * i) / rate))

    it("puts a 10 Hz rhythm in alpha", () => {
        const { bands, std } = bandPower(sine(10, 20))
        const top = Object.entries(bands).sort((a, b) => b[1] - a[1])[0][0]
        expect(top).toBe("alpha")
        // A sine of amplitude A has power A²/2 = 200 µV²; most of it in alpha.
        expect(bands.alpha).toBeGreaterThan(Math.log10(150))
        expect(bands.alpha).toBeLessThan(Math.log10(230))
        expect(std).toBeCloseTo(20 / Math.SQRT2, 0)
    })

    it("reads contact from the spread", () => {
        expect(contactQuality(15)).toBe("good")
        expect(contactQuality(0.2)).toBe("poor")
        expect(contactQuality(400)).toBe("poor")
        expect(contactQuality(null)).toBe("none")
    })
})

describe("epochs and taps", () => {
    it("emits whole 1-second epochs by sample index and drops one with a lost packet", () => {
        const got = []
        const a = new EpochAssembler((first, buf) => got.push([first, buf.length]))
        for (let p = 0; p < 64; p++) {
            if (p === 5) continue // lost packet in epoch 0
            a.push(p * 12, new Array(12).fill(1))
        }
        expect(got).toEqual([[256, 256], [512, 256]])
    })

    it("keeps raw counts in 1-second blocks, interleaved, with a lost packet marked", () => {
        const got = []
        const a = new RawBlockAssembler((first, data) => got.push([first, Array.from(data)]), {
            channels: 2,
            size: 4,
        })
        // Two channels, packets of two samples; channel 1 loses samples 2-3.
        for (let p = 0; p < 6; p++) {
            a.push(0, p * 2, [p * 10, p * 10 + 1])
            if (p !== 1) a.push(1, p * 2, [-(p * 10), -(p * 10 + 1)])
        }
        // Block 0 waits for its gap until two newer blocks have started, so
        // it goes after block 1; the server orders by time.
        expect(got.map(([first]) => first)).toEqual([4, 0, 8])
        expect(got[1]).toEqual([0, [0, 0, 1, -1, 10, RAW_MISSING, 11, RAW_MISSING]])
        expect(got[0]).toEqual([4, [20, -20, 21, -21, 30, -30, 31, -31]])
    })

    it("encodes a block as little-endian int16 base64", () => {
        const bytes = atob(encodeBlock(Int16Array.from([1, -2, RAW_MISSING])))
        expect(Array.from(bytes, (c) => c.charCodeAt(0))).toEqual([1, 0, 0xfe, 0xff, 0x00, 0x80])
    })

    it("fires once per knock, not on head movement", () => {
        const d = new TapDetector()
        const taps = []
        for (let i = 0; i < 200; i++) {
            const t = i * 19
            const sway = 0.05 * Math.sin(i / 10)
            const knock = i === 50 || i === 51 || i === 75 ? 1.2 : 0
            if (d.push(t, [sway + knock, 0, 1]) != null) taps.push(t)
        }
        expect(taps).toEqual([50 * 19, 75 * 19])
    })
})
