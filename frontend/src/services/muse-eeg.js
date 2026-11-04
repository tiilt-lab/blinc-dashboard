// Web Bluetooth client for a Muse EEG headband (Muse 2016, Muse 2, Muse S
// gen 1/2), one connection per speaker. Protocol as documented by the
// muse-js project: GATT service 0xfe8d, a control characteristic taking
// length-prefixed ASCII commands, one notifying characteristic per
// electrode (TP9, AF7, AF8, TP10) carrying a uint16 packet counter and 12
// samples packed 12 bits each, at 256 Hz; an accelerometer (52 Hz, 3
// samples per packet) and a telemetry characteristic (battery). The Muse S
// Athena (2025) speaks a different protocol and is not supported here.
//
// Each electrode is reduced in the browser to log band power per 1-second
// epoch (256 samples) plus the epoch's standard deviation, which doubles as
// electrode contact; that is what the dashboard charts. The raw signal goes
// up too, separately: the headband's own integer counts for all four
// electrodes at 256 Hz and the accelerometer at 52 Hz, one block per stream
// per second (RawBlockAssembler, stored by raw_signals.py). Samples are
// timed from the packet counter, not their arrival (sample-clock.js), and
// the accelerometer finds sync taps (sync_taps.py) while a sync is armed.
//
// Web Bluetooth exists in Chrome/Edge (desktop + Android) over HTTPS only.

import { DeviceClock, SequenceCounter } from "./sample-clock"
import { localNow } from "./server-clock"

const MUSE_SERVICE = 0xfe8d
const uuid = (n) => `273e00${n}-4c4d-454d-96be-f03bac821358`
const CONTROL = uuid("01")
const TELEMETRY = uuid("0b")
const ACCELEROMETER = uuid("0a")

export const EEG_CHANNELS = ["TP9", "AF7", "AF8", "TP10"]
const EEG_CHARACTERISTICS = [uuid("03"), uuid("04"), uuid("05"), uuid("06")]
export const EEG_RATE = 256
const EEG_PER_PACKET = 12
export const EPOCH_SAMPLES = 256
const ACC_RATE = 52
const ACC_PER_PACKET = 3
const ACC_SCALE = 0.0000610352 // g per count
const EEG_SCALE = 0.48828125 // µV per count, around the 0x800 midpoint

export const BANDS = {
    delta: [1, 4],
    theta: [4, 8],
    alpha: [8, 13],
    beta: [13, 30],
    gamma: [30, 44],
}

// "X<cmd>\n" with X replaced by the length of what follows it.
export const encodeCommand = (cmd) => {
    const encoded = new TextEncoder().encode(`X${cmd}\n`)
    encoded[0] = encoded.length - 1
    return encoded
}

// An EEG notification: {seq, counts: 12 values around the 0x800 midpoint
// (-2048..2047, what the raw blocks keep), samples: the same in µV}.
export const decodeEeg = (dataView) => {
    const seq = dataView.getUint16(0)
    const counts = []
    for (let i = 2; i + 2 < 20 && i + 2 < dataView.byteLength; i += 3) {
        const b0 = dataView.getUint8(i)
        const b1 = dataView.getUint8(i + 1)
        const b2 = dataView.getUint8(i + 2)
        counts.push(((b0 << 4) | (b1 >> 4)) - 0x800)
        counts.push((((b1 & 0xf) << 8) | b2) - 0x800)
    }
    return { seq, counts, samples: counts.map((c) => EEG_SCALE * c) }
}

// An accelerometer notification: {seq, counts: 3 × [x, y, z] as sent
// (int16), samples: the same in g}.
export const decodeAccelerometer = (dataView) => {
    const counts = [0, 1, 2].map((i) => [
        dataView.getInt16(2 + i * 6),
        dataView.getInt16(4 + i * 6),
        dataView.getInt16(6 + i * 6),
    ])
    return {
        seq: dataView.getUint16(0),
        counts,
        samples: counts.map((xyz) => xyz.map((c) => ACC_SCALE * c)),
    }
}

