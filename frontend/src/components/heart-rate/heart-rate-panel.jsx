import { useIsDark } from "../../myhooks/custom-hooks"
import { applyChartTheme } from "../chart-theme"
import { Line } from "react-chartjs-2"
import { speakerColorFor, formatHMS } from "../../globals"
import "chart.js/auto"

// Heart rate & HRV from Polar straps assigned on the join page. Rows are
// speaker_hr_metrics.json(): one per strap notification (~1/s) with bpm plus
// the notification's beat-to-beat RR intervals ("512,498" ms). HR is charted
// directly; HRV is RMSSD over the RR stream in 60-second windows — the
// standard short-window vagal-tone measure (higher = calmer/recovered,
// dips = load or stress).
//
// Rows since the clock sync carry time_ms (the notification, ms on the
// session clock) and beat_ms (when its last beat ended, rebuilt from the
// strap's own RR sequence); older rows have whole-second time_stamp only.

// Physiologically plausible beat-to-beat intervals only (30-200 bpm);
// anything outside is strap noise/dropout, and one bad beat would dominate
// a squared-difference metric like RMSSD.
const RR_MIN = 300
const RR_MAX = 2000
const HRV_WINDOW_S = 60
const HR_SMOOTH_S = 5

// One series per person, keyed by speaker_id: rows carry the alias current
// at sample time, so a mid-session rename ("Speaker 1" → its real username)
// used to split one strap's stream into two lines that read as two people.
// Each series is labeled with the roster's current name for that id. Rows
// whose speaker_id isn't in this pod's roster are stray flushes from an
// earlier session in the same browser tab — dropped, not phantom people.
function bySpeaker(rows, speakers) {
    const roster = new Map((speakers || []).map((s) => [s.id, s.alias]))
    const byKey = new Map()
    for (const r of rows || []) {
        if (r.speaker_id != null && roster.size > 0 && !roster.has(r.speaker_id)) continue
        const key = r.speaker_id != null ? "id:" + r.speaker_id : "alias:" + (r.speaker_alias || "Unknown")
        if (!byKey.has(key)) byKey.set(key, [])
        byKey.get(key).push(r)
    }
    const map = new Map()
    for (const list of byKey.values()) {
        list.sort((a, b) => a.time_stamp - b.time_stamp)
        const last = list[list.length - 1]
        const alias =
            (last.speaker_id != null && roster.get(last.speaker_id)) ||
            last.speaker_alias ||
            "Unknown"
        if (!map.has(alias)) map.set(alias, [])
        map.get(alias).push(...list)
    }
    for (const list of map.values()) list.sort((a, b) => a.time_stamp - b.time_stamp)
    return map
}

// Session seconds of a row: ms precision where the row has it.
const rowSeconds = (r) => (r.time_ms != null ? r.time_ms / 1000 : r.time_stamp)

function smoothHr(rows) {
    const out = []
    let bucket = null
    for (const r of rows) {
        const t = rowSeconds(r)
        const b = Math.floor(t / HR_SMOOTH_S)
        if (!bucket || bucket.b !== b) {
            if (bucket) out.push({ x: bucket.t / bucket.n, y: bucket.hr / bucket.n })
            bucket = { b, t: 0, hr: 0, n: 0 }
        }
        bucket.t += t
        bucket.hr += r.heart_rate
        bucket.n += 1
    }
    if (bucket) out.push({ x: bucket.t / bucket.n, y: bucket.hr / bucket.n })
    return out
}

