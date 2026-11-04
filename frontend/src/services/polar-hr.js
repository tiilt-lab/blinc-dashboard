// Web Bluetooth client for Polar H10 (or any strap exposing the standard
// GATT Heart Rate service). One connection per speaker; each notification
// carries BPM plus zero or more beat-to-beat RR intervals (1/1024 s units),
// which is the raw material for HRV (RMSSD etc.).
//
// Web Bluetooth exists in Chrome/Edge (desktop + Android) over HTTPS only —
// callers should gate UI on isBluetoothSupported().
//
// Timing: a notification is stamped when it arrives (t), but the strap only
// sends about once a second, so a beat can be up to a second old by then.
// The RR intervals themselves are the strap's clock: they add up to the
// time between beats, and sample-clock.js fits that running sum against
// the arrivals to say when each notification's last beat ended (beat).

import { DeviceClock } from "./sample-clock"
import { localNow } from "./server-clock"

const HEART_RATE_SERVICE = "heart_rate"
const HEART_RATE_MEASUREMENT = "heart_rate_measurement"
const BATTERY_SERVICE = "battery_service"
const BATTERY_LEVEL = "battery_level"

export const isBluetoothSupported = () =>
    typeof navigator !== "undefined" && !!navigator.bluetooth

// Parse a Heart Rate Measurement characteristic value (GATT 0x2A37).
// Returns { hr: bpm, rr: [ms, ...], rr1024: [raw 1/1024 s, ...] } — rr is
// rounded for storage, rr1024 kept exact so summing it does not drift.
export const parseHeartRateMeasurement = (dataView) => {
    const flags = dataView.getUint8(0)
    let offset = 1
    let hr
    if (flags & 0x01) {
        hr = dataView.getUint16(offset, true)
        offset += 2
    } else {
        hr = dataView.getUint8(offset)
        offset += 1
    }
    if (flags & 0x08) offset += 2 // energy expended field, skip
    const rr = []
    const rr1024 = []
    if (flags & 0x10) {
        for (; offset + 1 < dataView.byteLength; offset += 2) {
            // RR is in 1/1024ths of a second per the spec.
            const raw = dataView.getUint16(offset, true)
            rr1024.push(raw)
            rr.push(Math.round((raw * 1000) / 1024))
        }
    }
    return { hr, rr, rr1024 }
}

// A strap sends about once a second while connected; a longer silence may
// have lost beats, which would put the running RR sum behind for good.
const BEAT_GAP_MS = 3000

// The strap's beat timeline: the running sum of its RR intervals, fitted
// to notification arrivals (page clock). add() returns when the
// notification's last beat ended, or null when it carried no beats.
export class BeatClock {
    constructor() {
        // A notification may hold beats from up to ~1 s before it was sent.
        this.clock = new DeviceClock({ resetMs: 1500, maxDelayMs: 1200 })
        this.reset()
    }

    reset() {
        this.clock.reset()
        this.sumMs = 0
        this.lastArrival = null
    }

    add(rr1024, arrival) {
        if (this.lastArrival != null && arrival - this.lastArrival > BEAT_GAP_MS) this.reset()
        this.lastArrival = arrival
        if (!rr1024 || !rr1024.length) return null
        for (const raw of rr1024) this.sumMs += (raw * 1000) / 1024
        this.clock.add(this.sumMs, arrival)
        return this.clock.toLocal(this.sumMs)
    }
}

// One managed strap connection. onSample({hr, rr, t, beat}) fires per
// notification (H10: ~1/s), t its arrival and beat its last beat (both on
// the page clock, localNow; beat null without beats); onStatus("connecting"|"connected"|"disconnected", info) tracks
// the connection lifecycle, including auto-reconnect attempts.
export class PolarConnection {
    constructor({ onSample, onStatus }) {
        this.onSample = onSample || (() => {})
        this.onStatus = onStatus || (() => {})
        this.device = null
        this.characteristic = null
        this.battery = null
        this.closed = false
        this._reconnectTimer = null
        this.beats = new BeatClock()
        this._notifyHandler = (event) => {
            const t = localNow()
            const { hr, rr, rr1024 } = parseHeartRateMeasurement(event.target.value)
            this.onSample({ hr, rr, t, beat: this.beats.add(rr1024, t) })
        }
        this._disconnectHandler = () => {
            this.characteristic = null
            if (this.closed) return
            this.onStatus("disconnected")
            // Straps drop out when the wearer walks off or the band dries
            // out — retry quietly until close() or the strap comes back.
            this._reconnectTimer = setTimeout(() => this._attach().catch(() => {
                if (!this.closed) this._disconnectHandler()
            }), 3000)
        }
    }

    // Must be called from a user gesture (button click): opens the browser's
    // device chooser listing nearby heart-rate straps. Filters are OR'd:
    // Polar straps don't reliably put the HR service UUID in their
    // advertisement (varies with firmware/bonding state), so filtering on
    // the service alone can miss a worn, advertising strap — match on the
    // "Polar" name too, with the HR service in optionalServices so a
    // name-matched device still exposes it after connecting.
    async choose() {
        this.device = await navigator.bluetooth.requestDevice({
            filters: [
                { services: [HEART_RATE_SERVICE] },
                { namePrefix: "Polar" },
            ],
            optionalServices: [HEART_RATE_SERVICE, BATTERY_SERVICE],
        })
        this.device.addEventListener("gattserverdisconnected", this._disconnectHandler)
        await this._attach()
        return {
            // e.g. "Polar H10 8C0B2A2B" — the ID printed on the strap module.
            name: this.device.name || "Heart-rate sensor",
            battery: this.battery,
        }
    }

    async _attach() {
        if (this.closed || !this.device) return
        this.onStatus("connecting")
        this.beats.reset()
        const server = await this.device.gatt.connect()
        const service = await server.getPrimaryService(HEART_RATE_SERVICE)
        this.characteristic = await service.getCharacteristic(HEART_RATE_MEASUREMENT)
        this.characteristic.addEventListener(
            "characteristicvaluechanged",
            this._notifyHandler,
        )
        await this.characteristic.startNotifications()
        try {
            const batt = await server.getPrimaryService(BATTERY_SERVICE)
            const level = await batt.getCharacteristic(BATTERY_LEVEL)
            this.battery = (await level.readValue()).getUint8(0)
        } catch (ex) {
            this.battery = null // battery service is optional
        }
        this.onStatus("connected", { battery: this.battery })
    }

    close() {
        this.closed = true
        if (this._reconnectTimer) clearTimeout(this._reconnectTimer)
        if (this.device) {
            this.device.removeEventListener(
                "gattserverdisconnected",
                this._disconnectHandler,
            )
            if (this.device.gatt && this.device.gatt.connected) {
                this.device.gatt.disconnect()
            }
        }
        this.device = null
        this.characteristic = null
    }
}
