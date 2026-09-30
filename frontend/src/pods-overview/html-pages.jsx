import React, { useState, useEffect } from "react"
import { SessionModel } from "../models/session"
import { dlgHeading, dlgBody, dlgSelect, dlgPrimary, dlgCancel, dlgDanger, btnPrimary, btnPrimarySm, btnDangerOutlineSm, btnSecondary } from "../components/dialog-styles"
import { Appheader } from "../header/header-component"
import { GenericDialogBox } from "../dialog/dialog-component"
import { AppSessionToolbar } from "../session-toolbar/session-toolbar-component"
import { SessionTabs } from "../components/session-tabs"
import { SkeletonRows } from "../components/skeleton"

import MicIcon from "../Icons/Mic"
import { Camera, Chevron } from "@/Icons"
import { StatusPill } from "../components/status-pill"
import { ToastStack } from "../components/toast"
import { QrCode } from "../components/qr-code"


// null (hide) for missing durations, else SessionModel's shared H:MM:SS.
function fmtDur(seconds) {
    if (seconds == null || isNaN(seconds)) return null
    return SessionModel.formatDuration(seconds)
}

function PodStatus({ device, enrich, queue }) {
    const e = enrich || {}
    if (device.connected)
        return (
            <StatusPill tone="danger" pulse>
                Recording
            </StatusPill>
        )
    if (queue === "error") return <StatusPill tone="danger">Error</StatusPill>
    if (queue === "queued") return <StatusPill tone="brand">Queued</StatusPill>
    if (queue === "running" || e.analysis_running)
        return (
            <StatusPill tone="orange" pulse>
                Running…
            </StatusPill>
        )
    if (e.has_data === false)
        return <StatusPill tone="neutral">No data</StatusPill>
    if (device.posthoc_analyzed_date || e.posthoc_analyzed_date)
        return (
            <StatusPill
                tone="teal"
                title={
                    "Full analysis run " +
                    (device.posthoc_analyzed_date || e.posthoc_analyzed_date)
                }
            >
                Analyzed
            </StatusPill>
        )
    // Has data, no analysis yet: the recording ended and is ready to view.
    return <StatusPill tone="neutral">Ended</StatusPill>
}

// Live triage badges for a connected pod: silent / dominated / hanging
// question. Only shown while the pod is connected and has an alert.
function TriageBadges({ alerts, connected }) {
    if (!connected || !alerts) return null
    const badges = []
    if (alerts.silent)
        badges.push({ key: "silent", tone: "orange", label: "Silent" })
    if (alerts.dominated_by)
        badges.push({
            key: "dom",
            tone: "orange",
            label: `${alerts.dominated_by.name} ${Math.round(alerts.dominated_by.share * 100)}%`,
        })
    if (alerts.hanging_question)
        badges.push({ key: "q", tone: "brand", label: "Question hanging" })
    return badges.map((b) => (
        <span
            key={b.key}
            className={
                "rounded px-1.5 py-0.5 text-[10px] font-semibold " +
                (b.tone === "brand"
                    ? "bg-tiilt-soft text-tiilt"
                    : "bg-tiilt-orange/15 text-tiilt-orange-text")
            }
        >
            {b.label}
        </span>
    ))
}

