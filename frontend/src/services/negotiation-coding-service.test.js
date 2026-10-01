import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"
import { NegotiationCodingService } from "./negotiation-coding-service"

// Pins the URL / verb / body of every negotiation-coding route against the
// backend contract (routes under /sessions/<sid>/devices/<did>/negotiation_coding).
describe("NegotiationCodingService", () => {
    let calls
    let reply
    beforeEach(() => {
        calls = []
        reply = { status: 200, json: () => Promise.resolve({ ok: true }) }
        // ApiService.getEndpoint reads window.location; stub it (node env).
        vi.stubGlobal("window", {
            location: { protocol: "https:", host: "example.test" },
        })
        global.fetch = vi.fn((url, options) => {
            calls.push({ url, options })
            return Promise.resolve(reply)
        })
    })
    afterEach(() => {
        vi.unstubAllGlobals()
        vi.restoreAllMocks()
    })

    it("GETs the pod's coding and resolves {status, body}", async () => {
        reply = { status: 200, json: () => Promise.resolve({ run: { status: "done" } }) }
        const out = await new NegotiationCodingService().get(3, 42)
        expect(calls[0].url).toBe(
            "https://example.test/api/v1/sessions/3/devices/42/negotiation_coding",
        )
        expect(calls[0].options.method).toBe("GET")
        expect(calls[0].options.credentials).toBe("include")
        expect(out).toEqual({ status: 200, body: { run: { status: "done" } } })
    })

    it("resolves a 404 (never run) with a null body instead of rejecting", async () => {
        reply = { status: 404, json: () => Promise.reject(new Error("no body")) }
        await expect(new NegotiationCodingService().get(3, 42)).resolves.toEqual({
            status: 404,
            body: null,
        })
    })

    it("POSTs a run with the teams when given, an empty body otherwise", async () => {
        reply = { status: 202, json: () => Promise.resolve({ run_id: 7, status: "queued" }) }
        const svc = new NegotiationCodingService()
        const out = await svc.run(3, 42, { Alice: "Pat", Bob: "Sandy" })
        expect(calls[0].url).toBe(
            "https://example.test/api/v1/sessions/3/devices/42/negotiation_coding",
        )
        expect(calls[0].options.method).toBe("POST")
        expect(JSON.parse(calls[0].options.body)).toEqual({
            teams: { Alice: "Pat", Bob: "Sandy" },
        })
        expect(out).toEqual({ status: 202, body: { run_id: 7, status: "queued" } })

        await svc.run(3, 42, {})
        expect(JSON.parse(calls[1].options.body)).toEqual({})
        await svc.run(3, 42)
        expect(JSON.parse(calls[2].options.body)).toEqual({})
    })

    it("PUTs team assignments to the /teams route", async () => {
        await new NegotiationCodingService().setTeams(3, 42, { Alice: "Pat" })
        expect(calls[0].url).toBe(
            "https://example.test/api/v1/sessions/3/devices/42/negotiation_coding/teams",
        )
        expect(calls[0].options.method).toBe("PUT")
        expect(JSON.parse(calls[0].options.body)).toEqual({ teams: { Alice: "Pat" } })
    })

    it("builds the CSV export URL and can fetch it as text", async () => {
        const svc = new NegotiationCodingService()
        expect(svc.exportCsvUrl(3, 42)).toBe(
            "https://example.test/api/v1/sessions/3/devices/42/negotiation_coding/export.csv",
        )
        reply = { status: 200, text: () => Promise.resolve("a,b\n1,2\n") }
        await expect(svc.exportCsv(3, 42)).resolves.toEqual({ status: 200, text: "a,b\n1,2\n" })
        expect(calls[0].url).toBe(svc.exportCsvUrl(3, 42))
    })
})
