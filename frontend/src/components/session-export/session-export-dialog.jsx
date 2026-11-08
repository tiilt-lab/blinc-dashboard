import { useEffect, useMemo, useRef, useState } from "react"
import { SessionService } from "../../services/session-service"
import { startPolling } from "../../globals"
import { downloadBlob } from "../../utilities/download"
import { InlineSpinner } from "../inline-spinner"
import { dlgBody, dlgCancel, dlgError, dlgHeading, dlgLabel, dlgPrimary, dlgSelect, btnPrimarySm } from "../dialog-styles"
import {
    DATASETS,
    WINDOW_SIZES,
    attachmentName,
    errorMessage,
    formatBytes,
    loadItems,
    needsWindow,
    saveItems,
    videoPods,
} from "./session-export"

// While a pod's video is being prepared on the server, ask again this often.
const VIDEO_POLL_MS = 4000

// The overview's Download dialog: the session's data as one ZIP of CSV files,
// and each pod's recorded video (prepared on the server on first request).
// Both need a signed-in user with access to the session.
function SessionExportDialog({ session, enriched, onClose }) {
    const [items, setItems] = useState(loadItems)
    const [windowSize, setWindowSize] = useState(0)
    const [building, setBuilding] = useState(false)
    const [error, setError] = useState("")
    const [videos, setVideos] = useState({})
    const [videoError, setVideoError] = useState("")
    const videosRef = useRef(videos)
    videosRef.current = videos

    const pods = useMemo(() => videoPods(enriched), [enriched])
    const podIds = pods.map((p) => p.id).join(",")

    // Ask for each pod's video until it is ready, failed or absent; the
    // first ask starts the server preparing it.
    useEffect(() => {
        if (!podIds) return
        const ids = podIds.split(",").map(Number)
        return startPolling(async (signal) => {
            const pending = ids.filter((id) => {
                const v = videosRef.current[id]
                return !v || v.state === "preparing"
            })
            for (const id of pending) {
                const r = await new SessionService().getVideoDownloadStatus(session.id, id, { signal })
                if (r.status === 401) {
                    setVideoError("Your sign-in has expired. Sign in again to download videos.")
                    return false
                }
                if (r.status !== 200 && r.status !== 404) return false
                const status = r.status === 404 ? { state: "none" } : await r.json()
                setVideos((cur) => ({ ...cur, [id]: status }))
            }
            setVideoError("")
            return true
        }, VIDEO_POLL_MS)
    }, [session.id, podIds])

    const toggle = (key) =>
        setItems((cur) => (cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key]))

    const downloadData = async () => {
        setBuilding(true)
        setError("")
        try {
            const r = await new SessionService().exportSessionData(session.id, items, windowSize)
            if (r.status !== 200) {
                setError(await errorMessage(r))
                return
            }
            const name = attachmentName(r.headers.get("Content-Disposition"), `session${session.id}_data.zip`)
            downloadBlob(await r.blob(), name)
            saveItems(items)
        } catch {
            setError("Download failed. Check your connection and try again.")
        } finally {
            setBuilding(false)
        }
    }

    const service = new SessionService()

    return (
        <div className={dlgBody + " max-w-[min(36rem,92vw)]"}>
            <div className={dlgHeading}>Download session data</div>

            <div className="flex items-baseline justify-between">
                <div className={dlgLabel}>Data (one .zip of CSV files)</div>
                <div className="flex gap-3 text-xs">
                    <button type="button" className="text-tiilt hover:underline" onClick={() => setItems(DATASETS.map((d) => d.key))}>
                        All
                    </button>
                    <button type="button" className="text-tiilt hover:underline" onClick={() => setItems([])}>
                        None
                    </button>
                </div>
            </div>
            <div className="grid grid-cols-1 gap-x-4 gap-y-1.5 sm:grid-cols-2">
                {DATASETS.map((d) => (
                    <label key={d.key} className="flex cursor-pointer items-center gap-2 text-sm text-tiilt-ink">
                        <input
                            type="checkbox"
                            className="accent-tiilt"
                            checked={items.includes(d.key)}
                            onChange={() => toggle(d.key)}
                        />
                        {d.label}
                    </label>
                ))}
            </div>
            {needsWindow(items) ? (
                <label className="flex flex-col gap-1 text-sm text-tiilt-ink">
                    Window for the metric files
                    <select
                        aria-label="Window size in seconds"
                        className={dlgSelect}
                        value={windowSize}
                        onChange={(e) => setWindowSize(Number(e.target.value))}
                    >
                        {WINDOW_SIZES.map((w) => (
                            <option key={w} value={w}>
                                {w === 0 ? "None (every line / video sample)" : `${w} seconds`}
                            </option>
                        ))}
                    </select>
                </label>
            ) : null}
            {error ? <div className={dlgError}>{error}</div> : null}
            <button
                type="button"
                className={dlgPrimary + " flex items-center justify-center gap-2 disabled:opacity-50"}
                disabled={building || items.length === 0}
                onClick={downloadData}
            >
                {building ? (
                    <>
                        <InlineSpinner /> Building…
                    </>
                ) : (
                    "Download data (.zip)"
                )}
            </button>

            <div className={dlgLabel + " mt-3"}>Videos (one file per pod)</div>
            {videoError ? <div className={dlgError}>{videoError}</div> : null}
            {pods.length === 0 ? (
                <div className="text-sm text-tiilt-muted">No pod in this session recorded video.</div>
            ) : (
                <ul className="flex max-h-64 flex-col divide-y divide-tiilt-line overflow-y-auto">
                    {pods.map((pod) => (
                        <li key={pod.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                            <span className="min-w-0 truncate font-medium text-tiilt-ink">{pod.name || `Pod ${pod.id}`}</span>
                            <VideoStatus
                                status={videos[pod.id]}
                                url={(part) => service.videoDownloadUrl(session.id, pod.id, part)}
                            />
                        </li>
                    ))}
                </ul>
            )}

            <button type="button" className={dlgCancel} onClick={onClose}>
                Close
            </button>
        </div>
    )
}

function VideoStatus({ status, url }) {
    const muted = "flex items-center gap-2 text-tiilt-muted"
    if (!status) {
        return (
            <span className={muted}>
                <InlineSpinner /> Checking…
            </span>
        )
    }
    if (status.state === "preparing") {
        return (
            <span className={muted} title="Long recordings can take a few minutes. You can keep this open or come back later.">
                <InlineSpinner /> Preparing…
            </span>
        )
    }
    if (status.state === "ready") {
        return (
            <span className="flex items-center gap-2">
                <span className="text-tiilt-muted">{formatBytes(status.bytes)}</span>
                <a className={btnPrimarySm} href={url()} download={status.filename}>
                    Download
                </a>
            </span>
        )
    }
    if (status.state === "failed") {
        return (
            <span className="flex flex-wrap items-center justify-end gap-2">
                <span className="text-tiilt-muted">Couldn't join the recording. Download its files:</span>
                {status.parts.map((p) => (
                    <a key={p.part} className={btnPrimarySm} href={url(p.part)} download>
                        Part {p.part} ({formatBytes(p.bytes)})
                    </a>
                ))}
            </span>
        )
    }
    return <span className="text-tiilt-muted">No recording</span>
}

export { SessionExportDialog }