function PodRow({ device, enrich, alerts, onOpen, checked, onToggle, queue, index, lastSpoke, renameInline, onDelete }) {
    const e = enrich || {}
    const name =
        device.name && String(device.name).trim()
            ? device.name
            : `Pod ${index + 1}`
    const dur = fmtDur(e.duration)
    // A connected pod is always clickable — live views (transcript, facial
    // analytics) are exactly what you open before any data lands. The
    // no-data lockout is only for ended pods that truly recorded nothing.
    const noData = e.has_data === false && !device.connected
    const [editing, setEditing] = useState(false)
    const commitRename = (value) => {
        setEditing(false)
        if (renameInline) renameInline(device, value)
    }
    return (
        <tr
            onClick={noData ? undefined : onOpen}
            title={
                noData
                    ? "No data was recorded for this pod — there is nothing to view"
                    : `Open ${name}`
            }
            className={
                "group border-t border-tiilt-line bg-white transition first:border-t-0 " +
                (noData
                    ? "cursor-not-allowed opacity-60"
                    : "cursor-pointer hover:bg-tiilt-soft/50")
            }
        >
            <td
                className="py-2 pr-1 pl-3 text-center"
                onClick={(ev) => ev.stopPropagation()}
            >
                <button
                    onClick={onToggle}
                    role="checkbox"
                    aria-checked={!!checked}
                    aria-label={`Select ${name} for batch analysis`}
                    title={checked ? "Deselect" : "Select for batch analysis"}
                    className={
                        "mx-auto flex h-7 w-8 flex-none cursor-pointer items-center justify-center rounded-md font-ahamono text-xs tabular-nums transition " +
                        (checked
                            ? "bg-tiilt font-bold text-white ring-2 ring-tiilt/40"
                            : "text-tiilt-muted hover:bg-tiilt-soft hover:text-tiilt")
                    }
                >
                    {checked ? "✓" : index + 1}
                </button>
            </td>
            <td className="px-3 py-2">
                <span
                    title={e.has_video ? "Video pod" : "Audio-only pod"}
                    className={
                        "relative flex h-9 w-9 flex-none items-center justify-center rounded-md " +
                        (device.connected
                            ? "bg-tiilt-danger-soft text-tiilt-danger"
                            : "bg-tiilt-soft text-tiilt-muted")
                    }
                >
                    {e.has_video ? (
                        <Camera />
                    ) : (
                        <svg width="18" height="28" viewBox="0 0 17 27">
                            <MicIcon fill="currentColor" />
                        </svg>
                    )}
                    {device.button_pressed ? (
                        <span className="absolute -top-1 -right-1 h-3 w-3 animate-ping rounded-full bg-tiilt-orange" />
                    ) : null}
                </span>
            </td>
            <td className="px-3 py-2">
                {editing ? (
                    <input
                        autoFocus
                        defaultValue={name}
                        aria-label="Group name"
                        maxLength={64}
                        onClick={(ev) => ev.stopPropagation()}
                        onKeyDown={(ev) => {
                            if (ev.key === "Enter") commitRename(ev.target.value)
                            if (ev.key === "Escape") setEditing(false)
                        }}
                        onBlur={(ev) => commitRename(ev.target.value)}
                        className="w-full rounded-md border border-tiilt bg-white px-2 py-0.5 text-sm font-semibold text-tiilt-ink outline-none focus-visible:ring-[3px] focus-visible:ring-tiilt/30"
                    />
                ) : (
                <span className="flex items-center gap-2">
                    <span
                        title={name}
                        className="min-w-0 truncate font-semibold text-tiilt-ink"
                    >
                        {name}
                    </span>
                    {renameInline ? (
                        <button
                            onClick={(ev) => {
                                ev.stopPropagation()
                                setEditing(true)
                            }}
                            aria-label={`Rename ${name}`}
                            title="Rename group"
                            className="flex h-6 w-6 flex-none cursor-pointer items-center justify-center rounded text-xs text-tiilt-muted opacity-0 transition group-hover:opacity-100 hover:bg-tiilt-soft hover:text-tiilt focus-visible:opacity-100"
                        >
                            {"✎"}
                        </button>
                    ) : null}
                    {device.connected ? (
                        lastSpoke && Date.now() - lastSpoke < 30000 ? (
                            <StatusPill tone="teal" pulse className="text-[11px]">
                                Speaking
                            </StatusPill>
                        ) : (
                            <StatusPill tone="teal" dot className="text-[11px]">
                                Online
                            </StatusPill>
                        )
                    ) : null}
                </span>
                )}
            </td>
            <td className="px-3 py-2 whitespace-nowrap">
                <div className="flex flex-wrap items-center gap-1">
                    <PodStatus device={device} enrich={enrich} queue={queue} />
                    <TriageBadges alerts={alerts} connected={device.connected} />
                </div>
            </td>
            <td className="px-3 py-2 text-right font-ahamono tabular-nums whitespace-nowrap text-tiilt-ink">
                {e.speaker_count > 0 ? (
                    e.speaker_count
                ) : (
                    <span className="text-tiilt-muted">—</span>
                )}
            </td>
            <td className="px-3 py-2 text-right font-ahamono tabular-nums whitespace-nowrap text-tiilt-ink">
                {dur || <span className="text-tiilt-muted">—</span>}
            </td>
            <td className="py-2 pr-3 pl-1 text-right whitespace-nowrap">
                {onDelete && (
                    <button
                        type="button"
                        aria-label={`Delete ${name}`}
                        title="Delete this pod"
                        onClick={(ev) => {
                            ev.stopPropagation()
                            onDelete()
                        }}
                        className="mr-1.5 cursor-pointer rounded px-1.5 py-0.5 text-tiilt-muted transition hover:bg-tiilt-danger-soft hover:text-tiilt-danger"
                    >
                        ✕
                    </button>
                )}
                <Chevron
                    size={12}
                    className={noData ? "text-transparent" : "text-tiilt-muted"}
                />
            </td>
        </tr>
    )
}