// In-place iterative radix-2 FFT; re/im are Float64Arrays of a power-of-2
// length.
const fft = (re, im) => {
    const n = re.length
    for (let i = 1, j = 0; i < n; i++) {
        let bit = n >> 1
        for (; j & bit; bit >>= 1) j ^= bit
        j ^= bit
        if (i < j) {
            ;[re[i], re[j]] = [re[j], re[i]]
            ;[im[i], im[j]] = [im[j], im[i]]
        }
    }
    for (let len = 2; len <= n; len <<= 1) {
        const ang = (-2 * Math.PI) / len
        const wr = Math.cos(ang)
        const wi = Math.sin(ang)
        for (let i = 0; i < n; i += len) {
            let cr = 1
            let ci = 0
            for (let k = 0; k < len / 2; k++) {
                const a = i + k
                const b = a + len / 2
                const tr = re[b] * cr - im[b] * ci
                const ti = re[b] * ci + im[b] * cr
                re[b] = re[a] - tr
                im[b] = im[a] - ti
                re[a] += tr
                im[a] += ti
                const ncr = cr * wr - ci * wi
                ci = cr * wi + ci * wr
                cr = ncr
            }
        }
    }
}

// One epoch's log10 band power (µV², Hann-windowed one-sided periodogram)
// per band, and its standard deviation in µV. Bands are [lo, hi) in Hz.
export const bandPower = (samples, rate = EEG_RATE) => {
    const n = samples.length
    const mean = samples.reduce((s, v) => s + v, 0) / n
    const re = new Float64Array(n)
    const im = new Float64Array(n)
    let wss = 0
    let ss = 0
    for (let i = 0; i < n; i++) {
        const w = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1))
        const v = samples[i] - mean
        re[i] = v * w
        wss += w * w
        ss += v * v
    }
    fft(re, im)
    const df = rate / n
    const bands = {}
    for (const [name, [lo, hi]] of Object.entries(BANDS)) {
        let p = 0
        for (let k = Math.ceil(lo / df); k * df < hi && k <= n / 2; k++) {
            // PSD (µV²/Hz) × bin width, doubled for the negative frequencies.
            p += (2 * (re[k] ** 2 + im[k] ** 2)) / (rate * wss) * df
        }
        bands[name] = Math.log10(Math.max(p, 1e-12))
    }
    return { bands, std: Math.sqrt(ss / n) }
}

// A heuristic read of an epoch's spread: on the skin the Muse shows tens of
// µV (more on AF7/AF8 when the wearer blinks); off the skin it reads flat
// or swings by hundreds.
export const contactQuality = (std) => {
    if (std == null) return "none"
    if (std >= 1 && std <= 60) return "good"
    if (std > 60 && std <= 150) return "fair"
    return "poor"
}

// Knocks on the headband: the accelerometer jumps between two samples
// (19 ms apart at 52 Hz) far more than head movement can manage.
export class TapDetector {
    constructor({ thresholdG = 0.35, refractoryMs = 150 } = {}) {
        this.thresholdG = thresholdG
        this.refractoryMs = refractoryMs
        this.prev = null
        this.lastTap = -Infinity
    }

    // The sample's time if it is a tap, else null.
    push(t, [x, y, z]) {
        const prev = this.prev
        this.prev = [x, y, z]
        if (!prev) return null
        const jerk = Math.hypot(x - prev[0], y - prev[1], z - prev[2])
        if (jerk < this.thresholdG || t - this.lastTap < this.refractoryMs) return null
        this.lastTap = t
        return t
    }
}

// Collects each electrode's samples into 1-second epochs by sample index,
// so a lost packet leaves its epoch incomplete (dropped) rather than
// shifting every later sample.
export class EpochAssembler {
    constructor(onEpoch, size = EPOCH_SAMPLES) {
        this.onEpoch = onEpoch
        this.size = size
        this.open = new Map()
    }

    push(firstIndex, samples) {
        for (let k = 0; k < samples.length; k++) {
            const idx = firstIndex + k
            const e = Math.floor(idx / this.size)
            let ep = this.open.get(e)
            if (!ep) {
                ep = { buf: new Float64Array(this.size), filled: 0 }
                this.open.set(e, ep)
            }
            ep.buf[idx - e * this.size] = samples[k]
            ep.filled++
            if (ep.filled === this.size) {
                this.open.delete(e)
                this.onEpoch(e * this.size, ep.buf)
            }
        }
        const newest = Math.floor((firstIndex + samples.length - 1) / this.size)
        for (const e of this.open.keys()) if (e < newest - 1) this.open.delete(e)
    }
}

