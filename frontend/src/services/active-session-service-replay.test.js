import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"

// The service touches window.location at construction (ApiService); stub it.
beforeEach(() => {
    vi.stubGlobal("window", { location: { protocol: "https:", host: "x.test" } })
    vi.spyOn(console, "warn").mockImplementation(() => {})
})
afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
})

const { ActiveSessionService } = await import("./active-session-service")

const ok = (body) => ({ status: 200, json: () => Promise.resolve(body) })
const SESSION = { id: 414, name: "s", creation_date: "2026-08-09 00:00:00 UTC", end_date: null }

// A socket stand-in that records handlers so tests can fire server events.
function fakeSocket() {
    const handlers = {}
    return {
        handlers,
        disconnected: false,
        on(ev, fn) { handlers[ev] = fn },
        emit(ev, fn) { handlers[ev] && handlers[ev](fn) },
        removeAllListeners() { Object.keys(handlers).forEach((k) => delete handlers[k]) },
        disconnect() { this.disconnected = true },
    }
}

function withSocket() {
    const svc = new ActiveSessionService()
    const created = []
    svc.socketService = {
        createSocket: (endpoint, room, joinFields) => {
            const s = fakeSocket()
            created.push({ endpoint, room, joinFields, socket: s })
            return s
        },
    }
    svc.sessionService = {
        getSession: () => Promise.resolve(ok(SESSION)),
        getSessionDevices: () => Promise.resolve(ok([{ id: 1 }])),
    }
    return { svc, created }
}

const transcriptEvent = (id) =>
    JSON.stringify({ transcript: { id, session_device_id: 1, start_time: id, keywords: [] }, speaker_metrics: [] })
const videoEvent = (id) => JSON.stringify({ speaker_video_metrics: { id, session_device_id: 1, time_stamp: id } })

describe("join_room carries the last-seen ids", () => {
    it("sends no ids on the first join (full history) and the max ids after data arrived", async () => {
        const { svc, created } = withSocket()
        await svc.initialize(414, () => {})
        expect(created).toHaveLength(1)
        expect(created[0].room).toBe(414)
        expect(created[0].joinFields()).toEqual({})
        const s = created[0].socket
        s.emit("transcript_metrics_digest", JSON.stringify([JSON.parse(transcriptEvent(7)), JSON.parse(transcriptEvent(12))]))
        s.emit("video_metrics_digest", JSON.stringify([JSON.parse(videoEvent(3)), JSON.parse(videoEvent(9))]))
        expect(created[0].joinFields()).toEqual({ last_transcript_id: 12, last_video_metric_id: 9 })
    })
})

describe("room_joined keeps local state (partial replay expected)", () => {
    it("does not clear transcripts or video metrics on a rejoin, and de-duplicates the replay", async () => {
        const { svc, created } = withSocket()
        await svc.initialize(414, () => {})
        const s = created[0].socket
        s.emit("transcript_metrics_digest", JSON.stringify([JSON.parse(transcriptEvent(7))]))
        s.emit("video_metrics_digest", JSON.stringify([JSON.parse(videoEvent(3))]))
        s.emit("room_joined", "{}")
        expect(svc.transcriptSource.getValue().map((t) => t.id)).toEqual([7])
        expect(svc.videoMetricSource.getValue().map((v) => v.id)).toEqual([3])
        // A server that still replays everything must not duplicate rows.
        s.emit("transcript_metrics_digest", JSON.stringify([JSON.parse(transcriptEvent(7)), JSON.parse(transcriptEvent(8))]))
        s.emit("video_metrics_digest", JSON.stringify([JSON.parse(videoEvent(3)), JSON.parse(videoEvent(4))]))
        expect(svc.transcriptSource.getValue().map((t) => t.id)).toEqual([7, 8])
        expect(svc.videoMetricSource.getValue().map((v) => v.id)).toEqual([3, 4])
    })
})

describe("initialize() cancelled by close() opens no socket", () => {
    it("neither reports nor creates a socket when close() ran during the REST load", async () => {
        const { svc, created } = withSocket()
        let release
        svc.sessionService.getSession = () => new Promise((r) => { release = () => r(ok(SESSION)) })
        const results = []
        const p = svc.initialize(414, (r) => results.push(r))
        svc.close() // unmount while the session GET is in flight
        release()
        await p
        expect(created).toHaveLength(0)
        expect(results).toEqual([])
        expect(svc.initialized).toBe(false)
    })

    it("close() tears the socket down and forgets it", async () => {
        const { svc, created } = withSocket()
        await svc.initialize(414, () => {})
        const s = created[0].socket
        svc.close()
        expect(s.disconnected).toBe(true)
        expect(svc.socket).toBe(null)
    })
})