// At-a-glance roll-up for the whole session (pods, analyzed, people, time).
function SessionStats({ devices, enriched }) {
    const en = enriched || {}
    const list = devices || []
    const analyzed = list.filter(
        (d) => (en[d.id] && en[d.id].posthoc_analyzed_date) || d.posthoc_analyzed_date,
    ).length
    const participants = list.reduce(
        (a, d) => a + ((en[d.id] && en[d.id].speaker_count) || 0),
        0,
    )
    const totalDur = list.reduce(
        (a, d) => a + ((en[d.id] && en[d.id].duration) || 0),
        0,
    )
    const tiles = [
        ["Pods", String(list.length)],
        ["Analyzed", `${analyzed}/${list.length}`],
        ["Participants", String(participants)],
        ["Recorded", totalDur > 0 ? fmtDur(totalDur) : "—"],
    ]
    return (
        <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
            {tiles.map(([label, value]) => (
                <div
                    key={label}
                    className="rounded-xl border border-tiilt-line bg-white px-4 py-3"
                >
                    <div className="font-ahamono text-[11px] tracking-wider text-tiilt-muted uppercase">
                        {label}
                    </div>
                    <div className="mt-1 text-xl font-semibold text-tiilt-ink tabular-nums">
                        {value}
                    </div>
                </div>
            ))}
        </div>
    )
}

// Sticky status strip while recording: elapsed time, group count, code, End.
function RecordingBanner({ session, deviceCount, passcode, onShare, onEnd }) {
    const [, setTick] = useState(0)
    useEffect(() => {
        const t = setInterval(() => setTick((x) => x + 1), 1000)
        return () => clearInterval(t)
    }, [])
    const started = session.local_start_date || session.creation_date
    const secs = Math.max(0, Math.floor((Date.now() - new Date(started).getTime()) / 1000))
    const h = Math.floor(secs / 3600)
    const m = String(Math.floor((secs % 3600) / 60)).padStart(2, "0")
    const s = String(secs % 60).padStart(2, "0")
    return (
        <div className="flex w-full flex-none flex-wrap items-center gap-x-4 gap-y-1 border-b border-tiilt-line bg-tiilt-danger-soft/60 px-4 py-2 text-sm lg:px-8">
            <span className="flex items-center gap-1.5 font-semibold text-tiilt-danger">
                <span className="h-2 w-2 animate-pulse rounded-full bg-tiilt-danger" />
                Recording
            </span>
            <span className="font-ahamono tabular-nums text-tiilt-ink">
                {h > 0 ? `${h}:${m}:${s}` : `${m}:${s}`}
            </span>
            <span className="text-tiilt-muted">
                {deviceCount} {deviceCount === 1 ? "group" : "groups"}
            </span>
            {session.live_video_analytics === false ? (
                <span
                    title="Video is recorded but not analysed during class; the attention and emotion analysis is queued when the session ends."
                    className="rounded-md border border-tiilt-line bg-white px-1.5 py-0.5 text-xs text-tiilt-muted"
                >
                    video: recording only
                </span>
            ) : null}
            {passcode ? (
                <button
                    onClick={onShare}
                    title="Share join code"
                    className="cursor-pointer rounded-lg px-1.5 py-0.5 font-ahamono font-bold tracking-[0.2em] text-tiilt-ink transition hover:bg-white"
                >
                    {passcode}
                </button>
            ) : (
                <button
                    onClick={onShare}
                    className="cursor-pointer rounded-lg px-1.5 py-0.5 text-xs font-semibold text-tiilt-muted underline decoration-dotted underline-offset-2 transition hover:text-tiilt-ink"
                >
                    Locked — unlock to let more groups join
                </button>
            )}
            <button
                onClick={onEnd}
                className="ml-auto cursor-pointer rounded-lg border border-tiilt-danger/40 bg-white px-3 py-1 text-xs font-semibold text-tiilt-danger transition hover:border-tiilt-danger active:translate-y-px"
            >
                End session
            </button>
        </div>
    )
}

