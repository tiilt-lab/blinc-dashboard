import { useState, useEffect } from "react"
import { EmptyState } from "../components/empty-state"
import { formatDate } from "../globals"
import { ErrorDialog } from "../components/error-dialog"
import { useNavigate } from "react-router-dom"
import { AppContextMenu } from "../components/context-menu/context-menu-component"
import { GenericDialogBox } from "../dialog/dialog-component"
import { Appheader } from "../header/header-component"
import { AppFolderSelectComponent } from "../components/folder-select/folder-select-component"
import { FolderShareDialog } from "../components/folder-share/folder-share-dialog"
import { SessionService } from "../services/session-service"
import { SessionModel } from "../models/session"
import style from "./sessions.module.css"
import style2 from "../components/context-menu/context-menu.module.css"
import FolderIcon from "../Icons/Folder"
import MicIcon from "../Icons/Mic"
import { Camera, Chevron, Upload } from "@/Icons"
import { StatusPill } from "../components/status-pill"
import { SkeletonRows } from "../components/skeleton"
import { dlgWindow, dlgHeading, dlgInput, dlgPrimary, dlgCancel, btnPrimary, btnSecondary, btnSecondarySm, btnDangerOutlineSm } from "../components/dialog-styles"

const menuItemClass = style2["menu-item"]
const menuDangerClass = `${style2["menu-item"]} ${style2["red"]}`

// Search + filter chips; sorting moved into the table's column headers.
function SortControl({
    count,
    videoFilter,
    setVideoFilter,
    videoCount,
    audioCount,
    searchQuery,
    setSearchQuery,
}) {
    const chip = (value, label) => (
        <button
            onClick={() => setVideoFilter(value)}
            aria-pressed={videoFilter === value}
            className={
                "cursor-pointer rounded-full px-3 py-1 text-xs font-semibold whitespace-nowrap transition " +
                (videoFilter === value
                    ? "bg-tiilt text-white"
                    : "bg-tiilt-line/40 text-tiilt-muted hover:bg-tiilt-soft hover:text-tiilt")
            }
        >
            {label}
        </button>
    )
    return (
        <div className="mb-3 flex flex-wrap items-center gap-3">
            <input
                type="search"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search all sessions…"
                aria-label="Search sessions by name, across all folders"
                className="h-10 w-full max-w-60 rounded-lg border border-tiilt-line bg-white px-3 text-sm text-tiilt-ink transition outline-none placeholder:text-tiilt-muted focus-visible:border-tiilt focus-visible:ring-[3px] focus-visible:ring-tiilt/30"
            />
            <div className="flex items-center gap-1.5">
                {chip("all", `All ${count}`)}
                {chip("video", `Video ${videoCount}`)}
                {chip("audio", `Audio only ${audioCount}`)}
            </div>
        </div>
    )
}


// Small stat cards over ALL sessions (not just the open folder), matching
// the Students page pattern.
function SessionStats({ sessions }) {
    const all = sessions || []
    const live = all.filter((s) => s.recording).length
    const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000
    const thisWeek = all.filter(
        (s) => s.creation_date && s.creation_date.getTime() > weekAgo,
    ).length
    const card = (label, value) => (
        <div className="flex-1 rounded-xl border border-tiilt-line bg-white px-4 py-3">
            <div className="font-ahamono text-[11px] tracking-wider text-tiilt-muted uppercase">
                {label}
            </div>
            <div className="mt-0.5 text-2xl font-bold text-tiilt-ink">
                {value}
            </div>
        </div>
    )
    return (
        <div className="mb-4 flex flex-col gap-3 sm:flex-row">
            {card("Sessions", all.length)}
            {card(
                "Live now",
                live > 0 ? (
                    <span className="flex items-center gap-2">
                        <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-tiilt-danger" />
                        {live}
                    </span>
                ) : (
                    0
                ),
            )}
            {card("This week", thisWeek)}
        </div>
    )
}

// Global analysis-queue panel: appears whenever the post-hoc queue has jobs.
// Polls every 5s; shows progress, the running pod with elapsed time, an ETA
// projected from completed-job durations, and any errors.
function fmtEta(seconds) {
    if (seconds == null || !isFinite(seconds) || seconds <= 0) return null
    const h = Math.floor(seconds / 3600)
    const m = Math.round((seconds % 3600) / 60)
    if (h > 0) return `~${h}h ${m}m`
    if (m >= 1) return `~${m}m`
    return "under a minute"
}

