import React, { useEffect, useRef, useState } from "react"
import { AppSectionBoxComponent } from "../section-box/section-box-component"
import { StatusPill } from "../status-pill"
import { InlineSpinner } from "../inline-spinner"
import { btnPrimarySm, btnSecondarySm, dlgError } from "../dialog-styles"
import { NegotiationCodingService } from "../../services/negotiation-coding-service"
import {
    TEAMS,
    UNASSIGNED,
    DIMENSIONS,
    TIMELINE_FIELDS,
    fmtClock,
    describeRun,
    isActiveStatus,
    speakerTags,
    assignedTeams,
    sameTeams,
    teamOf,
    rollupTable,
    badgeCodes,
    filterKey,
    filterCodes,
    startRunPolling,
} from "./negotiation-coding-helpers"

// Negotiation coding for the Kellogg "Viking" case (3-on-3, Pat vs Sandy).
// A local LLM codes each utterance on emotion, interests/rights/power,
// frame and listening moves; this panel runs it, assigns speakers to teams,
// and shows the per-team roll-up, the de-escalation timeline and the coded
// utterances. Hidden unless the session opted in or a run already exists,
// so other classes never see it.

const POLL_MS = 5000

// Pill tone per code (listening moves stay neutral).
const BADGE_TONE = {
    "emotion:escalating": "danger",
    "emotion:defusing": "teal",
    "rip:power": "orange",
    "rip:right": "brand",
    "rip:interest": "teal",
    "frame:past_blame": "orange",
    "frame:future_problem_solving": "teal",
}
const TEAM_TONE = { Pat: "brand", Sandy: "teal", [UNASSIGNED]: "neutral" }

const selectCls =
    "app-select w-full cursor-pointer rounded-lg border border-tiilt-line bg-white py-1.5 pr-8 pl-3 text-sm text-tiilt-ink transition outline-none focus-visible:border-tiilt focus-visible:ring-[3px] focus-visible:ring-tiilt/30 disabled:cursor-default disabled:bg-tiilt-ground disabled:text-tiilt-muted"

const sectionTitle = "font-ahamono text-[11px] tracking-wider text-tiilt-muted uppercase"

