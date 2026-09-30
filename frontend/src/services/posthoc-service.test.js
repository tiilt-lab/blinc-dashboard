import { describe, it, expect, vi, afterEach } from "vitest"
import { PosthocService } from "./posthoc-service"
import { ApiService } from "./api-service"

// The trigger component must mint a per-pod ticket from THIS route before it
// opens a post-hoc socket; pin the URL shape and the refusal behaviour.
describe("PosthocService.getTicket", () => {
    afterEach(() => vi.restoreAllMocks())

    it("POSTs to the per-pod ticket route and resolves the body", async () => {
        const spy = vi.spyOn(ApiService.prototype, "httpRequestCall").mockResolvedValue({
            status: 200,
            json: () => Promise.resolve({ ticket: "abc", ttl: 900 }),
        })
        await expect(new PosthocService().getTicket(3, 42)).resolves.toEqual({
            ticket: "abc",
            ttl: 900,
        })
        expect(spy).toHaveBeenCalledWith("api/v1/sessions/3/devices/42/posthoc_ticket", "POST", {})
    })

    it("rejects when the API refuses (no write access or rate limited)", async () => {
        vi.spyOn(ApiService.prototype, "httpRequestCall").mockResolvedValue({
            status: 404,
            json: () => Promise.resolve({}),
        })
        await expect(new PosthocService().getTicket(3, 42)).rejects.toThrow(/404/)
    })
})