function AnalysisQueuePanel() {
    const navigate = useNavigate()
    const [jobs, setJobs] = useState(null)
    const [expanded, setExpanded] = useState(false)
    const [, setTick] = useState(0) // re-render so elapsed/ETA move
    useEffect(() => {
        let alive = true
        const load = () =>
            new SessionService().getGlobalPosthocQueue().then(
                (r) =>
                    r.status === 200 &&
                    r.json().then((list) => {
                        if (alive) setJobs(list)
                    }),
                () => {},
            )
        load()
        const poll = setInterval(load, 5000)
        const tick = setInterval(() => setTick((x) => x + 1), 1000)
        return () => {
            alive = false
            clearInterval(poll)
            clearInterval(tick)
        }
    }, [])

    if (!jobs || jobs.length === 0) return null

    const byState = (s) => jobs.filter((j) => j.state === s)
    const done = byState("done")
    const running = byState("running")
    const queued = byState("queued")
    const errors = byState("error")
    const finished = done.length + errors.length
    const total = jobs.length
    const active = running.length + queued.length

    // ETA: average duration of completed jobs x remaining work. Before the
    // first job completes there is nothing sound to project from.
    const durations = done
        .filter((j) => j.started_at && j.finished_at)
        .map((j) => j.finished_at - j.started_at)
    const avg = durations.length
        ? durations.reduce((a, b) => a + b, 0) / durations.length
        : null
    const now = Date.now() / 1000
    const runElapsed = running.length && running[0].started_at ? now - running[0].started_at : 0
    const eta =
        avg != null && active > 0
            ? Math.max(avg - runElapsed, 30) + queued.length * avg
            : null

    // Group by session, preserving queue order.
    const groups = []
    for (const j of jobs) {
        let g = groups.find((x) => x.id === j.session_id)
        if (!g) {
            g = { id: j.session_id, name: j.session_name, jobs: [] }
            groups.push(g)
        }
        g.jobs.push(j)
    }

    const stateChip = (j) =>
        j.state === "running" ? (
            <StatusPill tone="orange" pulse className="text-[11px]">
                {j.device_name}
            </StatusPill>
        ) : j.state === "done" ? (
            <StatusPill tone="teal" className="text-[11px]">
                {j.device_name}
            </StatusPill>
        ) : j.state === "error" ? (
            <StatusPill
                tone="danger"
                className="text-[11px]"
                title={j.error || "Failed"}
            >
                {j.device_name}
            </StatusPill>
        ) : (
            <StatusPill tone="neutral" className="text-[11px]">
                {j.device_name}
            </StatusPill>
        )

    const pct = total ? Math.round((finished / total) * 100) : 0
    const countChip = (n, label, cls) =>
        n > 0 ? (
            <span className={"flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold " + cls}>
                {n} {label}
            </span>
        ) : null

    return (
        <div className="mb-4 overflow-hidden rounded-xl border border-tiilt-line bg-white">
            <button
                onClick={() => setExpanded((v) => !v)}
                aria-expanded={expanded}
                className="w-full cursor-pointer px-4 py-3 text-left transition hover:bg-tiilt-soft/30"
            >
                <span className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                    <span className="flex items-center gap-2 text-sm font-semibold text-tiilt-ink">
                        {active > 0 ? (
                            <span className="h-2 w-2 animate-pulse rounded-full bg-tiilt-orange" />
                        ) : errors.length > 0 ? (
                            <span className="h-2 w-2 rounded-full bg-tiilt-danger" />
                        ) : (
                            <span className="h-2 w-2 rounded-full bg-tiilt-teal" />
                        )}
                        Analysis queue
                    </span>
                    {countChip(done.length, "done", "bg-tiilt-teal/15 text-tiilt-teal-text")}
                    {countChip(running.length, "running", "bg-tiilt-orange/15 text-tiilt-orange-text")}
                    {countChip(queued.length, "queued", "bg-tiilt-line/40 text-tiilt-muted")}
                    {countChip(errors.length, errors.length === 1 ? "error" : "errors", "bg-tiilt-danger-soft text-tiilt-danger")}
                    <span className="ml-auto flex items-center gap-2 text-sm whitespace-nowrap text-tiilt-muted">
                        {active > 0 ? (
                            eta != null ? (
                                <span title="Projected from the average duration of completed pods">
                                    {fmtEta(eta)} left
                                </span>
                            ) : (
                                <span>estimating…</span>
                            )
                        ) : (
                            <span className="font-semibold text-tiilt-teal-text">
                                Finished
                            </span>
                        )}
                        <Chevron
                            direction="down"
                            size={14}
                            style={{
                                transform: expanded ? "rotate(180deg)" : "none",
                                transition: "transform 0.15s",
                            }}
                        />
                    </span>
                </span>
                <span className="mt-2.5 flex items-center gap-3">
                    <span className="h-2 grow overflow-hidden rounded-full bg-tiilt-line/40">
                        <span
                            className="block h-full rounded-full bg-tiilt transition-all duration-700"
                            style={{ width: `${pct}%` }}
                        />
                    </span>
                    <span className="flex-none font-ahamono text-xs font-semibold text-tiilt-ink tabular-nums">
                        {finished}/{total}
                    </span>
                </span>
                {running.length > 0 ? (
                    <span className="mt-1.5 block text-xs text-tiilt-muted">
                        Now running{" "}
                        <span className="font-semibold text-tiilt-ink">
                            {running[0].session_name} › {running[0].device_name}
                        </span>
                        {runElapsed > 0 ? (
                            <span className="font-ahamono tabular-nums">
                                {" "}
                                · {Math.floor(runElapsed / 60)}:{String(Math.floor(runElapsed % 60)).padStart(2, "0")}
                            </span>
                        ) : null}
                    </span>
                ) : null}
            </button>
            {expanded ? (
                <div className="flex max-h-80 flex-col gap-3 overflow-y-auto border-t border-tiilt-line bg-tiilt-ground/40 px-4 py-3">
                    {errors.length > 0 ? (
                        <div className="rounded-lg bg-tiilt-danger-soft px-3 py-2 text-xs leading-relaxed text-tiilt-danger">
                            {errors.map((j) => (
                                <div key={j.device_id}>
                                    <span className="font-semibold">
                                        {j.session_name} › {j.device_name}:
                                    </span>{" "}
                                    {j.error || "failed"}
                                </div>
                            ))}
                        </div>
                    ) : null}
                    {groups.map((g) => {
                        const gDone = g.jobs.filter((j) => j.state === "done").length
                        return (
                            <div
                                key={g.id}
                                className="rounded-lg border border-tiilt-line bg-white px-3 py-2"
                            >
                                <div className="flex items-baseline justify-between gap-2">
                                    <button
                                        onClick={() => navigate(`/sessions/${g.id}/overview`)}
                                        className="cursor-pointer text-xs font-semibold text-tiilt hover:underline"
                                    >
                                        {g.name} ›
                                    </button>
                                    <span className="font-ahamono text-[11px] text-tiilt-muted tabular-nums">
                                        {gDone}/{g.jobs.length}
                                    </span>
                                </div>
                                <div className="mt-1.5 flex flex-wrap gap-1">
                                    {g.jobs.map((j) => (
                                        <span key={j.device_id}>{stateChip(j)}</span>
                                    ))}
                                </div>
                            </div>
                        )
                    })}
                </div>
            ) : null}
        </div>
    )
}

const PODS_EXPLAINER =
    "A session is one recorded session. Each pod is a participant group (device) within that session — a session can have several pods running at once. Expand a session to see its pods, and click any pod to open its analysis."

