// Event markers from the join page: joining, starting the recording and each
// step of the baseline (src/server/tables/event_marker.py has the labels).
// A marker is timed the moment it happens and queued; the queue posts it to
// the server and keeps it (retrying) until the server has it, so a marker
// pressed while the network blips, or before the pod's key exists, is not
// lost. The server puts it on the session clock like the wearable samples
// (src/server/physio_time.py): server_t when the page knows the server's
// clock, else t corrected by the batch's client_now.

const RETRY_MS = 3000
const MAX_BATCH = 50

// Unique per press on this page; the server stores a re-sent id once.
export const newMarkerId = () =>
    Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10)

// "polar:Polar H10 A1B2 (Ana); muse:Muse-12AB (Ben)" for the sensors column.
export const describeSensors = (sensors) =>
    (sensors || [])
        .filter((s) => s && s.name)
        .map((s) => `${s.kind}:${s.name}${s.alias ? ` (${s.alias})` : ""}`)
        .join("; ")

export class MarkerQueue {
    // send(batch) resolves true once the server stored the batch, false (or
    // throws) to keep it for a retry. now() is the page's clock; toServer(t)
    // turns it into server time, or returns null while unknown.
    constructor({ send, now = Date.now, toServer = null, retryMs = RETRY_MS, setTimer = setTimeout, clearTimer = clearTimeout }) {
        this.send = send
        this.now = now
        this.toServer = toServer
        this.retryMs = retryMs
        this.setTimer = setTimer
        this.clearTimer = clearTimer
        this.pending = []
        this.sending = false
        this.timer = null
    }

    mark(label, { sensors = "", detail = "" } = {}) {
        const marker = { marker_id: newMarkerId(), label, t: this.now() }
        if (sensors) marker.sensors = sensors
        if (detail) marker.detail = detail
        this.pending.push(marker)
        this.flush()
        return marker
    }

    async flush() {
        if (this.sending || !this.pending.length) return
        this.clearTimer(this.timer)
        this.timer = null
        this.sending = true
        // A clock that synced after the press still converts the press time.
        const batch = this.pending.slice(0, MAX_BATCH).map((m) => {
            const server = m.server_t == null && this.toServer ? this.toServer(m.t) : null
            return server == null ? m : { ...m, server_t: server }
        })
        let ok = false
        try {
            ok = (await this.send(batch)) === true
        } catch {
            ok = false
        }
        this.sending = false
        if (ok) {
            const sent = new Set(batch.map((m) => m.marker_id))
            this.pending = this.pending.filter((m) => !sent.has(m.marker_id))
            if (this.pending.length) this.flush()
        } else if (this.timer === null) {
            this.timer = this.setTimer(() => {
                this.timer = null
                this.flush()
            }, this.retryMs)
        }
    }

    stop() {
        this.clearTimer(this.timer)
        this.timer = null
    }
}
