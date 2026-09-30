import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"
import { SessionService } from "./session-service"
import { ApiService } from "./api-service"

// The three live polls take { afterId, signal }: afterId > 0 must land in
// the query string (rows newer than it), 0/absent must keep the plain URL,
// and a signal must reach fetch so an unmounted view can abort.
describe("SessionService live polls: after_id and abort", () => {
    let spy
    beforeEach(() => {
        vi.stubGlobal("window", { location: { protocol: "https:", host: "x.test" } })
        spy = vi
            .spyOn(ApiService.prototype, "httpRequestCallWithHeader")
            .mockResolvedValue({ status: 200, json: () => Promise.resolve([]) })
    })
    afterEach(() => {
        vi.unstubAllGlobals()
        vi.restoreAllMocks()
    })

    it("appends after_id only when positive", () => {
        const svc = new SessionService()
        svc.getSessionDeviceTranscriptsForClient(5, 0, { afterId: 42 })
        expect(spy.mock.calls[0][0]).toBe("api/v1/devices/5/transcripts/client?after_id=42")
        svc.getSessionDeviceTranscriptsForClient(5, 0, { afterId: 0 })
        expect(spy.mock.calls[1][0]).toBe("api/v1/devices/5/transcripts/client")
        svc.getSessionDeviceTranscriptsForClient(5)
        expect(spy.mock.calls[2][0]).toBe("api/v1/devices/5/transcripts/client")
    })

    it("keeps the pod key header on the keyed polls", () => {
        const svc = new SessionService()
        svc.getSessionDeviceTranscriptSpeakerMetricsForClient(5, 0, "KEY", { afterId: 7 })
        expect(spy.mock.calls[0][0]).toBe("api/v1/devices/5/transcriptspeakermetrics/client?after_id=7")
        expect(spy.mock.calls[0][3]).toEqual({ "X-Processing-Key": "KEY" })
        svc.getSessionDeviceVideoMetricsForClient(5, 0, "KEY", { afterId: 9 })
        expect(spy.mock.calls[1][0]).toBe("api/v1/devices/5/videometrics/client?after_id=9")
    })

    it("routes through fetch with the signal when one is given", async () => {
        const fetchSpy = vi.fn().mockResolvedValue({ status: 200, json: () => Promise.resolve([]) })
        vi.stubGlobal("fetch", fetchSpy)
        const svc = new SessionService()
        const controller = new AbortController()
        await svc.getSessionDeviceVideoMetricsForClient(5, 0, "KEY", { afterId: 3, signal: controller.signal })
        expect(spy).not.toHaveBeenCalled()
        expect(fetchSpy).toHaveBeenCalledTimes(1)
        const [url, opts] = fetchSpy.mock.calls[0]
        expect(url).toBe("https://x.test/api/v1/devices/5/videometrics/client?after_id=3")
        expect(opts.signal).toBe(controller.signal)
        expect(opts.credentials).toBe("include")
        expect(opts.cache).toBe("no-store")
        expect(opts.headers["X-Processing-Key"]).toBe("KEY")
    })

    it("the teacher-overview and list polls accept a signal too", async () => {
        const fetchSpy = vi.fn().mockResolvedValue({ status: 200, json: () => Promise.resolve([]) })
        vi.stubGlobal("fetch", fetchSpy)
        const svc = new SessionService()
        const { signal } = new AbortController()
        await svc.getSessions({ signal })
        await svc.getSessionDevices(3, { signal })
        await svc.getPosthocQueue(3, { signal })
        await svc.getSessionTriage(3, { signal })
        expect(fetchSpy.mock.calls.map((c) => c[0])).toEqual([
            "https://x.test/api/v1/sessions",
            "https://x.test/api/v1/sessions/3/devices",
            "https://x.test/api/v1/sessions/3/posthoc_queue",
            "https://x.test/api/v1/sessions/3/triage",
        ])
    })
})