// Upload a recording to be analyzed: creates a session+pod server-side and
// auto-queues the full analysis. Self-contained state so failures are VISIBLE
// (the previous prompt-based flow could fail silently).
function UploadVideoButton() {
    const [busy, setBusy] = useState(false)
    const [note, setNote] = useState("")
    const onPick = (e) => {
        const file = e.target.files && e.target.files[0]
        e.target.value = ""
        if (!file) return
        setBusy(true)
        setNote("Uploading " + file.name + "…")
        const fd = new FormData()
        fd.append("video", file)
        fd.append("name", file.name.replace(/\.[^.]+$/, ""))
        fetch("/api/v1/sessions/upload_video", {
            method: "POST",
            body: fd,
            credentials: "include",
        })
            .then(async (r) => {
                if (r.status === 200) {
                    setNote("Uploaded — analysis queued. Reloading…")
                    setTimeout(() => window.location.reload(), 900)
                } else {
                    const t = await r.text().catch(() => "")
                    setBusy(false)
                    setNote("Upload failed (" + r.status + "): " + t.slice(0, 120))
                }
            })
            .catch((err) => {
                setBusy(false)
                setNote("Upload failed: " + err)
            })
    }
    return (
        <label
            className={
                "flex items-center gap-1.5 rounded-lg border border-tiilt-line bg-white px-4 py-2 text-sm font-semibold text-tiilt-ink transition focus-within:border-tiilt focus-within:ring-[3px] focus-within:ring-tiilt/30 active:translate-y-px " +
                (busy ? "cursor-wait opacity-60" : "cursor-pointer hover:border-tiilt hover:bg-tiilt-soft")
            }
            title={note || "Upload a webm/mp4 recording to analyze"}
        >
            <Upload />
            {busy ? "Uploading…" : "Upload video"}
            {note && !busy ? (
                <span className="max-w-56 truncate text-xs font-normal text-tiilt-danger">{note}</span>
            ) : null}
            {/* sr-only (not display:none) keeps the file input keyboard-focusable */}
            <input type="file" accept="video/webm,video/mp4" className="sr-only" disabled={busy} onChange={onPick} />
        </label>
    )
}

// Folders live inside the sessions table, one row each above the session
// rows: same anatomy as a session row (icon tile, name, context menu), and
// clicking the row opens the folder to show the sessions within it.
function FolderRow({ folder, sessions, onOpen, onOpenSession, openFolderDialog }) {
    const [expanded, setExpanded] = useState(false)
    const count = sessions.length
    return (
        <>
        <tr
            onClick={() => onOpen(folder.id)}
            title={`Open folder ${folder.name}`}
            className="group cursor-pointer border-t border-tiilt-line bg-white transition first:border-t-0 hover:bg-tiilt-soft/50"
        >
            <td className="py-2 pr-1 pl-3" />
            <td className="px-3 py-2">
                <span className="flex h-9 w-9 flex-none items-center justify-center rounded-md bg-tiilt-soft text-tiilt">
                    <FolderIcon className="h-5 w-5" />
                </span>
            </td>
            <td colSpan={5} className="max-w-0 overflow-hidden px-3 py-2">
                <span className="flex min-w-0 items-center gap-2">
                    <span
                        title={folder.name}
                        className="truncate font-semibold text-tiilt-ink"
                    >
                        {folder.name}
                    </span>
                    {!folder.owned ? (
                        <span
                            title={`Shared by ${folder.owner || "another account"} — you are a${folder.access === "editor" ? "n" : ""} ${folder.access}`}
                            className="flex-none rounded-full bg-tiilt-line/40 px-1.5 py-0.5 text-[11px] font-semibold whitespace-nowrap text-tiilt-muted"
                        >
                            Shared{folder.owner ? ` by ${folder.owner}` : ""}
                        </span>
                    ) : null}
                    <span className="flex-none text-xs text-tiilt-muted">
                        {count === 1 ? "1 session" : `${count} sessions`}
                    </span>
                </span>
            </td>
            <td
                className="py-2 pr-3 pl-1 text-right whitespace-nowrap"
                onClick={(e) => e.stopPropagation()}
            >
                <span className="flex items-center justify-end gap-0.5">
                    {count > 0 ? (
                        <button
                            onClick={() => setExpanded((v) => !v)}
                            title={expanded ? "Hide sessions" : "Show sessions"}
                            aria-expanded={expanded}
                            className="flex h-7 w-7 flex-none cursor-pointer items-center justify-center rounded-md text-tiilt-muted transition hover:bg-tiilt-soft hover:text-tiilt"
                        >
                            <Chevron
                                direction="down"
                                style={{
                                    transform: expanded ? "rotate(180deg)" : "none",
                                    transition: "transform 0.15s",
                                }}
                            />
                        </button>
                    ) : null}
                    <AppContextMenu label={`Options for folder ${folder.name}`}>
                        <button
                            role="menuitem"
                            className={menuItemClass}
                            onClick={() => openFolderDialog("ShareFolder", folder)}
                        >
                            {folder.can_manage_members ? "Share..." : "People with access"}
                        </button>
                        {/* Renaming, moving and deleting need manager access. */}
                        {folder.access === "manager" ? (
                            <>
                                <button
                                    role="menuitem"
                                    className={menuItemClass}
                                    onClick={() => openFolderDialog("RenameFolder", folder)}
                                >
                                    Edit Name
                                </button>
                                <button
                                    role="menuitem"
                                    className={menuItemClass}
                                    onClick={() => openFolderDialog("MoveFolder", folder)}
                                >
                                    Move To...
                                </button>
                                <button
                                    role="menuitem"
                                    className={menuDangerClass}
                                    onClick={() => openFolderDialog("DeleteFolder", folder)}
                                >
                                    Delete
                                </button>
                            </>
                        ) : null}
                    </AppContextMenu>
                </span>
            </td>
        </tr>
        {expanded ? (
            <tr className="border-t border-tiilt-line bg-tiilt-ground/40">
                <td colSpan={8} className="px-3 py-1">
                    <ul className="flex flex-col gap-0.5 px-2 py-1.5 pl-[3rem]">
                        {sessions.map((s) => (
                            <li key={s.id}>
                                <button
                                    onClick={() => onOpenSession(s)}
                                    className="flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1 text-left text-xs transition hover:bg-tiilt-soft"
                                >
                                    <span className="min-w-0 grow truncate font-medium text-tiilt-ink">
                                        {s.title}
                                    </span>
                                    <span className="flex-none text-tiilt-muted">
                                        {formatDate(s.creation_date)}
                                    </span>
                                    <span className="flex-none font-ahamono tabular-nums text-tiilt-muted">
                                        {s.lengthFormatted}
                                    </span>
                                    <Chevron size={12} className="flex-none text-tiilt-muted" />
                                </button>
                            </li>
                        ))}
                    </ul>
                </td>
            </tr>
        ) : null}
        </>
    )
}