function NegotiationCodingPanel({ session, sessionDeviceId, tagCounts }) {
    const sessionId = session ? session.id : null
    const optedIn = !!(session && session.negotiation_coding)
    const [svc] = useState(() => new NegotiationCodingService())
    // Latest GET/PUT body; exists: null = unknown yet, false = 404 (never run).
    const [data, setData] = useState(null)
    const [exists, setExists] = useState(null)
    const [loadError, setLoadError] = useState(null)
    const [busy, setBusy] = useState(null) // "run" | "save"
    const [actionError, setActionError] = useState(null)
    const [teamsDraft, setTeamsDraft] = useState({})
    const [filters, setFilters] = useState(() => new Set())
    const [now, setNow] = useState(() => Date.now())
    // Teams as last seen from the server, so a 5 s poll never clobbers an
    // edit in progress: the draft resets only when the server's teams change.
    const serverTeams = useRef(null)

    const apply = (body) => {
        setData(body)
        setExists(true)
        const teams = (body && body.teams) || {}
        if (serverTeams.current == null || !sameTeams(serverTeams.current, teams)) {
            serverTeams.current = teams
            setTeamsDraft(teams)
        }
    }

    useEffect(() => {
        if (sessionId == null || sessionDeviceId == null) return undefined
        let cancelled = false
        // Switching pods: drop the previous pod's coding before fetching.
        setData(null)
        setExists(null)
        setLoadError(null)
        setActionError(null)
        setTeamsDraft({})
        setFilters(new Set())
        serverTeams.current = null
        svc.get(sessionId, sessionDeviceId)
            .then(({ status, body }) => {
                if (cancelled) return
                if (status === 200 && body) apply(body)
                else if (status === 404) {
                    setExists(false)
                    setData(null)
                } else setLoadError(`Could not load the coding (HTTP ${status}).`)
            })
            .catch(() => {
                if (!cancelled) setLoadError("Could not load the coding (network error).")
            })
        return () => {
            cancelled = true
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [sessionId, sessionDeviceId])

    const run = data && data.run
    const runStatus = run ? run.status : null
    const active = isActiveStatus(runStatus)

    // Poll every 5 s while queued/running; the cleanup stops it on unmount
    // and the callback stops it once the run settles.
    useEffect(() => {
        if (!active) return undefined
        return startRunPolling({
            intervalMs: POLL_MS,
            poll: () => svc.get(sessionId, sessionDeviceId),
            onResult: ({ status, body }) => {
                if (status === 200 && body) {
                    apply(body)
                    return isActiveStatus(body.run && body.run.status)
                }
                if (status === 404) {
                    setExists(false)
                    setData(null)
                    return false
                }
                return true
            },
        })
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [active, sessionId, sessionDeviceId])

    // Tick the elapsed time while running.
    useEffect(() => {
        if (runStatus !== "running") return undefined
        setNow(Date.now())
        const id = setInterval(() => setNow(Date.now()), 1000)
        return () => clearInterval(id)
    }, [runStatus])

    if (!optedIn && exists !== true) return null

    const codes = (data && data.codes) || []
    const teams = (data && data.teams) || {}
    const tags = speakerTags(tagCounts, codes, { ...teams, ...teamsDraft })
    const teamsDirty = !sameTeams(teamsDraft, teams)
    const status = describeRun(run, now)

    const runCoding = () => {
        setBusy("run")
        setActionError(null)
        const chosen = assignedTeams(teamsDraft)
        svc.run(sessionId, sessionDeviceId, chosen)
            .then(({ status: st, body }) => {
                if (st === 202 || st === 200) {
                    setExists(true)
                    serverTeams.current = chosen
                    setData((d) => ({
                        ...(d || {}),
                        teams: chosen,
                        run: {
                            id: body && body.run_id,
                            status: (body && body.status) || "queued",
                            started_at: null,
                            finished_at: null,
                            error: null,
                        },
                    }))
                } else {
                    const why = body && (body.error || body.message)
                    setActionError(`Run refused (HTTP ${st})${why ? ": " + why : ""}.`)
                }
            })
            .catch(() => setActionError("Run request failed (network error)."))
            .finally(() => setBusy(null))
    }

    const saveTeams = () => {
        setBusy("save")
        setActionError(null)
        svc.setTeams(sessionId, sessionDeviceId, assignedTeams(teamsDraft))
            .then(({ status: st, body }) => {
                if (st === 200 && body) {
                    serverTeams.current = null // force the draft to follow the server
                    apply(body)
                } else {
                    const why = body && (body.error || body.message)
                    setActionError(`Saving teams failed (HTTP ${st})${why ? ": " + why : ""}.`)
                }
            })
            .catch(() => setActionError("Saving teams failed (network error)."))
            .finally(() => setBusy(null))
    }

    const toggleFilter = (key) =>
        setFilters((prev) => {
            const next = new Set(prev)
            if (next.has(key)) next.delete(key)
            else next.add(key)
            return next
        })

    const table = rollupTable(data && data.summary)
    const timeline = (data && data.summary && data.summary.timeline) || {}
    const shown = filterCodes(codes, filters)
    const hasResults = runStatus === "done" && codes.length > 0

    return (
        <AppSectionBoxComponent
            type={"w-full"}
            heading={"Negotiation coding"}
            badge={"Viking case · LLM-coded"}
            badgeTone={"amber"}
        >
            <div className="flex w-full flex-col gap-4 text-sm">
                {/* Status + actions */}
                <div className="flex flex-wrap items-center gap-2">
                    <StatusPill tone={status.tone} pulse={active} className="text-xs">
                        {status.text}
                    </StatusPill>
                    {run && run.status === "done" ? (
                        <span className="text-xs text-tiilt-muted">
                            {typeof run.utterances_coded === "number"
                                ? `${run.utterances_coded} utterances coded`
                                : null}
                            {typeof run.invalid_codes === "number" && run.invalid_codes > 0
                                ? ` · ${run.invalid_codes} invalid codes dropped`
                                : null}
                            {run.model ? ` · ${run.model}` : null}
                            {run.codebook_version ? ` · codebook ${run.codebook_version}` : null}
                        </span>
                    ) : null}
                    <span className="grow" />
                    {hasResults ? (
                        <a
                            href={svc.exportCsvUrl(sessionId, sessionDeviceId)}
                            download
                            className={btnSecondarySm}
                        >
                            Export CSV
                        </a>
                    ) : null}
                    <button
                        type="button"
                        onClick={runCoding}
                        disabled={active || busy != null}
                        className={btnPrimarySm + " inline-flex items-center gap-2 disabled:opacity-50"}
                    >
                        {busy === "run" ? <InlineSpinner /> : null}
                        {run ? "Re-run coding" : "Run coding"}
                    </button>
                </div>
                {loadError ? <div className={dlgError}>{loadError}</div> : null}
                {actionError ? <div className={dlgError}>{actionError}</div> : null}
                {run && run.status === "error" ? (
                    <div className={dlgError}>
                        The last run failed: {run.error || "unknown error"}. Fix the cause and
                        re-run.
                    </div>
                ) : null}
                {exists === null && !loadError ? (
                    <div className="text-xs text-tiilt-muted">Loading…</div>
                ) : null}
                {exists === false ? (
                    <div className="rounded-lg bg-tiilt-ground/50 px-3 py-2 text-xs text-tiilt-muted">
                        No coding yet. Assign the speakers to their teams below, then run coding to
                        have every utterance coded for emotion, interests/rights/power, frame and
                        listening moves.
                    </div>
                ) : null}
                {active ? (
                    <div className="rounded-lg bg-tiilt-ground/50 px-3 py-2 text-xs text-tiilt-muted">
                        Coding runs on the local model and usually takes a few minutes; this panel
                        updates itself every {POLL_MS / 1000} s.
                    </div>
                ) : null}

                {/* Team assignment */}
                <div className="flex flex-col gap-2">
                    <div className={sectionTitle}>Teams</div>
                    {tags.length === 0 ? (
                        <div className="text-xs text-tiilt-muted">
                            No speakers in the transcript yet.
                        </div>
                    ) : (
                        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                            {tags.map((tag) => (
                                <label key={tag} className="flex items-center gap-2 text-xs">
                                    <span className="w-28 flex-none truncate font-semibold text-tiilt-ink" title={tag}>
                                        {tag}
                                    </span>
                                    <select
                                        value={TEAMS.includes(teamsDraft[tag]) ? teamsDraft[tag] : UNASSIGNED}
                                        onChange={(e) =>
                                            setTeamsDraft((d) => ({ ...d, [tag]: e.target.value }))
                                        }
                                        disabled={busy != null}
                                        className={selectCls}
                                    >
                                        <option value={UNASSIGNED}>Unassigned</option>
                                        {TEAMS.map((t) => (
                                            <option key={t} value={t}>
                                                {t}
                                            </option>
                                        ))}
                                    </select>
                                </label>
                            ))}
                        </div>
                    )}
                    {exists ? (
                        <div className="flex items-center gap-2">
                            <button
                                type="button"
                                onClick={saveTeams}
                                disabled={!teamsDirty || busy != null}
                                className={btnSecondarySm + " inline-flex items-center gap-2 disabled:opacity-50"}
                            >
                                {busy === "save" ? <InlineSpinner /> : null}
                                Save teams
                            </button>
                            <span className="text-xs text-tiilt-muted">
                                {teamsDirty
                                    ? "Unsaved changes — saving recomputes the roll-up."
                                    : "Speakers not assigned count as unassigned."}
                            </span>
                        </div>
                    ) : (
                        <div className="text-xs text-tiilt-muted">
                            Assignments are sent with the run and can be changed afterwards.
                        </div>
                    )}
                </div>

                {runStatus === "done" && codes.length === 0 ? (
                    <div className="rounded-lg bg-tiilt-ground/50 px-3 py-2 text-xs text-tiilt-muted">
                        The run finished but coded no utterances — the pod may have an empty
                        transcript.
                    </div>
                ) : null}

                {hasResults ? (
                    <>
                        {/* Roll-up */}
                        <div className="flex flex-col gap-2">
                            <div className={sectionTitle}>Roll-up by team</div>
                            <div className="overflow-x-auto rounded-lg border border-tiilt-line">
                                <table className="w-full text-xs">
                                    <thead>
                                        <tr className="bg-tiilt-ground/60 text-tiilt-muted">
                                            <th className="px-3 py-1.5 text-left font-semibold">Code</th>
                                            {table.columns.map((c) => (
                                                <th key={c.key} className="px-3 py-1.5 text-right font-semibold">
                                                    <span className="text-tiilt-ink">{c.label}</span>
                                                    <span className="block font-normal">
                                                        {c.utterances} utt.
                                                    </span>
                                                </th>
                                            ))}
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {table.sections.map((s) => (
                                            <React.Fragment key={s.key}>
                                                <tr>
                                                    <td
                                                        colSpan={1 + table.columns.length}
                                                        className={"border-t border-tiilt-line bg-tiilt-ground/30 px-3 py-1 " + sectionTitle}
                                                    >
                                                        {s.label}
                                                    </td>
                                                </tr>
                                                {s.rows.map((r) => (
                                                    <tr key={r.code} className="border-t border-tiilt-line/60">
                                                        <td className="px-3 py-1 text-tiilt-ink">{r.label}</td>
                                                        {r.cells.map((cell, i) => (
                                                            <td
                                                                key={table.columns[i].key}
                                                                className="px-3 py-1 text-right tabular-nums text-tiilt-ink"
                                                            >
                                                                {cell.n}
                                                                {cell.pct != null ? (
                                                                    <span className="ml-1 text-tiilt-muted">
                                                                        {cell.pct}%
                                                                    </span>
                                                                ) : null}
                                                            </td>
                                                        ))}
                                                    </tr>
                                                ))}
                                            </React.Fragment>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                            <div className="text-xs text-tiilt-muted">
                                Percentages are of each team&apos;s own utterances; listening moves can
                                stack on one utterance.
                            </div>
                        </div>

                        {/* Timeline */}
                        <div className="flex flex-col gap-2">
                            <div className={sectionTitle}>De-escalation timeline</div>
                            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                                {TIMELINE_FIELDS.map((f) => (
                                    <div key={f.key} className="rounded-lg border border-tiilt-line bg-white px-3 py-2">
                                        <div className="text-xs font-semibold text-tiilt-ink">{f.label}</div>
                                        <div className="font-ahamono text-lg tabular-nums text-tiilt-ink">
                                            {fmtClock(timeline[f.key])}
                                        </div>
                                        <div className="text-[11px] leading-snug text-tiilt-muted">
                                            {timeline[f.key] == null ? "Not observed. " : ""}
                                            {f.desc}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>

                        {/* Utterances */}
                        <div className="flex flex-col gap-2">
                            <div className="flex flex-wrap items-baseline gap-2">
                                <div className={sectionTitle}>Utterances</div>
                                <span className="text-xs text-tiilt-muted">
                                    {shown.length} of {codes.length} shown
                                </span>
                                {filters.size > 0 ? (
                                    <button
                                        type="button"
                                        onClick={() => setFilters(new Set())}
                                        className="text-xs text-tiilt hover:underline"
                                    >
                                        Clear filters
                                    </button>
                                ) : null}
                            </div>
                            <div className="flex flex-col gap-1.5">
                                {DIMENSIONS.map((d) => (
                                    <div key={d.key} className="flex flex-wrap items-center gap-1">
                                        <span className="w-20 flex-none text-[11px] text-tiilt-muted">{d.label}</span>
                                        {d.codes.map((c) => {
                                            const key = filterKey(d.key, c.code)
                                            const on = filters.has(key)
                                            return (
                                                <button
                                                    key={key}
                                                    type="button"
                                                    onClick={() => toggleFilter(key)}
                                                    aria-pressed={on}
                                                    className={
                                                        "rounded-full border px-2 py-0.5 text-[11px] transition " +
                                                        (on
                                                            ? "border-tiilt bg-tiilt-soft font-semibold text-tiilt"
                                                            : "border-tiilt-line bg-white text-tiilt-muted hover:border-tiilt hover:text-tiilt-ink")
                                                    }
                                                >
                                                    {c.label}
                                                </button>
                                            )
                                        })}
                                    </div>
                                ))}
                            </div>
                            {shown.length === 0 ? (
                                <div className="rounded-lg bg-tiilt-ground/50 px-3 py-2 text-xs text-tiilt-muted">
                                    No utterance carries every selected code.
                                </div>
                            ) : (
                                <ol className="flex max-h-96 flex-col gap-1 overflow-y-auto rounded-lg border border-tiilt-line p-2">
                                    {shown.map((row) => {
                                        const team = teamOf(row, teams)
                                        return (
                                            <li
                                                key={row.transcript_id != null ? row.transcript_id : `${row.start_time}-${row.speaker_tag}`}
                                                className="flex flex-col gap-1 rounded-md px-2 py-1.5 hover:bg-tiilt-ground/50"
                                            >
                                                <div className="flex flex-wrap items-center gap-2 text-xs">
                                                    <span className="font-ahamono tabular-nums text-tiilt-muted">
                                                        {fmtClock(row.start_time)}
                                                    </span>
                                                    <span className="font-semibold text-tiilt-ink">
                                                        {row.speaker_tag || "Unknown"}
                                                    </span>
                                                    <StatusPill tone={TEAM_TONE[team]} className="text-[10px]">
                                                        {team === UNASSIGNED ? "Unassigned" : team}
                                                    </StatusPill>
                                                    <span className="flex flex-wrap gap-1">
                                                        {badgeCodes(row).map((c) => (
                                                            <StatusPill
                                                                key={`${c.dim}:${c.code}`}
                                                                tone={BADGE_TONE[`${c.dim}:${c.code}`] || "neutral"}
                                                                className="text-[10px]"
                                                                title={d3Label(c.dim)}
                                                            >
                                                                {c.label}
                                                            </StatusPill>
                                                        ))}
                                                    </span>
                                                </div>
                                                <div className="text-sm text-tiilt-ink">{row.text}</div>
                                            </li>
                                        )
                                    })}
                                </ol>
                            )}
                        </div>
                    </>
                ) : null}
            </div>
        </AppSectionBoxComponent>
    )
}

// Tooltip on a code badge: which dimension it belongs to.
function d3Label(dim) {
    const d = DIMENSIONS.find((x) => x.key === dim)
    return d ? d.label : dim
}

export { NegotiationCodingPanel }
