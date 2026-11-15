// Web Bluetooth client for an Empatica E4 wristband, one connection per
// wearer, with no Empatica app or server. Protocol as reverse-engineered at
// github.com/ismaelwarnants/e4-python-server and checked against a real band
// (the standalone recorder in github.com/bookyungshin/multimodal-data-collection):
//
//   service 0x3ea0  sensor streams, 20-byte notifications, each ending in a
//                   packet counter that runs 1..max and wraps back to 1:
//     0x3ea1 BVP    22 7-bit signed values packed MSB-first = 11 (green, red)
//                   sample-to-sample differences, then a 6-bit counter. 64 Hz.
//     0x3ea8 EDA    6 24-bit big-endian counts (skin conductance = 1e6 /
//                   count, in µS), then a uint16 big-endian counter. 4 Hz.
//     0x3ea6 TEMP   8 uint16 little-endian counts (0.02 °C per count, -275.54),
//                   then a uint32 big-endian counter. 4 Hz.
//     0x3ea3 ACC    6 int8 x/y/z triples (1/64 g), an unknown byte, then a
//                   uint8 counter. 32 Hz.
//   service 0x3e70  0x3e71 takes the start command: 0x01 + uint32 LE Unix time.
//   service 0x3eb0  0x3eb2 notifies a press of the band's button: 0x01 and the
//                   press time as uint32 LE in 1/64 s since streaming began,
//                   i.e. the BVP sample number at the press.
//
// Nothing is computed from the signals here: every stream goes up as raw
// device counts, one block per stream per second (RawBlockAssembler, stored
// by raw_signals.py), timed from the packet counters (sample-clock.js) rather
// than Bluetooth arrival. The card shows the latest skin conductance and
// temperature so a band that sits badly is noticed, and flashes when the
// band's button is pressed, which is how a band is matched to its wearer:
// every E4 advertises the same name, "Empatica E4".
//
// Web Bluetooth exists in Chrome/Edge (desktop + Android) over HTTPS only.

import { DeviceClock } from "./sample-clock"
import { localNow } from "./server-clock"
import { RawBlockAssembler, encodeBlock } from "./raw-blocks"

const uuid16 = (n) => `0000${n}-0000-1000-8000-00805f9b34fb`
export const E4_DATA_SERVICE = uuid16("3ea0")
export const E4_COMMAND_SERVICE = uuid16("3e70")
export const E4_EVENT_SERVICE = uuid16("3eb0")
const COMMAND = uuid16("3e71")
const BUTTON = uuid16("3eb2")

// Per stream: notifying characteristic, samples per packet, channels, the
// largest counter value before it wraps to 1, and the raw block layout.
export const E4_STREAMS = {
    bvp: { char: uuid16("3ea1"), rate: 64, perPacket: 11, channels: 2, counterMax: 63 },
    eda: { char: uuid16("3ea8"), rate: 4, perPacket: 6, channels: 1, counterMax: 65535, int32: true },
    temp: { char: uuid16("3ea6"), rate: 4, perPacket: 8, channels: 1, counterMax: 4294967295 },
    acc: { char: uuid16("3ea3"), rate: 32, perPacket: 6, channels: 3, counterMax: 255 },
}
// The button's timestamps count BVP samples.
export const BUTTON_TICK_HZ = 64

const signExtend = (value, bits) => {
    const sign = 1 << (bits - 1)
    const masked = value & ((1 << bits) - 1)
    return masked & sign ? masked - (1 << bits) : masked
}

const bytesOf = (dataView) => new Uint8Array(dataView.buffer, dataView.byteOffset, dataView.byteLength)

// Each decoder: a notification -> {seq, counts: one [channel values] per
// sample}, or null when it is too short to be one.
export const decodeBvp = (dataView) => {
    if (dataView.byteLength < 20) return null
    const bytes = bytesOf(dataView)
    const bits = (start, len) => {
        let v = 0
        for (let b = start; b < start + len; b++) v = (v << 1) | ((bytes[b >> 3] >> (7 - (b & 7))) & 1)
        return v
    }
    const counts = []
    for (let k = 0; k < 11; k++) {
        counts.push([signExtend(bits(14 * k, 7), 7), signExtend(bits(14 * k + 7, 7), 7)])
    }
    return { seq: bytes[19] & 0x3f, counts }
}