// Waiting room shown while the session is live but no group has joined yet:
// the share code/QR is the whole page instead of a hidden dialog.
function ProjectOverlay({ passcode, joinLink, onClose }) {
    useEffect(() => {
        const onKey = (e) => e.key === "Escape" && onClose()
        document.addEventListener("keydown", onKey)
        return () => document.removeEventListener("keydown", onKey)
    }, [onClose])
    return (
        /* role="dialog" also keeps the header's Escape-to-back handler from
           firing while the overlay is up (it skips Escape when a dialog is open). */
        <div
            role="dialog"
            aria-modal="true"
            aria-label="Projector view"
            className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-8 bg-white p-8"
        >
            <button
                onClick={onClose}
                aria-label="Close projector view"
                className="absolute top-4 right-5 cursor-pointer rounded-lg px-3 py-1.5 text-lg text-tiilt-muted transition hover:bg-tiilt-soft hover:text-tiilt-ink"
            >
                ✕
            </button>
            <a
                href={joinLink}
                target="_blank"
                rel="noreferrer"
                className="text-center font-ahamono text-2xl tracking-wide text-tiilt-muted transition hover:text-tiilt"
            >
                {joinLink.replace(/^https?:\/\//, "")}
            </a>
            <QrCode value={joinLink} size={340} />
            <div className="text-center">
                <div className="font-ahamono text-sm tracking-[0.3em] text-tiilt-muted uppercase">
                    Passcode
                </div>
                <div className="mt-2 font-ahamono text-7xl font-bold tracking-[0.35em] text-tiilt-ink">
                    {passcode}
                </div>
            </div>
        </div>
    )
}

function LobbyHero({ passcode, joinLink, copyJoinLink, onLock, deviceCount }) {
    const [projecting, setProjecting] = useState(false)
    // Same card before and after the first group joins — only the status
    // line changes, so the join info never jumps around mid-class.
    const joined = deviceCount > 0
    return (
        <div className="mx-auto flex max-w-xl flex-col items-center gap-5 rounded-2xl border border-tiilt-line bg-white px-6 py-10 text-center shadow-pop">
            <div className="flex items-center gap-2 text-sm font-semibold text-tiilt-muted">
                <span
                    className={
                        "h-2 w-2 rounded-full " +
                        (joined
                            ? "bg-tiilt-teal"
                            : "animate-pulse bg-tiilt-orange")
                    }
                />
                {joined
                    ? deviceCount +
                      (deviceCount === 1 ? " group has" : " groups have") +
                      " joined — the code stays open for more"
                    : "Waiting for groups to join…"}
            </div>
            <div className="rounded-xl bg-tiilt-soft/50 px-8 py-4">
                <div className="font-ahamono text-[11px] tracking-wider text-tiilt-muted uppercase">
                    Passcode
                </div>
                <div className="mt-1 font-ahamono text-4xl font-bold tracking-[0.3em] text-tiilt-ink">
                    {passcode}
                </div>
            </div>
            <QrCode value={joinLink} size={190} />
            <a
                href={joinLink}
                target="_blank"
                rel="noreferrer"
                className="font-ahamono text-sm break-all text-tiilt-muted underline decoration-tiilt-line underline-offset-2 transition hover:text-tiilt"
            >
                {joinLink}
            </a>
            <div className="flex flex-wrap justify-center gap-2">
                <button className={btnPrimary} onClick={copyJoinLink}>
                    Copy join link
                </button>
                <button className={btnSecondary} onClick={() => setProjecting(true)}>
                    ⛶ Project
                </button>
                <button className={btnSecondary} onClick={onLock}>
                    Lock joining
                </button>
            </div>
            <div className="text-xs text-tiilt-muted">
                Groups appear here the moment they connect.
            </div>
            {projecting ? (
                <ProjectOverlay
                    passcode={passcode}
                    joinLink={joinLink}
                    onClose={() => setProjecting(false)}
                />
            ) : null}
        </div>
    )
}

function PodsOverviewPages(props) {
    return (
        <>
            <div role="main" className="main-container">
                <Appheader
                    title={"Overview"}
                    leftText={false}
                    rightText={props.righttext}
                    rightPill={props.rightpill}
                    rightPillClick={
                        props.rightpill === "Locked"
                            ? () => props.openDialog("Passcode")
                            : undefined
                    }
                    rightTextClick={() => {
                        props.openDialog("Passcode")
                    }}
                    nav={props.navigateToSessions}
                    escToBack={true}
                />

                {props.session && props.session.recording ? (
                    <RecordingBanner
                        session={props.session}
                        deviceCount={(props.sessionDevices || []).length}
                        passcode={props.passcode}
                        onShare={() => props.openDialog("Passcode")}
                        onEnd={() => props.openDialog("ConfirmEnd")}
                    />
                ) : null}
                <SessionTabs />
                <div className="toolbar-view-container">
                    {props.session !== null ? (
                        <AppSessionToolbar
                            session={props.session}
                            closingSession={props.onSessionClosing}
                            menus={[
                                {
                                    title: "Download",
                                    action: () =>
                                        props.openDownloadOptionDialog(
                                            "selectdownloadoption",
                                        ),
                                },
                            ]}
                            participants={props.sessionSpeaker.map(
                                (speaker, index) => ({
                                    alias: speaker.alias,
                                    action: () =>
                                        props.goToSpeakerMetrics(speaker),
                                }),
                            )}
                        ></AppSessionToolbar>
                    ) : (
                        <></>
                    )}
                    <div className="min-h-0 grow overflow-y-auto">
                        <div className="mx-auto w-full max-w-6xl px-4 py-8 lg:px-8">
                            {/* Standardized join hero: identical card for a
                                live session whether zero or many pods have
                                joined; only its status line differs. */}
                            {props.sessionDevices !== null &&
                            props.initialized &&
                            props.session &&
                            !props.session.end_date ? (
                                props.passcode ? (
                                    <div className="mb-6">
                                        <LobbyHero
                                            passcode={props.passcode}
                                            joinLink={props.joinLink}
                                            copyJoinLink={props.copyJoinLink}
                                            onLock={() => props.setPasscodeState("lock")}
                                            deviceCount={props.sessionDevices.length}
                                        />
                                    </div>
                                ) : (
                                    <div className="mx-auto mb-6 flex max-w-xl flex-col items-center gap-4 rounded-2xl border border-tiilt-line bg-white px-6 py-10 text-center shadow-pop">
                                        <div className="text-base font-semibold text-tiilt-ink">
                                            Joining is locked
                                        </div>
                                        <div className="text-sm text-tiilt-muted">
                                            Unlock to get a fresh passcode and
                                            let groups join this session.
                                        </div>
                                        <button
                                            className={btnPrimary}
                                            onClick={() => props.setPasscodeState("unlock")}
                                        >
                                            Unlock joining
                                        </button>
                                    </div>
                                )
                            ) : (
                                <></>
                            )}
                            {props.sessionDevices !== null && props.initialized ? (
                                <SessionStats
                                    devices={props.sessionDevices}
                                    enriched={props.enriched}
                                />
                            ) : (
                                <SkeletonRows rows={6} className="mt-2" />
                            )}
                            <div className="mb-4 flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
                                <div>
                                    <h2 className="text-lg font-semibold text-tiilt-ink">
                                        Groups
                                        {props.sessionDevices !== null &&
                                        props.initialized ? (
                                            <span className="ml-1.5 font-normal text-tiilt-muted">
                                                ({props.sessionDevices.length})
                                            </span>
                                        ) : (
                                            <></>
                                        )}
                                    </h2>
                                    <div className="mt-0.5 text-xs text-tiilt-muted">
                                        Open a group to view its analytics
                                    </div>
                                </div>
                                {/* Post-hoc analysis is for ended recordings —
                                    while live, this screen is for monitoring. */}
                                {props.session && !props.session.recording ? (
                                <span className="flex flex-none items-center gap-3 text-sm text-tiilt-muted">
                                    {Object.values(props.queueState || {}).some((v) => v === "queued" || v === "running") ||
                                    Object.values(props.enriched || {}).some((e) => e && e.analysis_running) ? (
                                        <button
                                            onClick={props.stopRuns}
                                            className={btnDangerOutlineSm}
                                        >
                                            Stop runs
                                        </button>
                                    ) : null}
                                    {Object.values(props.selected || {}).filter(Boolean).length > 0 ? (
                                        <button
                                            onClick={props.runSelected}
                                            className={btnPrimarySm + " whitespace-nowrap"}
                                        >
                                            Run analysis ({Object.values(props.selected || {}).filter(Boolean).length})
                                        </button>
                                    ) : null}
                                </span>
                                ) : null}
                            </div>
                            {props.sessionDevices !== null &&
                            props.initialized &&
                            Object.keys(props.sessionDevices).length === 0 &&
                            props.session &&
                            props.session.end_date ? (
                                <div className="rounded-xl border border-dashed border-tiilt-line py-16 text-center">
                                    <div className="text-base font-semibold text-tiilt-ink">
                                        No groups in this session
                                    </div>
                                    <div className="mt-1 text-sm text-tiilt-muted">
                                        Participants and recording devices
                                        will appear here once they join.
                                    </div>
                                </div>
                            ) : (
                                <></>
                            )}
                            {props.sessionDevices !== null &&
                            props.initialized &&
                            props.sessionDevices.length > 0 ? (
                                <div className="overflow-x-auto rounded-xl border border-tiilt-line bg-white">
                                    <table className="w-full border-collapse text-left text-sm">
                                        <thead>
                                            <tr className="border-b border-tiilt-line">
                                                <th className="sticky top-0 bg-tiilt-ground py-2.5 pr-1 pl-3 text-center">
                                                    <input
                                                        type="checkbox"
                                                        title="Select all groups for batch analysis"
                                                        aria-label="Select all groups"
                                                        className="h-4 w-4 cursor-pointer accent-tiilt"
                                                        checked={
                                                            props.sessionDevices.length > 0 &&
                                                            props.sessionDevices.every(
                                                                (d) => props.selected && props.selected[d.id],
                                                            )
                                                        }
                                                        onChange={() =>
                                                            props.toggleSelectAll(
                                                                props.sessionDevices.map((d) => d.id),
                                                            )
                                                        }
                                                    />
                                                </th>
                                                <th className="sticky top-0 bg-tiilt-ground px-3 py-2.5">
                                                    <span className="sr-only">Type</span>
                                                </th>
                                                <th className="sticky top-0 bg-tiilt-ground px-3 py-2.5 text-left text-sm font-semibold whitespace-nowrap text-tiilt-ink">
                                                    Group
                                                </th>
                                                <th className="sticky top-0 bg-tiilt-ground px-3 py-2.5 text-left text-sm font-semibold whitespace-nowrap text-tiilt-ink">
                                                    Status
                                                </th>
                                                <th className="sticky top-0 bg-tiilt-ground px-3 py-2.5 text-right text-sm font-semibold whitespace-nowrap text-tiilt-ink">
                                                    People
                                                </th>
                                                <th className="sticky top-0 bg-tiilt-ground px-3 py-2.5 text-right text-sm font-semibold whitespace-nowrap text-tiilt-ink">
                                                    Recorded
                                                </th>
                                                <th className="sticky top-0 bg-tiilt-ground py-2.5 pr-3 pl-1" />
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {props.sessionDevices.map(
                                                (device, index) => (
                                                    <PodRow
                                                        key={device.id}
                                                        index={index}
                                                        lastSpoke={props.lastActivity && props.lastActivity[device.id]}
                                                        device={device}
                                                        enrich={props.enriched && props.enriched[device.id]}
                                                        alerts={props.triage && props.triage[device.id]}
                                                        checked={props.selected && props.selected[device.id]}
                                                        onToggle={() => props.toggleSelect(device.id)}
                                                        queue={props.queueState && props.queueState[device.id]}
                                                        renameInline={props.renamePodInline}
                                                        onDelete={
                                                            props.canDeletePod &&
                                                            props.canDeletePod(device)
                                                                ? () => props.requestDeletePod(device)
                                                                : null
                                                        }
                                                        onOpen={() =>
                                                            props.goToDevice(device)
                                                        }
                                                    />
                                                ),
                                            )}
                                        </tbody>
                                    </table>
                                </div>
                            ) : (
                                <></>
                            )}
                        </div>
                    </div>
                </div>
            </div>

            <ToastStack toasts={props.toasts} dismiss={props.dismissToast} />

            <GenericDialogBox
                onClose={props.cancelDeletePod}
                show={!!props.podToDelete}
            >
                <div className={dlgBody}>
                    <div className={dlgHeading}>Delete pod</div>
                    <div className="text-sm text-tiilt-ink">
                        Delete{" "}
                        <span className="font-semibold">
                            {(props.podToDelete &&
                                props.podToDelete.name &&
                                String(props.podToDelete.name).trim()) ||
                                "this pod"}
                        </span>
                        ? The pod and anything it recorded are removed from
                        the session permanently.
                    </div>
                    <button className={dlgDanger} onClick={props.confirmDeletePod}>
                        Delete pod
                    </button>
                    <button className={dlgCancel} onClick={props.cancelDeletePod}>
                        Cancel
                    </button>
                </div>
            </GenericDialogBox>

            <GenericDialogBox onClose={props.closeDialog} show={props.currentForm !== ""}>
                {(props.currentForm === "AddDevice" && (
                    <div className={dlgBody}>
                        <div className={dlgHeading}>Add pod to Session</div>
                        {props.devices.length > 0 ? (
                            <React.Fragment>
                                <select id="ddDevice" aria-label="Pod to add" className={dlgSelect}>
                                    {props.devices.map((device, index) => (
                                        <option
                                            key={index}
                                            defaultValue={device.id}
                                        >
                                            {device.name}
                                        </option>
                                    ))}
                                </select>
                                <button
                                    className={dlgPrimary}
                                    onClick={() =>
                                        props.addPodToSession(
                                            document.getElementById("ddDevice")
                                                .value,
                                        )
                                    }
                                >
                                    Add
                                </button>
                            </React.Fragment>
                        ) : (
                            <></>
                        )}
                        {props.devices.length === 0 ? (
                            <div className="py-3 text-sm text-tiilt-muted">
                                No devices available.
                            </div>
                        ) : (
                            <></>
                        )}
                        <button
                            className={dlgCancel}
                            onClick={props.closeDialog}
                        >
                            Close
                        </button>
                    </div>
                )) ||
                    (props.currentForm === "selectdownloadoption" && (
                        <div className={dlgBody}>
                            <div className={dlgHeading}>
                                Select the Data to Download:
                            </div>
                            <select id="format" aria-label="Download format" className={dlgSelect}>
                                <option value="">Select format</option>
                                <option value="csv">CSV</option>
                                <option value="json">JSON</option>
                            </select>
                            <select id="windowsize" aria-label="Window size in seconds" className={dlgSelect}>
                                <option value="">
                                    Select window size in secs
                                </option>
                                <option value="0">0</option>
                                <option value="10">10</option>
                                <option value="20">20</option>
                                <option value="30">30</option>
                                <option value="40">40</option>
                                <option value="50">50</option>
                                <option value="60">60</option>
                            </select>
                            <select id="datatype" aria-label="Data to download" className={dlgSelect}>
                                <option value="">
                                    Select data to download
                                </option>
                                <option value="audiometrics">
                                    Transcript Metrics
                                </option>
                                <option value="videometrics">
                                    Video Metrics
                                </option>
                                <option value="transcriptvideometrics">
                                    Transcript-Video Metrics
                                </option>
                            </select>

                            <button
                                className={dlgPrimary}
                                onClick={() => {
                                    props.downloadData(
                                        document.getElementById("windowsize")
                                            .value,
                                        document.getElementById("datatype")
                                            .value,
                                        document.getElementById("format").value,
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
                    )) ||
                    (props.currentForm === "ConfirmEnd" && (
                        <div className={dlgBody}>
                            <div className={dlgHeading}>End this session?</div>
                            <div className="text-sm text-tiilt-muted">
                                Recording stops for every group and the
                                passcode is retired. This can't be undone.
                            </div>
                            <button
                                className={dlgDanger}
                                onClick={props.endThisSession}
                            >
                                End session
                            </button>
                            <button
                                className={dlgCancel}
                                onClick={props.closeDialog}
                            >
                                Cancel
                            </button>
                        </div>
                    )) ||
                    (props.currentForm === "Passcode" && (
                        // Mirrors the join hero exactly: passcode, QR,
                        // clickable link, and the same simple actions —
                        // not the old seven-button pile.
                        <div className={dlgBody}>
                            <div className={dlgHeading}>Share this session</div>
                            {props.passcode ? (
                                <>
                                    <div className="rounded-xl border border-tiilt-line bg-tiilt-soft/50 px-4 py-3 text-center">
                                        <div className="font-ahamono text-[11px] tracking-wider text-tiilt-muted uppercase">
                                            Passcode
                                        </div>
                                        <div className="mt-1 font-ahamono text-3xl font-bold tracking-[0.3em] text-tiilt-ink">
                                            {props.passcode}
                                        </div>
                                    </div>
                                    <QrCode value={props.joinLink} />
                                    <a
                                        href={props.joinLink}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="text-center font-ahamono text-sm break-all text-tiilt-muted underline decoration-tiilt-line underline-offset-2 transition hover:text-tiilt"
                                    >
                                        {props.joinLink}
                                    </a>
                                    <button
                                        className={dlgPrimary}
                                        onClick={() => props.copyJoinLink()}
                                    >
                                        Copy join link
                                    </button>
                                    <div className="flex gap-2">
                                        <button
                                            className={btnSecondary + " flex-1"}
                                            onClick={() => props.setPasscodeState("lock")}
                                        >
                                            Lock joining
                                        </button>
                                        <button
                                            className={btnSecondary + " flex-1"}
                                            onClick={() => props.setPasscodeState("refresh")}
                                        >
                                            Get a new code
                                        </button>
                                    </div>
                                </>
                            ) : (
                                <>
                                    <div className="text-sm text-tiilt-muted">
                                        Joining is locked. Unlock to get a
                                        fresh passcode and let groups join
                                        this session.
                                    </div>
                                    <button
                                        className={dlgPrimary}
                                        onClick={() => props.setPasscodeState("unlock")}
                                    >
                                        Unlock joining
                                    </button>
                                </>
                            )}
                            <button
                                className={dlgCancel}
                                onClick={props.closeDialog}
                            >
                                Close
                            </button>
                        </div>
                    ))}
            </GenericDialogBox>
        </>
    )
}

export { PodsOverviewPages }