// Lazily fetches a session's pods and lists each one as a clickable row that
// opens the pod's detail page, with duration, participant count and status.
function PodDurations({ sessionId }) {
    const [pods, setPods] = useState(null) // null = loading
    const [error, setError] = useState(false)
    const navigate = useNavigate()
    useEffect(() => {
        let alive = true
        new SessionService().getSessionDevices(sessionId).then(
            (response) => {
                if (response.status === 200) {
                    response.json().then((devices) => {
                        if (alive) setPods(devices)
                    })
                } else if (alive) {
                    setError(true)
                }
            },
            () => {
                if (alive) setError(true)
            },
        )
        return () => {
            alive = false
        }
    }, [sessionId])

    if (error || pods === null || pods.length === 0) {
        const msg = error
            ? "Couldn't load pods."
            : pods === null
              ? "Loading pods…"
              : "No pods in this session."
        return (
            <div className="border-t border-tiilt-line px-3 py-2 pl-[3.25rem] text-xs text-tiilt-muted">
                {msg}
            </div>
        )
    }

    return (
        <ul className="flex flex-col gap-0.5 border-t border-tiilt-line px-2 py-1.5 pl-[3rem]">
            {pods.map((pod) => (
                <li key={pod.id}>
                    <button
                        onClick={() =>
                            navigate(`/sessions/${sessionId}/pods/${pod.id}`)
                        }
                        className="flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1 text-left text-xs transition hover:bg-tiilt-soft"
                    >
                        <span
                            className={
                                "min-w-0 grow truncate font-medium " +
                                (pod.has_data === false
                                    ? "text-tiilt-muted"
                                    : "text-tiilt-ink")
                            }
                        >
                            {pod.name || `Pod ${pod.id}`}
                        </span>
                        {pod.has_data === false ? (
                            <StatusPill tone="neutral">
                                No data recorded
                            </StatusPill>
                        ) : (
                            <>
                                {pod.analysis_running ? (
                                    <StatusPill tone="orange" pulse>
                                        Analyzing…
                                    </StatusPill>
                                ) : pod.posthoc_analyzed_date ? (
                                    <StatusPill tone="teal">
                                        Analyzed
                                    </StatusPill>
                                ) : null}
                                {pod.speaker_count > 0 ? (
                                    <span className="flex-none text-tiilt-muted">
                                        {pod.speaker_count}{" "}
                                        {pod.speaker_count === 1
                                            ? "participant"
                                            : "participants"}
                                    </span>
                                ) : null}
                                <span className="flex-none font-ahamono tabular-nums text-tiilt-muted">
                                    {SessionModel.formatDuration(pod.duration)}
                                </span>
                            </>
                        )}
                        <Chevron size={12} className="flex-none text-tiilt-muted" />
                    </button>
                </li>
            ))}
        </ul>
    )
}