export const decodeEda = (dataView) => {
    if (dataView.byteLength < 20) return null
    const counts = []
    for (let i = 0; i < 18; i += 3) {
        counts.push([(dataView.getUint8(i) << 16) | (dataView.getUint8(i + 1) << 8) | dataView.getUint8(i + 2)])
    }
    return { seq: dataView.getUint16(18), counts }
}

export const decodeTemp = (dataView) => {
    if (dataView.byteLength < 20) return null
    const counts = []
    for (let i = 0; i < 16; i += 2) counts.push([dataView.getUint16(i, true)])
    return { seq: dataView.getUint32(16), counts }
}

export const decodeAcc = (dataView) => {
    if (dataView.byteLength < 20) return null
    const counts = []
    for (let i = 0; i < 18; i += 3) counts.push([dataView.getInt8(i), dataView.getInt8(i + 1), dataView.getInt8(i + 2)])
    return { seq: dataView.getUint8(19), counts }
}

const DECODERS = { bvp: decodeBvp, eda: decodeEda, temp: decodeTemp, acc: decodeAcc }

// A button press: its time in 1/64 s since streaming began, or null.
export const decodeButton = (dataView) => {
    if (dataView.byteLength < 5 || dataView.getUint8(0) !== 0x01) return null
    return dataView.getUint32(1, true)
}

export const startCommand = (unixSeconds) => {
    const cmd = new Uint8Array(5)
    const view = new DataView(cmd.buffer)
    view.setUint8(0, 0x01)
    view.setUint32(1, unixSeconds >>> 0, true)
    return cmd
}

export const edaMicrosiemens = (count) => (count > 0 ? 1e6 / count : null)
export const tempCelsius = (count) => count * 0.02 - 275.54

// Every E4 advertises as "Empatica E4"; the browser's device id (random,
// fixed per site) tells two apart in the data.
export const e4Label = (device) => {
    const id = ((device && device.id) || "").replace(/[^A-Za-z0-9]/g, "").slice(0, 4)
    return ((device && device.name) || "Empatica E4") + (id ? " " + id : "")
}

// The samples of one stream, packet by packet: index of each packet's first
// sample since streaming began (counters start at 1), packets lost, and the
// device clock that turns a sample index into page time. A counter as short
// as BVP's wraps every ~11 s, so a gap is measured in both: the counter says
// how far into its cycle the band moved, arrival time how many whole cycles
// went by in between.
export class StreamTimeline {
    constructor({ rate, perPacket, counterMax }) {
        this.rate = rate
        this.perPacket = perPacket
        this.modulo = counterMax
        this.clock = new DeviceClock()
        this.lastSeq = null
        this.lastArrival = null
        this.packet = null
        this.lost = 0
    }

    // The packet's first sample index, or null for a repeated counter.
    push(seq, arrival) {
        if (this.lastSeq == null) {
            this.packet = seq
        } else {
            let step = (((seq - this.lastSeq) % this.modulo) + this.modulo) % this.modulo
            if (step === 0) return null
            const expected = ((arrival - this.lastArrival) * this.rate) / this.perPacket / 1000
            step += Math.max(0, Math.round((expected - step) / this.modulo)) * this.modulo
            this.lost += step - 1
            this.packet += step
        }
        this.lastSeq = seq
        this.lastArrival = arrival
        const first = (this.packet - 1) * this.perPacket
        // The packet arrived once its last sample was taken.
        this.clock.add(((first + this.perPacket - 1) * 1000) / this.rate, arrival)
        return first
    }

    toLocal(index) {
        return this.clock.toLocal((index * 1000) / this.rate)
    }
}

// One managed wristband. onRaw({stream, index, t, data}) once a second per
// stream, data base64 counts (encodeBlock), t its first sample on the page
// clock (localNow); onButton({t, tick}) per button press, t on the page
// clock; onStatus like MuseConnection. latest holds the newest skin
// conductance (µS) and temperature (°C) for the card.
export class E4Connection {
    constructor({ onRaw, onButton, onStatus }) {
        this.onRaw = onRaw || (() => {})
        this.onButton = onButton || (() => {})
        this.onStatus = onStatus || (() => {})
        this.device = null
        this.closed = false
        this.latest = { eda: null, temp: null }
        this._reconnectTimer = null
        this._handlers = []
        this._resetTiming()
        this._disconnectHandler = () => {
            if (this.closed) return
            this.onStatus("disconnected")
            this._reconnectTimer = setTimeout(() => this._attach().catch(() => {
                if (!this.closed) this._disconnectHandler()
            }), 3000)
        }
    }