// Marks a lost sample in a raw block (raw_signals.MISSING).
export const RAW_MISSING = -32768

// Collects every channel of one stream into 1-second blocks by sample
// index: onBlock(firstIndex, Int16Array) with the counts interleaved by
// sample (s0c0, s0c1, ..., s1c0, ...). A block goes once every channel has
// filled it, or with RAW_MISSING in the gaps once two newer blocks have
// started (a lost packet), so every second that saw any data is kept and
// no sample shifts.
export class RawBlockAssembler {
    constructor(onBlock, { channels, size }) {
        this.onBlock = onBlock
        this.channels = channels
        this.size = size
        this.open = new Map()
    }

    push(channel, firstIndex, counts) {
        const per = this.size * this.channels
        for (let k = 0; k < counts.length; k++) {
            const idx = firstIndex + k
            const b = Math.floor(idx / this.size)
            let block = this.open.get(b)
            if (!block) {
                block = { data: new Int16Array(per).fill(RAW_MISSING), filled: 0 }
                this.open.set(b, block)
            }
            block.data[(idx - b * this.size) * this.channels + channel] = counts[k]
            block.filled++
            if (block.filled === per) {
                this.open.delete(b)
                this.onBlock(b * this.size, block.data)
            }
        }
        const newest = Math.floor((firstIndex + counts.length - 1) / this.size)
        for (const [b, block] of [...this.open.entries()].sort((x, y) => x[0] - y[0])) {
            if (b >= newest - 1) break
            this.open.delete(b)
            this.onBlock(b * this.size, block.data)
        }
    }
}

// Int16Array -> base64 of its little-endian bytes (what the server decodes).
export const encodeBlock = (data) => {
    const bytes = new Uint8Array(data.length * 2)
    const view = new DataView(bytes.buffer)
    data.forEach((v, i) => view.setInt16(i * 2, v, true))
    let text = ""
    for (let i = 0; i < bytes.length; i += 0x8000) {
        text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000))
    }
    return btoa(text)
}

// One managed headband. onEpoch({channel, t, bands, std}) fires per
// electrode per second, t the epoch's first sample on the page clock
// (localNow); onRaw({stream, index, t, data}) once a second per stream
// (EEG, accelerometer), data base64 counts (encodeBlock), t its first
// sample; onTap({t}) while a sync is armed; onStatus like PolarConnection.
export class MuseConnection {
    constructor({ onEpoch, onRaw, onStatus }) {
        this.onEpoch = onEpoch || (() => {})
        this.onRaw = onRaw || (() => {})
        this.onStatus = onStatus || (() => {})
        this.device = null
        this.control = null
        this.battery = null
        this.closed = false
        this.contact = {}
        this._armed = null
        this._reconnectTimer = null
        this._handlers = []
        this._resetTiming()
        this._disconnectHandler = () => {
            this.control = null
            if (this.closed) return
            this.onStatus("disconnected")
            this._reconnectTimer = setTimeout(() => this._attach().catch(() => {
                if (!this.closed) this._disconnectHandler()
            }), 3000)
        }
    }

    // A reconnect can restart the counters: every timeline starts over.
    _resetTiming() {
        this.eegClock = new DeviceClock({ resetMs: 1000, maxDelayMs: 300 })
        this.accClock = new DeviceClock({ resetMs: 1000, maxDelayMs: 300 })
        this.eegSeq = EEG_CHANNELS.map(() => new SequenceCounter())
        this.accSeq = new SequenceCounter()
        this.taps = new TapDetector()
        this.assemblers = EEG_CHANNELS.map((channel) => new EpochAssembler((first, buf) => {
            const t = this.eegClock.toLocal((first * 1000) / EEG_RATE)
            if (t == null) return
            const { bands, std } = bandPower(buf)
            this.contact[channel] = std
            this.onEpoch({ channel, t, bands, std })
        }))
        const raw = (stream, clock, rate) => (index, data) => {
            const t = clock.toLocal((index * 1000) / rate)
            if (t != null) this.onRaw({ stream, index, t, data: encodeBlock(data) })
        }
        this.rawEeg = new RawBlockAssembler(raw("eeg", this.eegClock, EEG_RATE), {
            channels: EEG_CHANNELS.length,
            size: EEG_RATE,
        })
        this.rawAcc = new RawBlockAssembler(raw("acc", this.accClock, ACC_RATE), { channels: 3, size: ACC_RATE })
    }