function SessionRow({ session, rowIndex, onOpen, openSessionDialog, endSession, checked, onToggle, renameInline, folderHint, canModify = true }) {
    const [expanded, setExpanded] = useState(false)
    const [editing, setEditing] = useState(false)
    const hasPods = session.pod_count != null && session.pod_count > 0
    const commitRename = (value) => {
        setEditing(false)
        if (renameInline) renameInline(session, value)
    }
    return (
        <>
            <tr
                onClick={() => onOpen(session)}
                title={`Open ${session.title}`}
                className="group cursor-pointer border-t border-tiilt-line bg-white transition first:border-t-0 hover:bg-tiilt-soft/50"
            >
                <td
                    className="py-2 pr-1 pl-3 text-center"
                    onClick={(e) => e.stopPropagation()}
                >
                    <button
                        onClick={onToggle}
                        role="checkbox"
                        aria-checked={!!checked}
                        aria-label={`Select session ${session.title}`}
                        title={checked ? "Deselect" : "Select for bulk actions"}
                        className={
                            "mx-auto flex h-7 w-8 flex-none cursor-pointer items-center justify-center rounded-md font-ahamono text-xs tabular-nums transition " +
                            (checked
                                ? "bg-tiilt font-bold text-white ring-2 ring-tiilt/40"
                                : "text-tiilt-muted hover:bg-tiilt-soft hover:text-tiilt")
                        }
                    >
                        {checked ? "\u2713" : rowIndex + 1}
                    </button>
                </td>
                <td className="px-3 py-2">
                    <span
                        title={
                            session.has_video
                                ? "Video session"
                                : "Audio-only session"
                        }
                        className={
                            "flex h-9 w-9 flex-none items-center justify-center rounded-md " +
                            (session.recording
                                ? "bg-tiilt-danger-soft text-tiilt-danger"
                                : "bg-tiilt-soft text-tiilt")
                        }
                    >
                        {session.has_video ? (
                            <Camera />
                        ) : (
                            <svg width="20" height="20" viewBox="0 0 20 30">
                                <MicIcon fill="currentColor" />
                            </svg>
                        )}
                    </span>
                </td>
                {/* min-w keeps the cell usable on phones: without it the
                    38%/max-w-0 combo collapsed the column to ~3 characters
                    and the status pill overflowed into the Date column
                    (the table wrapper already scrolls horizontally). */}
                <td className="max-w-0 min-w-44 overflow-hidden px-3 py-2" style={{ width: "38%" }}>
                    {editing ? (
                        <input
                            autoFocus
                            defaultValue={session.title}
                            aria-label="Session name"
                            maxLength={64}
                            onClick={(e) => e.stopPropagation()}
                            onKeyDown={(e) => {
                                if (e.key === "Enter") commitRename(e.target.value)
                                if (e.key === "Escape") setEditing(false)
                            }}
                            onBlur={(e) => commitRename(e.target.value)}
                            className="w-full rounded-md border border-tiilt bg-white px-2 py-0.5 text-sm font-semibold text-tiilt-ink outline-none focus-visible:ring-[3px] focus-visible:ring-tiilt/30"
                        />
                    ) : (
                        <span className="flex min-w-0 items-center gap-1">
                            <span
                                title={session.title}
                                className="truncate font-semibold text-tiilt-ink"
                            >
                                {session.title}
                            </span>
                            {session.recording ? (
                                <StatusPill tone="danger" dot pulse className="text-xs">
                                    Live
                                </StatusPill>
                            ) : session.analysis_running ? (
                                <StatusPill
                                    tone="orange"
                                    pulse
                                    className="text-xs"
                                    title="A full analysis is running right now"
                                >
                                    Analyzing{"\u2026"}
                                </StatusPill>
                            ) : null}
                            {canModify ? (
                                <button
                                    onClick={(e) => {
                                        e.stopPropagation()
                                        setEditing(true)
                                    }}
                                    aria-label={`Rename ${session.title}`}
                                    title="Rename"
                                    className="flex h-6 w-6 flex-none cursor-pointer items-center justify-center rounded text-xs text-tiilt-muted opacity-0 transition group-hover:opacity-100 hover:bg-tiilt-soft hover:text-tiilt focus-visible:opacity-100"
                                >
                                    {"\u270E"}
                                </button>
                            ) : null}
                        </span>
                    )}
                    {session.owner && !session.owned ? (
                        <span
                            title={`Owned by ${session.owner}${canModify ? "" : " \u2014 you can view this session but not change it"}`}
                            className="mt-0.5 flex w-fit flex-none items-center gap-1 rounded-full bg-tiilt-line/40 px-1.5 py-0.5 text-[11px] font-semibold whitespace-nowrap text-tiilt-muted"
                        >
                            {session.owner}
                        </span>
                    ) : null}
                    {folderHint ? (
                        <span
                            title={`In folder ${folderHint}`}
                            className="mt-0.5 flex w-fit flex-none items-center gap-1 rounded-full bg-tiilt-line/40 px-1.5 py-0.5 text-[11px] font-semibold whitespace-nowrap text-tiilt-muted"
                        >
                            <FolderIcon className="h-3 w-3" />
                            {folderHint}
                        </span>
                    ) : null}
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-tiilt-ink">
                    {formatDate(session.creation_date)}
                </td>
                <td className="px-3 py-2 text-right font-ahamono text-xs tabular-nums whitespace-nowrap text-tiilt-ink">
                    {session.lengthFormatted}
                </td>
                <td className="px-3 py-2 text-right font-ahamono tabular-nums whitespace-nowrap text-tiilt-ink">
                    {session.participant_count > 0 ? (
                        session.participant_count
                    ) : (
                        <span className="text-tiilt-muted">{"\u2014"}</span>
                    )}
                </td>
                <td className="px-3 py-2 text-right font-ahamono tabular-nums whitespace-nowrap text-tiilt-ink">
                    {hasPods ? (
                        session.pod_count
                    ) : (
                        <span className="text-tiilt-muted">{"\u2014"}</span>
                    )}
                </td>
                <td
                    className="py-2 pr-3 pl-1 text-right whitespace-nowrap"
                    onClick={(e) => e.stopPropagation()}
                >
                    <span className="flex items-center justify-end gap-0.5">
                        {hasPods ? (
                            <button
                                onClick={() => setExpanded((v) => !v)}
                                title={expanded ? "Hide pods" : "Show pods"}
                                aria-expanded={expanded}
                                className="flex h-7 w-7 flex-none cursor-pointer items-center justify-center rounded-md text-tiilt-muted transition hover:bg-tiilt-soft hover:text-tiilt"
                            >
                                <Chevron
                                    direction="down"
                                    style={{
                                        transform: expanded ? "rotate(180deg)" : "none",
                                        transition: "transform 0.15s",
                                    }}
                                />
                            </button>
                        ) : null}
                        <AppContextMenu label={`Options for session ${session.title}`}>
                            {canModify ? (
                                <>
                                    <button
                                        role="menuitem"
                                        className={menuItemClass}
                                        onClick={() =>
                                            openSessionDialog("RenameSession", session)
                                        }
                                    >
                                        Edit Name
                                    </button>
                                    <button
                                        role="menuitem"
                                        className={menuItemClass}
                                        onClick={() =>
                                            openSessionDialog("MoveSession", session)
                                        }
                                    >
                                        Move To...
                                    </button>
                                    {!session.recording ? (
                                        <button
                                            role="menuitem"
                                            className={menuDangerClass}
                                            onClick={() =>
                                                openSessionDialog("DeleteSession", session)
                                            }
                                        >
                                            Delete
                                        </button>
                                    ) : (
                                        <button
                                            role="menuitem"
                                            className={menuDangerClass}
                                            onClick={() => endSession(session)}
                                        >
                                            End
                                        </button>
                                    )}
                                </>
                            ) : (
                                // The server refuses these for a session you do not
                                // own, so offering them would only produce a 404.
                                <span className="block px-3 py-2 text-xs text-tiilt-muted">
                                    View only{"—"}owned by {session.owner}
                                </span>
                            )}
                        </AppContextMenu>
                    </span>
                </td>
            </tr>
            {expanded ? (
                <tr className="border-t border-tiilt-line bg-tiilt-ground/40">
                    <td colSpan={8} className="px-3 py-1">
                        <PodDurations sessionId={session.id} />
                    </td>
                </tr>
            ) : null}
        </>
    )
}

// Clickable, aria-sorted column header driving the shared sortBy state
// ("date-desc" style keys).
function SortableTh({ label, sortKey, sortBy, setSortBy, alignRight, defaultDir = "desc" }) {
    const active = sortBy.startsWith(sortKey + "-")
    const dir = active ? sortBy.slice(sortKey.length + 1) : null
    const next = active
        ? `${sortKey}-${dir === "asc" ? "desc" : "asc"}`
        : `${sortKey}-${defaultDir}`
    return (
        <th
            aria-sort={
                active ? (dir === "asc" ? "ascending" : "descending") : undefined
            }
            className={
                "sticky top-0 bg-tiilt-ground px-3 py-2.5 whitespace-nowrap " +
                (alignRight ? "text-right" : "text-left")
            }
        >
            <button
                onClick={() => setSortBy(next)}
                className="cursor-pointer text-sm font-semibold text-tiilt-ink transition hover:text-tiilt"
            >
                {label}
                <span
                    aria-hidden="true"
                    className={
                        "ml-1 inline-block text-[10px] " +
                        (active ? "text-tiilt" : "text-tiilt-line")
                    }
                >
                    {active && dir === "asc" ? "\u25B2" : "\u25BC"}
                </span>
            </button>
        </th>
    )
}