function rrSequence(rows) {
    const seq = []
    for (const r of rows) {
        if (!r.rr_ms) continue
        if (r.beat_ms != null) {
            // The last interval ended at beat_ms; each earlier one ended an
            // interval before the next.
            const parts = String(r.rr_ms).split(",").map((p) => parseInt(p, 10))
            const beats = []
            let end = r.beat_ms / 1000
            for (let i = parts.length - 1; i >= 0; i--) {
                const v = parts[i]
                if (v >= RR_MIN && v <= RR_MAX) beats.push({ t: end, rr: v })
                if (Number.isFinite(v)) end -= v / 1000
            }
            seq.push(...beats.reverse())
            continue
        }
        // A notification can carry several beats; space them out by their
        // own intervals instead of stacking them on one timestamp, which
        // drew vertical zigzags on the tachogram.
        let off = 0
        for (const part of String(r.rr_ms).split(",")) {
            const v = parseInt(part, 10)
            if (v >= RR_MIN && v <= RR_MAX) {
                seq.push({ t: r.time_stamp + off, rr: v })
                off += v / 1000
            }
        }
    }
    return seq
}

// RMSSD per fixed window: sqrt(mean(successive-difference²)) of the RRs
// whose window it is. Needs a handful of beats to be meaningful.
function rollingRmssd(seq) {
    const buckets = new Map()
    for (let i = 1; i < seq.length; i++) {
        // Only difference beats that are actually adjacent (same or
        // neighboring notification) — a gap means dropped beats.
        if (seq[i].t - seq[i - 1].t > 5) continue
        const d = seq[i].rr - seq[i - 1].rr
        const w = Math.floor(seq[i].t / HRV_WINDOW_S)
        if (!buckets.has(w)) buckets.set(w, { sum: 0, n: 0 })
        const b = buckets.get(w)
        b.sum += d * d
        b.n += 1
    }
    const points = []
    for (const [w, b] of [...buckets.entries()].sort((p, q) => p[0] - q[0])) {
        if (b.n < 10) continue
        points.push({ x: w * HRV_WINDOW_S + HRV_WINDOW_S / 2, y: Math.sqrt(b.sum / b.n) })
    }
    return points
}

function summarize(rows, seq) {
    const hrs = rows.map((r) => r.heart_rate)
    const mean = (a) => a.reduce((s, v) => s + v, 0) / a.length
    const diffs = []
    for (let i = 1; i < seq.length; i++) {
        if (seq[i].t - seq[i - 1].t <= 5) diffs.push(seq[i].rr - seq[i - 1].rr)
    }
    const rrs = seq.map((p) => p.rr)
    const sdnn = rrs.length > 1
        ? Math.sqrt(mean(rrs.map((v) => (v - mean(rrs)) ** 2)))
        : null
    return {
        avgHr: Math.round(mean(hrs)),
        minHr: Math.min(...hrs),
        maxHr: Math.max(...hrs),
        rmssd: diffs.length >= 10 ? Math.round(Math.sqrt(mean(diffs.map((d) => d * d)))) : null,
        sdnn: sdnn != null && rrs.length >= 10 ? Math.round(sdnn) : null,
        sensor: (rows.find((r) => r.sensor_name) || {}).sensor_name || "",
    }
}

const chartOptions = (yTitle) => ({
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
        legend: { display: false },
        tooltip: {
            callbacks: {
                title: (items) => formatHMS(items[0].parsed.x),
                label: (ctx) => `${ctx.dataset.label}: ${Math.round(ctx.parsed.y)}`,
            },
        },
    },
    interaction: { mode: "nearest", intersect: false },
    scales: {
        x: { type: "linear", display: false },
        y: {
            grid: { color: "rgba(58,33,99,0.08)" },
            title: { display: true, text: yTitle },
        },
    },
})

function ChartCard({ title, note, datasets, yTitle, dark, className = "" }) {
    return (
        <div className={"rounded-lg border border-tiilt-line bg-white p-3 " + className} title={note}>
            <div className="truncate text-sm font-semibold text-tiilt-ink">{title}</div>
            <div className="mt-2 h-36">
                {datasets.length === 0 ? (
                    <div className="flex h-full items-center justify-center rounded-lg bg-tiilt-ground/60 text-xs text-tiilt-muted">
                        Not enough beat data
                    </div>
                ) : (
                    <Line key={dark ? "d" : "l"} data={{ datasets }} options={chartOptions(yTitle)} />
                )}
            </div>
        </div>
    )
}