    get label() {
        return e4Label(this.device)
    }

    // Packets lost so far on this connection, all streams.
    get lost() {
        return Object.values(this.timelines).reduce((n, tl) => n + tl.lost, 0)
    }

    // A reconnect sends a new start command, which restarts the counters:
    // every timeline starts over.
    _resetTiming() {
        this.timelines = {}
        this.assemblers = {}
        for (const [stream, sp] of Object.entries(E4_STREAMS)) {
            const timeline = new StreamTimeline(sp)
            this.timelines[stream] = timeline
            this.assemblers[stream] = new RawBlockAssembler(
                (index, data) => {
                    const t = timeline.toLocal(index)
                    if (t != null) this.onRaw({ stream, index, t, data: encodeBlock(data) })
                },
                { channels: sp.channels, size: sp.rate, int32: !!sp.int32 },
            )
        }
    }

    // Must be called from a user gesture: opens the browser's chooser, which
    // lists every band as "Empatica E4".
    async choose() {
        this.device = await navigator.bluetooth.requestDevice({
            filters: [{ namePrefix: "Empatica" }],
            optionalServices: [E4_DATA_SERVICE, E4_COMMAND_SERVICE, E4_EVENT_SERVICE],
        })
        this.device.addEventListener("gattserverdisconnected", this._disconnectHandler)
        await this._attach()
        return { name: this.label }
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
        const data = await server.getPrimaryService(E4_DATA_SERVICE)
        for (const [stream, sp] of Object.entries(E4_STREAMS)) {
            await this._listen(data, sp.char, (dv) => this._packet(stream, dv))
        }
        try {
            const events = await server.getPrimaryService(E4_EVENT_SERVICE)
            await this._listen(events, BUTTON, (dv) => this._button(dv))
        } catch (ex) {
            console.warn("E4 button presses unavailable", ex)
        }
        const command = await (await server.getPrimaryService(E4_COMMAND_SERVICE)).getCharacteristic(COMMAND)
        const start = startCommand(Math.floor(Date.now() / 1000))
        await (command.writeValueWithResponse ? command.writeValueWithResponse(start) : command.writeValue(start))
        this.onStatus("connected")
    }

    _packet(stream, dataView) {
        const arrival = localNow()
        const decoded = DECODERS[stream](dataView)
        if (!decoded) return
        const first = this.timelines[stream].push(decoded.seq, arrival)
        if (first == null) return
        const { counts } = decoded
        const assembler = this.assemblers[stream]
        for (let ch = 0; ch < E4_STREAMS[stream].channels; ch++) {
            assembler.push(ch, first, counts.map((sample) => sample[ch]))
        }
        if (stream === "eda") this.latest.eda = edaMicrosiemens(counts[counts.length - 1][0])
        if (stream === "temp") this.latest.temp = tempCelsius(counts[counts.length - 1][0])
    }

    _button(dataView) {
        const arrival = localNow()
        const tick = decodeButton(dataView)
        if (tick == null) return
        // The band's own time of the press, on the BVP timeline; arrival if
        // no BVP has come in yet to place it.
        const t = this.timelines.bvp.toLocal((tick * E4_STREAMS.bvp.rate) / BUTTON_TICK_HZ)
        this.onButton({ t: t == null ? arrival : Math.min(t, arrival), tick })
    }

    _detachHandlers() {
        for (const [ch, fn] of this._handlers) ch.removeEventListener("characteristicvaluechanged", fn)
        this._handlers = []
    }

    close() {
        this.closed = true
        if (this._reconnectTimer) clearTimeout(this._reconnectTimer)
        this._detachHandlers()
        if (this.device) {
            this.device.removeEventListener("gattserverdisconnected", this._disconnectHandler)
            if (this.device.gatt && this.device.gatt.connected) this.device.gatt.disconnect()
        }
        this.device = null
    }
}
