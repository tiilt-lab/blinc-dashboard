import { describe, it, expect } from "vitest"
import {
    DATASETS,
    DEFAULT_ITEMS,
    attachmentName,
    cleanItems,
    errorMessage,
    formatBytes,
    loadItems,
    needsWindow,
    saveItems,
    videoPods,
} from "./session-export"

const memoryStorage = () => {
    const data = {}
    return {
        getItem: (k) => (k in data ? data[k] : null),
        setItem: (k, v) => {
            data[k] = String(v)
        },
    }
}

const response = (status, body) => ({
    status,
    json: async () => {
        if (body === undefined) throw new Error("no body")
        return body
    },
})

describe("datasets", () => {
    it("keeps known keys in the dialog's order", () => {
        expect(cleanItems(["heart_rate", "nope", "pods"])).toEqual(["pods", "heart_rate"])
        expect(cleanItems(null)).toEqual([])
    })

    it("asks for a window only when a metric file is chosen", () => {
        expect(needsWindow(["heart_rate", "transcripts"])).toBe(false)
        expect(needsWindow(["heart_rate", "video_metrics"])).toBe(true)
        expect(DATASETS.filter((d) => d.windowed).map((d) => d.key)).toEqual([
            "transcript_metrics",
            "video_metrics",
            "transcript_video_metrics",
        ])
    })

    it("remembers the last choice and falls back to the defaults", () => {
        const storage = memoryStorage()
        expect(loadItems(storage)).toEqual(DEFAULT_ITEMS)
        saveItems(["rr_intervals", "heart_rate"], storage)
        expect(loadItems(storage)).toEqual(["heart_rate", "rr_intervals"])
        storage.setItem("sessionExportItems", "{not json")
        expect(loadItems(storage)).toEqual(DEFAULT_ITEMS)
        const broken = {
            getItem: () => {
                throw new Error("denied")
            },
            setItem: () => {
                throw new Error("denied")
            },
        }
        expect(loadItems(broken)).toEqual(DEFAULT_ITEMS)
        expect(() => saveItems(["pods"], broken)).not.toThrow()
    })
})

describe("errorMessage", () => {
    it("explains sign-in and access failures", async () => {
        expect(await errorMessage(response(401))).toMatch(/sign in again/i)
        expect(await errorMessage(response(404))).toMatch(/access/)
    })

    it("passes the server's reason through, else the status", async () => {
        expect(await errorMessage(response(400, { message: "Unknown data: x." }))).toBe("Unknown data: x.")
        expect(await errorMessage(response(500))).toBe("Download failed (HTTP 500).")
    })
})

describe("attachmentName", () => {
    it("reads plain and UTF-8 file names", () => {
        expect(attachmentName("attachment; filename=session3_data.zip", "x")).toBe("session3_data.zip")
        expect(attachmentName('attachment; filename="a b.zip"', "x")).toBe("a b.zip")
        expect(attachmentName("attachment; filename*=UTF-8''s%C3%A9ance.zip", "x")).toBe("séance.zip")
        expect(attachmentName(null, "fallback.zip")).toBe("fallback.zip")
    })
})

describe("formatBytes", () => {
    it("rounds to a readable unit", () => {
        expect(formatBytes(512)).toBe("512 B")
        expect(formatBytes(1536)).toBe("1.5 KB")
        expect(formatBytes(1932735283)).toBe("1.8 GB")
        expect(formatBytes(250 * 1024 * 1024)).toBe("250 MB")
        expect(formatBytes(null)).toBe("")
    })
})

describe("videoPods", () => {
    it("lists pods with video in natural name order", () => {
        const enriched = {
            3: { id: 3, name: "Group 10", has_video: true },
            1: { id: 1, name: "Group 2", has_video: true },
            2: { id: 2, name: "Group 1", has_video: false },
        }
        expect(videoPods(enriched).map((d) => d.id)).toEqual([1, 3])
        expect(videoPods(null)).toEqual([])
    })
})
