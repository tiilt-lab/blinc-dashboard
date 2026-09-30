import { ApiService } from "./api-service"

// The post-hoc websockets act on a message that names a pod only when it
// carries a short-lived ticket the API mints after its session write check
// (routes/posthoc_ticket.py). Resolves to { ticket, ttl } (ttl in seconds);
// rejects when the API refuses (no write access, rate limited).
export class PosthocService {
    api = new ApiService()

    getTicket(sessionId, sessionDeviceId) {
        return this.api
            .httpRequestCall(
                `api/v1/sessions/${sessionId}/devices/${sessionDeviceId}/posthoc_ticket`,
                "POST",
                {},
            )
            .then((r) => {
                if (r.status !== 200) throw new Error(`posthoc ticket refused (${r.status})`)
                return r.json()
            })
    }
}
