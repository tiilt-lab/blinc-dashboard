// The Download dialog's data side: the datasets the session ZIP can hold
// (keys match src/server/exports.py DATASETS), which of them take a window
// size, the remembered choice, and the messages for failed downloads.

export const DATASETS = [
    { key: "pods", label: "Pods summary" },
    { key: "participants", label: "Participants" },
    { key: "markers", label: "Event markers (baseline)" },
    { key: "transcripts", label: "Transcripts" },
    { key: "transcript_metrics", label: "Transcript metrics", windowed: true },
    { key: "video_metrics", label: "Video metrics", windowed: true },
    { key: "transcript_video_metrics", label: "Transcript + video metrics", windowed: true },
    { key: "keyword_usage", label: "Keyword usage" },
    { key: "joint_attention", label: "Joint attention" },
    { key: "heart_rate", label: "Heart rate" },
    { key: "rr_intervals", label: "RR intervals (HRV)" },
    { key: "video_segments", label: "Video segments" },
]

export const WINDOW_SIZES = [0, 10, 20, 30, 40, 50, 60]

export const DEFAULT_ITEMS = ["pods", "participants", "markers", "transcripts"]

const STORAGE_KEY = "sessionExportItems"

// Only keys the server knows, in the dialog's order.
export function cleanItems(items) {
    const chosen = new Set(items || [])
    return DATASETS.map((d) => d.key).filter((k) => chosen.has(k))
}

export function needsWindow(items) {
    const chosen = new Set(items || [])
    return DATASETS.some((d) => d.windowed && chosen.has(d.key))
}

// Reading window.localStorage itself throws where storage is blocked, so the
// default is resolved inside the try.
export function loadItems(storage) {
    try {
        const saved = cleanItems(JSON.parse((storage || window.localStorage).getItem(STORAGE_KEY)))
        return saved.length ? saved : DEFAULT_ITEMS
    } catch {
        return DEFAULT_ITEMS
    }
}

export function saveItems(items, storage) {
    try {
        (storage || window.localStorage).setItem(STORAGE_KEY, JSON.stringify(cleanItems(items)))
    } catch {
        // private mode / storage disabled: the choice just isn't remembered
    }
}

export async function errorMessage(response) {
    if (response.status === 401) return "Your sign-in has expired. Sign in again, then retry."
    if (response.status === 404) return "You don't have access to this session's data."
    let body = null
    try {
        body = await response.json()
    } catch {
        body = null
    }
    if (body && body.message) return body.message
    return `Download failed (HTTP ${response.status}).`
}

// "attachment; filename=x.zip" (or filename*=UTF-8''x.zip) -> "x.zip"
export function attachmentName(disposition, fallback) {
    const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(disposition || "")
    if (!m) return fallback
    return m[1] ? decodeURIComponent(m[1]) : m[2]
}

export function formatBytes(bytes) {
    if (bytes == null) return ""
    const units = ["B", "KB", "MB", "GB", "TB"]
    let value = bytes
    let unit = 0
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024
        unit += 1
    }
    return `${unit === 0 ? value : value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}

// Pods whose recording the Videos section lists, by name (natural order).
// has_video also counts pods with only video metrics; the status check
// tells those apart ("No recording").
export function videoPods(enriched) {
    return Object.values(enriched || {})
        .filter((d) => d && d.has_video)
        .sort((a, b) =>
            String(a.name || "").localeCompare(String(b.name || ""), undefined, { numeric: true }) || a.id - b.id,
        )
}
