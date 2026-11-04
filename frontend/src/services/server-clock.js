// The server's clock as seen from this page. Every wearable sample is put on
// the session clock server-side (src/server/physio_time.py), so the page
// sends its samples already converted to server time.
//
// NTP-style: time a round trip to /api/v1/time. The server read its clock
// somewhere inside that trip, so the offset is server - midpoint, wrong by
// at most half the trip; the probe with the shortest trip in recent rounds
// is kept (typically 10-50 ms of error, vs a whole one-way trip and any
// batching delay for the old "server now - client now at POST" skew).
//
// The local clock is performance.timeOrigin + performance.now(): epoch ms
// like Date.now(), but it never jumps when the OS sets the wall clock
// mid-session. It can stall across a laptop sleep, which a resync catches
// as a jump in offset (older probes are then dropped).

const PROBES = 5
const RESYNC_MS = 60000
// A laptop crystal runs off by tens of ppm, a few ms a minute; probes older
// than this are no longer trusted however short their trip.
const KEEP_MS = 3 * 60000
// A round's offset this far from the kept one (beyond both trips' slack)
// means the local clock itself moved: start over from this round.
const JUMP_MS = 50

export const localNow = () =>
    typeof performance !== "undefined" && performance.timeOrigin
        ? performance.timeOrigin + performance.now()
        : Date.now()

export class ServerClock {
    constructor({ fetchServerMs, now = localNow, probes = PROBES, resyncMs = RESYNC_MS, keepMs = KEEP_MS }) {
        this.fetchServerMs = fetchServerMs
        this.now = now
        this.probes = probes
        this.resyncMs = resyncMs
        this.keepMs = keepMs
        this.estimates = []
        this.best = null
        this._timer = null
    }

    async probe() {
        const t0 = this.now()
        const server = await this.fetchServerMs()
        const t1 = this.now()
        if (typeof server !== "number" || !Number.isFinite(server)) throw new Error("bad server time")
        return { offset: server - (t0 + t1) / 2, rtt: t1 - t0, at: t1 }
    }

    // One round of probes; resolves to the kept estimate (null if every
    // probe failed and nothing was kept before).
    async sync() {
        const round = []
        for (let i = 0; i < this.probes; i++) {
            try {
                round.push(await this.probe())
            } catch (ex) {
                // offline for a moment: keep what we had
            }
        }
        if (round.length) {
            const fastest = round.reduce((a, b) => (b.rtt < a.rtt ? b : a))
            if (this.best && Math.abs(fastest.offset - this.best.offset) > JUMP_MS + (fastest.rtt + this.best.rtt) / 2) {
                this.estimates = []
            }
            this.estimates.push(...round)
        }
        const horizon = this.now() - this.keepMs
        this.estimates = this.estimates.filter((e) => e.at >= horizon)
        if (this.estimates.length) {
            this.best = this.estimates.reduce((a, b) => (b.rtt < a.rtt ? b : a))
        }
        return this.best
    }

    start() {
        if (this._timer) return
        this.sync()
        this._timer = setInterval(() => this.sync(), this.resyncMs)
    }

    stop() {
        if (this._timer) clearInterval(this._timer)
        this._timer = null
    }

    get synced() {
        return !!this.best
    }

    // A local time (localNow's clock) on the server clock; null until synced.
    toServer(localMs) {
        if (!this.best || localMs == null) return null
        return localMs + this.best.offset
    }

    info() {
        return this.best ? { offset: Math.round(this.best.offset), rtt: Math.round(this.best.rtt) } : null
    }
}

// A sample with server_<field> beside each local time field, for the
// ingest endpoints: physio_time.py takes server_<field> when present and
// falls back to the local field plus the batch's client_now skew.
export const withServerTimes = (clock, sample, fields = ["t"]) => {
    const out = { ...sample }
    for (const f of fields) {
        const v = clock ? clock.toServer(sample[f]) : null
        if (v != null) out["server_" + f] = v
    }
    return out
}