function HeartRatePanel({ rows, speakers }) {
    applyChartTheme()
    const dark = useIsDark()
    const grouped = bySpeaker(rows, speakers)
    if (grouped.size === 0)
        return (
            <div className="py-6 text-center text-sm text-tiilt-muted">
                No heart-rate straps streamed data for this pod.
            </div>
        )

    // Color space is the pod roster plus any strap-only aliases, so colors
    // line up with the other panels.
    const allAliases = [
        ...new Set([...(speakers || []).map((s) => s.alias), ...grouped.keys()]),
    ]
    const colorOf = (alias) => speakerColorFor(alias, allAliases)

    const series = [...grouped.entries()].map(([alias, list]) => {
        const seq = rrSequence(list)
        return {
            alias,
            hr: smoothHr(list),
            hrv: rollingRmssd(seq),
            // Raw tachogram: every plausible beat interval, unsmoothed — the
            // beat-to-beat jitter is exactly what HRV summarizes, so
            // averaging it away here would defeat the chart.
            rr: seq.map((p) => ({ x: p.t, y: p.rr })),
            stats: summarize(list, seq),
        }
    })

    const line = (points, alias) => ({
        label: alias,
        data: points,
        borderColor: colorOf(alias),
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        fill: false,
    })

    return (
        <div className="flex w-full flex-col gap-3">
            <div className="flex flex-wrap gap-x-3 gap-y-1">
                {series.map((s) => (
                    <span key={s.alias} className="flex items-center gap-1.5 text-xs text-tiilt-ink">
                        <span
                            className="h-2.5 w-2.5 flex-none rounded-full"
                            style={{ backgroundColor: colorOf(s.alias) }}
                        />
                        {s.alias}
                        {s.stats.sensor && (
                            <span className="text-tiilt-muted">({s.stats.sensor})</span>
                        )}
                    </span>
                ))}
            </div>
            <div className="grid w-full grid-cols-1 gap-3 sm:grid-cols-2">
                <ChartCard
                    title="Heart rate"
                    note="Beats per minute, 5-second averages."
                    yTitle="bpm"
                    dark={dark}
                    datasets={series.filter((s) => s.hr.length > 0).map((s) => line(s.hr, s.alias))}
                />
                <ChartCard
                    title="HRV — RMSSD"
                    note="Root-mean-square of successive beat-interval differences per 60-second window. Higher generally reads as calmer; dips as load or stress."
                    yTitle="ms"
                    dark={dark}
                    datasets={series.filter((s) => s.hrv.length > 0).map((s) => line(s.hrv, s.alias))}
                />
                <ChartCard
                    title="RR intervals"
                    note="Raw beat-to-beat intervals (tachogram), milliseconds per beat. This is the stream HRV is computed from — the wiggle itself is the variability."
                    yTitle="ms"
                    dark={dark}
                    className="sm:col-span-2"
                    datasets={series.filter((s) => s.rr.length > 0).map((s) => line(s.rr, s.alias))}
                />
            </div>
            <div className="flex flex-wrap gap-2">
                {series.map((s) => (
                    <div
                        key={s.alias}
                        className="rounded-lg border border-tiilt-line bg-tiilt-ground/40 px-3 py-2 text-xs text-tiilt-ink"
                    >
                        <span className="font-semibold">{s.alias}</span>
                        <span className="text-tiilt-muted">
                            {" · avg " + s.stats.avgHr + " bpm (" + s.stats.minHr + "–" + s.stats.maxHr + ")"}
                            {s.stats.rmssd != null && " · RMSSD " + s.stats.rmssd + " ms"}
                            {s.stats.sdnn != null && " · SDNN " + s.stats.sdnn + " ms"}
                        </span>
                    </div>
                ))}
            </div>
        </div>
    )
}

export { HeartRatePanel, bySpeaker, ChartCard, rrSequence }