function DiscussionSessionPage(props) {
    return (
        <>
            <div role="main" className="main-container">
                <Appheader
                    title={"Manage Sessions"}
                    leftText={false}
                    rightText={""}
                    nav={props.navigateToHomescreen}
                />

                <div className="relative min-h-0 w-full grow overflow-y-auto">
                    <div className="mx-auto w-full max-w-4xl px-4 py-8">
                        {props.isLoading ? (
                            <SkeletonRows rows={8} className="mt-14" />
                        ) : (
                            <></>
                        )}
                        {!props.isLoading &&
                        props.sessions.length === 0 &&
                        props.folders.length === 0 ? (
                            <EmptyState
                                title="No sessions or folders"
                                subtitle="Record your first session or create a folder with the buttons above."
                            />
                        ) : (
                            <></>
                        )}

                        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                            <nav className="flex min-w-0 items-center gap-2 text-lg">
                                {props.breadcrumbs.length > 0 ? (
                                    <button
                                        onClick={props.goBackToPrevious}
                                        className="flex items-center gap-2 font-semibold text-tiilt hover:underline"
                                    >
<Chevron direction="left" size={16} />
                                        Home
                                    </button>
                                ) : (
                                    <span className="font-semibold text-tiilt-ink">
                                        Home
                                    </span>
                                )}
                                {props.breadcrumbs.length > 1 ? (
                                    <span className="text-tiilt-muted">
                                        / … /
                                    </span>
                                ) : props.breadcrumbs.length === 1 ? (
                                    <span className="text-tiilt-muted">/</span>
                                ) : (
                                    <></>
                                )}
                                {props.breadcrumbs.length > 0 ? (
                                    <span className="max-w-60 truncate font-semibold text-tiilt-ink">
                                        {
                                            props.breadcrumbs[
                                                props.breadcrumbs.length - 1
                                            ].name
                                        }
                                    </span>
                                ) : (
                                    <></>
                                )}
                                <span
                                    tabIndex={0}
                                    role="note"
                                    title={PODS_EXPLAINER}
                                    aria-label={PODS_EXPLAINER}
                                    className="flex h-5 w-5 flex-none cursor-help items-center justify-center rounded-full bg-tiilt-line/40 text-[11px] font-bold text-tiilt-muted transition hover:bg-tiilt-soft hover:text-tiilt"
                                >
                                    ?
                                </span>
                            </nav>
                            <div className="flex flex-wrap justify-end gap-2">
                                {/* Viewers of a shared folder cannot add to it. */}
                                {!props.breadcrumbs.length ||
                                props.breadcrumbs[props.breadcrumbs.length - 1].access !== "viewer" ? (
                                    <>
                                        <button
                                            className={btnSecondary + " flex items-center gap-1.5"}
                                            onClick={() => props.openFolderDialog("NewFolder")}
                                        >
                                            <FolderIcon className="h-4 w-4" />
                                            New folder
                                        </button>
                                        <UploadVideoButton />
                                        <button
                                            className={btnPrimary + " flex items-center gap-1.5"}
                                            onClick={props.newRecording}
                                        >
                                            <span aria-hidden="true" className="text-base leading-none">+</span>
                                            New session
                                        </button>
                                    </>
                                ) : (
                                    <span className="self-center text-xs text-tiilt-muted">
                                        View only {"\u2014"} shared by {props.breadcrumbs[props.breadcrumbs.length - 1].owner}
                                    </span>
                                )}
                            </div>
                        </div>

                        {!props.isLoading ? (
                            <SessionStats sessions={props.sessions} />
                        ) : null}

                        {!props.isLoading ? <AnalysisQueuePanel /> : null}

                        {!props.isLoading &&
                        (props.videoCount + props.audioCount > 0 ||
                            props.displayedFolders.length > 0 ||
                            props.searchingAll) ? (
                            <div className="mt-4">
                                <SortControl
                                    searchQuery={props.searchQuery}
                                    setSearchQuery={props.setSearchQuery}
                                    count={props.videoCount + props.audioCount}
                                    videoFilter={props.videoFilter}
                                    setVideoFilter={props.setVideoFilter}
                                    videoCount={props.videoCount}
                                    audioCount={props.audioCount}
                                />
                                {props.selectedCount > 0 ? (
                                    <div className="mb-3 flex flex-wrap items-center gap-3 rounded-lg border border-tiilt-line bg-tiilt-soft/50 px-3.5 py-2 text-sm">
                                        <span className="font-semibold text-tiilt-ink">
                                            {props.selectedCount} selected
                                        </span>
                                        <button
                                            onClick={() => props.openBulkDialog("MoveSessions")}
                                            className={btnSecondarySm}
                                        >
                                            Move to…
                                        </button>
                                        <button
                                            onClick={() => props.openBulkDialog("DeleteSessions")}
                                            className={btnDangerOutlineSm}
                                        >
                                            Delete…
                                        </button>
                                        <button
                                            onClick={props.clearSelected}
                                            className="ml-auto text-xs font-semibold text-tiilt-muted transition hover:text-tiilt"
                                        >
                                            Clear
                                        </button>
                                    </div>
                                ) : null}
                                {props.displayedSessions.length > 0 ||
                                (!props.searchingAll &&
                                    props.displayedFolders.length > 0) ? (
                                    <div className="overflow-x-auto rounded-xl border border-tiilt-line bg-white">
                                        <table className="w-full border-collapse text-left text-sm">
                                            <thead>
                                                <tr className="border-b border-tiilt-line">
                                                    <th className="sticky top-0 bg-tiilt-ground py-2.5 pr-1 pl-3 text-center">
                                                        <input
                                                            type="checkbox"
                                                            title="Select all displayed sessions"
                                                            aria-label="Select all displayed sessions"
                                                            className="h-4 w-4 cursor-pointer accent-tiilt"
                                                            checked={
                                                                props.displayedSessions.length > 0 &&
                                                                props.displayedSessions.every((s) => props.selectedIds[s.id])
                                                            }
                                                            onChange={() =>
                                                                props.toggleSelectAll(
                                                                    props.displayedSessions.map((s) => s.id),
                                                                )
                                                            }
                                                        />
                                                    </th>
                                                    <SortableTh
                                                        label="Type"
                                                        sortKey="type"
                                                        sortBy={props.sortBy}
                                                        setSortBy={props.setSortBy}
                                                    />
                                                    <SortableTh
                                                        label="Session"
                                                        sortKey="name"
                                                        defaultDir="asc"
                                                        sortBy={props.sortBy}
                                                        setSortBy={props.setSortBy}
                                                    />
                                                    <SortableTh
                                                        label="Date"
                                                        sortKey="date"
                                                        sortBy={props.sortBy}
                                                        setSortBy={props.setSortBy}
                                                    />
                                                    <SortableTh
                                                        label="Duration"
                                                        sortKey="length"
                                                        alignRight
                                                        sortBy={props.sortBy}
                                                        setSortBy={props.setSortBy}
                                                    />
                                                    <SortableTh
                                                        label="People"
                                                        sortKey="participants"
                                                        alignRight
                                                        sortBy={props.sortBy}
                                                        setSortBy={props.setSortBy}
                                                    />
                                                    <SortableTh
                                                        label="Pods"
                                                        sortKey="pods"
                                                        alignRight
                                                        sortBy={props.sortBy}
                                                        setSortBy={props.setSortBy}
                                                    />
                                                    <th className="sticky top-0 bg-tiilt-ground py-2.5 pr-3 pl-1">
                                                        <span className="sr-only">
                                                            Actions
                                                        </span>
                                                    </th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {/* Folder rows come first; a global search spans
                                                    every folder, so they hide while searching. */}
                                                {!props.searchingAll
                                                    ? [...props.displayedFolders]
                                                          .sort((a, b) => a.name.localeCompare(b.name))
                                                          .map((folder) => (
                                                              <FolderRow
                                                                  key={"folder-" + folder.id}
                                                                  folder={folder}
                                                                  sessions={(props.sessions || []).filter((x) => x.folder === folder.id)}
                                                                  onOpen={props.displayFolder}
                                                                  onOpenSession={props.goToSession}
                                                                  openFolderDialog={props.openFolderDialog}
                                                              />
                                                          ))
                                                    : null}
                                                {props.displayedSessions.map(
                                                    (session, rowIndex) => (
                                                        <SessionRow
                                                            key={session.id}
                                                            rowIndex={rowIndex}
                                                            session={session}
                                                            // Owner, super, or an editor of the session's
                                                            // folder; the server decides (can_modify).
                                                            canModify={session.can_modify}
                                                            checked={props.selectedIds[session.id]}
                                                            onToggle={() => props.toggleSelected(session.id)}
                                                            onOpen={props.goToSession}
                                                            renameInline={props.renameSessionInline}
                                                            folderHint={
                                                                props.searchingAll
                                                                    ? (props.folders.find((f) => f.id === session.folder) || {}).name || "Home"
                                                                    : null
                                                            }
                                                            openSessionDialog={
                                                                props.openSessionDialog
                                                            }
                                                            endSession={
                                                                props.endSession
                                                            }
                                                        />
                                                    ),
                                                )}
                                            </tbody>
                                        </table>
                                    </div>
                                ) : (
                                    <div className="py-8 text-center text-sm text-tiilt-muted">
                                        {props.searchingAll
                                            ? "No sessions match your search (searched every folder)."
                                            : props.videoFilter === "video"
                                              ? "No sessions with video."
                                              : "No audio-only sessions."}
                                    </div>
                                )}
                            </div>
                        ) : (
                            <></>
                        )}

                        {(props.folders.length > 0 ||
                            props.sessions.length > 0) &&
                        !props.isLoading ? (
                            <div>
                                {props.displayedFolders.length === 0 &&
                                props.displayedSessions.length === 0 ? (
                                    <EmptyState
                                        title="This folder is empty"
                                        subtitle="Move sessions here or record a new one."
                                    />
                                ) : (
                                    <></>
                                )}
                            </div>
                        ) : (
                            <></>
                        )}
                    </div>
                </div>
            </div>

            <GenericDialogBox onClose={props.closeDialog} show={props.currentForm !== ""}>
                {props.currentForm === "RenameSession" ? (
                    <div
                        className={dlgWindow}
                        style={{ minWidth: "min(20rem, 76vw)" }}
                    >
                        <div className={dlgHeading}>
                            Update Session Name:
                        </div>
                        <input
                            id="txtName"
                            defaultValue={props.selectedSession.title}
                            aria-label="Session name"
                            className={dlgInput}
                            maxLength="64"
                        />
                        <div>
                            {props.invalidName
                                ? "Your proposed rename is invalid."
                                : ""}
                        </div>
                        <button
                            className={dlgPrimary}
                            onClick={() =>
                                props.changeSessionName(
                                    document.getElementById("txtName").value,
                                )
                            }
                        >
                            {" "}
                            Confirm
                        </button>
                        <button
                            className={dlgCancel}
                            onClick={props.closeDialog}
                        >
                            {" "}
                            Cancel
                        </button>
                    </div>
                ) : (
                    <></>
                )}

                {props.currentForm === "RenameFolder" ? (
                    <div
                        className={dlgWindow}
                        style={{ minWidth: "min(20rem, 76vw)" }}
                    >
                        <div className={dlgHeading}>
                            Update Folder Name:
                        </div>
                        <input
                            id="txtName"
                            defaultValue={props.selectedFolder.name}
                            aria-label="Folder name"
                            className={dlgInput}
                            maxLength="64"
                        />
                        <div>
                            {props.invalidName
                                ? "Your proposed rename is invalid."
                                : ""}
                        </div>
                        <button
                            className={dlgPrimary}
                            onClick={() => {
                                props.changeFolderName(
                                    document.getElementById("txtName").value,
                                )
                            }}
                        >
                            {" "}
                            Confirm
                        </button>
                        <button
                            className={dlgCancel}
                            onClick={props.closeDialog}
                        >
                            {" "}
                            Cancel
                        </button>
                    </div>
                ) : (
                    <></>
                )}

                {props.currentForm === "DeleteSession" ? (
                    <div
                        className={dlgWindow}
                        style={{ minWidth: "min(20rem, 76vw)" }}
                    >
                        <div className={style["dialog-body"]}>
                            Are you sure you want to permanently delete this
                            session?
                        </div>
                        <button
                            className={style["delete-button"]}
                            onClick={props.deleteSession}
                        >
                            {" "}
                            Delete
                        </button>
                        <button
                            className={dlgCancel}
                            onClick={props.closeDialog}
                        >
                            {" "}
                            Cancel
                        </button>
                    </div>
                ) : (
                    <></>
                )}

                {props.currentForm === "DeleteFolder" ? (
                    <div
                        className={dlgWindow}
                        style={{ minWidth: "min(20rem, 76vw)" }}
                    >
                        <div className={dlgHeading}>
                            {" "}
                            Delete Folder
                        </div>
                        <div className={style["dialog-body"]}>
                            {" "}
                            Are you sure you want to permanently delete this
                            folder and all of its contents?
                        </div>
                        <button
                            className={style["delete-button"]}
                            onClick={props.deleteFolder}
                        >
                            {" "}
                            Delete
                        </button>
                        <button
                            className={dlgCancel}
                            onClick={props.closeDialog}
                        >
                            {" "}
                            Cancel
                        </button>
                    </div>
                ) : (
                    <></>
                )}

                {props.currentForm === "ShareFolder" && props.selectedFolder ? (
                    <FolderShareDialog
                        folder={props.selectedFolder}
                        me={props.me}
                        onClose={props.closeDialog}
                        onLeft={() => {
                            props.closeDialog()
                            props.displayFolder()
                        }}
                    />
                ) : (
                    <></>
                )}

                {props.currentForm === "NewFolder" ? (
                    <div
                        className={dlgWindow}
                        style={{ minWidth: "min(20rem, 76vw)" }}
                    >
                        <div className={dlgHeading}>
                            {" "}
                            Add New Folder
                        </div>
                        <input
                            placeholder="Enter new folder name"
                            aria-label="New folder name"
                            id="txtName"
                            className={dlgInput}
                            maxLength="64"
                        />
                        <button
                            className={dlgPrimary}
                            onClick={() =>
                                props.addFolder(
                                    document.getElementById("txtName").value,
                                    props.breadcrumbs.length
                                        ? props.breadcrumbs[
                                              props.breadcrumbs.length - 1
                                          ].id
                                        : null,
                                )
                            }
                        >
                            {" "}
                            Confirm
                        </button>

                        <button
                            className={dlgCancel}
                            onClick={props.closeDialog}
                        >
                            {" "}
                            Cancel
                        </button>
                    </div>
                ) : (
                    <></>
                )}

                {props.currentForm === "MoveFolder" ? (
                    <div
                        className={dlgWindow}
                        style={{ minWidth: "min(20rem, 76vw)" }}
                    >
                        <div className={dlgHeading}>
                            Move Folder
                        </div>
                        <AppFolderSelectComponent
                            selectableFolders={props.selectableFolders}
                            setFolderSelect={props.setFolderSelect}
                            breadcrumbs={props.breadcrumbs}
                        />
                        {/* <app-folder-select #folderSelect [folders] = "selectableFolders" ></app - folder - select > */}
                        {props.folderSelect !== null ? (
                            <button
                                className={dlgPrimary}
                                onClick={() =>
                                    props.moveFolder(props.folderSelect)
                                }
                            >
                                {" "}
                                OK
                            </button>
                        ) : (
                            <></>
                        )}
                        <button
                            className={dlgCancel}
                            onClick={props.closeDialog}
                        >
                            {" "}
                            Cancel
                        </button>
                    </div>
                ) : (
                    <></>
                )}

                {props.currentForm === "MoveSession" ? (
                    <div
                        className={dlgWindow}
                        style={{ minWidth: "min(20rem, 76vw)" }}
                    >
                        <div className={dlgHeading}>
                            Move Session
                        </div>
                        <AppFolderSelectComponent
                            selectableFolders={props.selectableFolders}
                            setFolderSelect={props.setFolderSelect}
                            breadcrumbs={props.breadcrumbs}
                        />
                        {/* <app-folder-select #folderSelect [folders] = "selectableFolders" ></app - folder - select > */}
                        {props.folderSelect !== null ? (
                            <button
                                className={dlgPrimary}
                                onClick={() =>
                                    props.moveSession(props.folderSelect)
                                }
                            >
                                {" "}
                                OK
                            </button>
                        ) : (
                            <></>
                        )}
                        <button
                            className={dlgCancel}
                            onClick={props.closeDialog}
                        >
                            {" "}
                            Cancel
                        </button>
                    </div>
                ) : (
                    <></>
                )}

                {props.currentForm === "MoveSessions" ? (
                    <div
                        className={dlgWindow}
                        style={{ minWidth: "min(20rem, 76vw)" }}
                    >
                        <div className={dlgHeading}>
                            Move {props.selectedCount} session{props.selectedCount === 1 ? "" : "s"}
                        </div>
                        <AppFolderSelectComponent
                            selectableFolders={props.selectableFolders}
                            setFolderSelect={props.setFolderSelect}
                            breadcrumbs={props.breadcrumbs}
                        />
                        {props.folderSelect !== null ? (
                            <button
                                className={dlgPrimary}
                                onClick={() =>
                                    props.bulkMoveSessions(props.folderSelect)
                                }
                            >
                                Move
                            </button>
                        ) : (
                            <></>
                        )}
                        <button
                            className={dlgCancel}
                            onClick={props.closeDialog}
                        >
                            Cancel
                        </button>
                    </div>
                ) : (
                    <></>
                )}

                {props.currentForm === "DeleteSessions" ? (
                    <div
                        className={dlgWindow}
                        style={{ minWidth: "min(20rem, 76vw)" }}
                    >
                        <div className={dlgHeading}>
                            Delete {props.selectedCount} session{props.selectedCount === 1 ? "" : "s"}
                        </div>
                        <div className={style["dialog-body"]}>
                            This permanently deletes the selected sessions and
                            all of their recordings and analyses. This cannot
                            be undone.
                        </div>
                        <button
                            className={style["delete-button"]}
                            onClick={props.bulkDeleteSessions}
                        >
                            Delete {props.selectedCount}
                        </button>
                        <button
                            className={dlgCancel}
                            onClick={props.closeDialog}
                        >
                            Cancel
                        </button>
                    </div>
                ) : (
                    <></>
                )}

                {props.currentForm === "Loading" ? (
                    <div
                        className={dlgWindow}
                        style={{ minWidth: "min(20rem, 76vw)" }}
                    >
                        <div className={dlgHeading}>
                            Loading...please wait...
                        </div>
                    </div>
                ) : (
                    <></>
                )}
            </GenericDialogBox>

            <ErrorDialog message={props.alertMessage} show={props.showAlert} onClose={props.closeAlert} />
        </>
    )
}

export { DiscussionSessionPage }
