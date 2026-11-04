// A Bluetooth sensor's own timeline on this page's clock.
//
// A sensor counts what it measures: a Muse numbers its packets (12 EEG
// samples each at 256 Hz), a heart-rate strap's RR intervals add up to the
// time between its beats. Its packets reach the page late by a delay that
// varies (radio retries, the OS, a strap that only sends once a second), so
// stamping each sample with its arrival time smears it by that delay.
// Instead each packet gives one point: device time d (from the counter) and
// arrival a. a - d = clock offset + delay, and the delay is never negative,
// so the smallest a - d seen is the offset with the least delay in it.
// DeviceClock keeps that minimum over a sliding window: a sensor crystal
// off by 50 ppm moves 1.5 ms in 30 s, far less than the delay it removes,
// and the window lets the estimate follow it. (A line fitted through the
// points would average the delay back in: a strap's delays spread over a
// whole second.) A point far off the estimate (a dropped connection, lost
// beats, a counter that restarted) starts it over.

const BUCKET_MS = 2000
const WINDOW_MS = 30000

export class DeviceClock {
    constructor({ bucketMs = BUCKET_MS, windowMs = WINDOW_MS, resetMs = 1500, maxDelayMs = 500 } = {}) {
        this.bucketMs = bucketMs
        this.windowMs = windowMs
        // How far below the estimate (impossible: earlier than measured) or
        // above it beyond the usual delay a point may land before the
        // device timeline is taken to have jumped.
        this.resetMs = resetMs
        this.maxDelayMs = maxDelayMs
        this.reset()
    }

    reset() {
        this.buckets = new Map()
        this.offset = null
    }

    add(deviceMs, arrivalMs) {
        const off = arrivalMs - deviceMs
        if (this.offset != null && (off - this.offset < -this.resetMs || off - this.offset > this.maxDelayMs + this.resetMs)) {
            this.reset()
        }
        const key = Math.floor(deviceMs / this.bucketMs)
        const b = this.buckets.get(key)
        if (b == null || off < b) this.buckets.set(key, off)
        const oldest = Math.floor((deviceMs - this.windowMs) / this.bucketMs)
        for (const k of this.buckets.keys()) if (k < oldest) this.buckets.delete(k)
        this.offset = Math.min(...this.buckets.values())
    }

    get ready() {
        return this.offset != null
    }

    // A device time on the page clock; null before the first point.
    toLocal(deviceMs) {
        return this.offset == null ? null : deviceMs + this.offset
    }
}

// A wrapping packet counter (Muse: uint16) as an ever-growing index, so
// sample k of packet p is sample p * perPacket + k however long the
// session runs. A step back (a duplicate or a reordered packet) returns
// null; the counter is otherwise taken at its word, so a jump forward
// counts the packets that never arrived.
export class SequenceCounter {
    constructor(modulo = 65536) {
        this.modulo = modulo
        this.last = null
        this.index = null
    }

    reset() {
        this.last = null
        this.index = null
    }

    next(seq) {
        if (this.last == null) {
            this.last = seq
            this.index = seq
            return this.index
        }
        const delta = (seq - this.last + this.modulo) % this.modulo
        if (delta === 0 || delta > this.modulo / 2) return null
        this.last = seq
        this.index += delta
        return this.index
    }
}