    // Must be called from a user gesture: opens the browser's chooser,
    // which lists headbands by name (e.g. "Muse-1A2B", printed inside the band).
    async choose() {
        this.device = await navigator.bluetooth.requestDevice({
            filters: [{ services: [MUSE_SERVICE] }, { namePrefix: "Muse" }],
            optionalServices: [MUSE_SERVICE],
        })
        this.device.addEventListener("gattserverdisconnected", this._disconnectHandler)
        await this._attach()
        return { name: this.device.name || "Muse headband", battery: this.battery }
    }

    async _send(cmd) {
        await this.control.writeValue(encodeCommand(cmd))
    }

    async _listen(service, uuidStr, handler) {
        const ch = await service.getCharacteristic(uuidStr)
        const fn = (event) => handler(event.target.value)
        ch.addEventListener("characteristicvaluechanged", fn)
        this._handlers.push([ch, fn])
        await ch.startNotifications()
    }

    async _attach() {
        if (this.closed || !this.device) return
        this.onStatus("connecting")
        this._detachHandlers()
        this._resetTiming()
        const server = await this.device.gatt.connect()
        const service = await server.getPrimaryService(MUSE_SERVICE)
        this.control = await service.getCharacteristic(CONTROL)
        for (let i = 0; i < EEG_CHANNELS.length; i++) {
            await this._listen(service, EEG_CHARACTERISTICS[i], (dv) => this._eeg(i, dv))
        }
        await this._listen(service, ACCELEROMETER, (dv) => this._acc(dv))
        await this._listen(service, TELEMETRY, (dv) => {
            this.battery = Math.round(dv.getUint16(2) / 512)
        })
        // Pause, preset 21 (four EEG channels, no aux, no PPG), start, resume.
        await this._send("h")
        await this._send("p21")
        await this._send("s")
        await this._send("d")
        this.onStatus("connected", { battery: this.battery })
    }

    _eeg(i, dataView) {
        const arrival = localNow()
        const { seq, counts, samples } = decodeEeg(dataView)
        const packet = this.eegSeq[i].next(seq)
        if (packet == null) return
        const first = packet * EEG_PER_PACKET
        // The packet arrived once its last sample was taken.
        this.eegClock.add(((first + samples.length - 1) * 1000) / EEG_RATE, arrival)
        this.assemblers[i].push(first, samples)
        this.rawEeg.push(i, first, counts)
    }

    _acc(dataView) {
        const arrival = localNow()
        const { seq, counts, samples } = decodeAccelerometer(dataView)
        const packet = this.accSeq.next(seq)
        if (packet == null) return
        const first = packet * ACC_PER_PACKET
        this.accClock.add(((first + ACC_PER_PACKET - 1) * 1000) / ACC_RATE, arrival)
        for (let axis = 0; axis < 3; axis++) {
            this.rawAcc.push(axis, first, counts.map((xyz) => xyz[axis]))
        }
        samples.forEach((xyz, k) => {
            const t = this.accClock.toLocal(((first + k) * 1000) / ACC_RATE)
            const tap = t == null ? null : this.taps.push(t, xyz)
            if (tap != null && this._armed && tap <= this._armed.until) this._armed.onTap({ t: tap })
        })
    }

    // Listen for taps for the next ``ms``; onTap({t}) per tap (page clock).
    arm(ms, onTap) {
        this._armed = { until: localNow() + ms, onTap }
    }

    _detachHandlers() {
        for (const [ch, fn] of this._handlers) ch.removeEventListener("characteristicvaluechanged", fn)
        this._handlers = []
    }

    close() {
        this.closed = true
        this._armed = null
        if (this._reconnectTimer) clearTimeout(this._reconnectTimer)
        this._detachHandlers()
        if (this.device) {
            this.device.removeEventListener("gattserverdisconnected", this._disconnectHandler)
            if (this.device.gatt && this.device.gatt.connected) {
                if (this.control) this._send("h").catch(() => {})
                this.device.gatt.disconnect()
            }
        }
        this.device = null
        this.control = null
    }
}
