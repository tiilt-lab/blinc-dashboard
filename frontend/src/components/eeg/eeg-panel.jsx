import { useIsDark } from "../../myhooks/custom-hooks"
import { applyChartTheme } from "../chart-theme"
import { speakerColorFor } from "../../globals"
import { bySpeaker, ChartCard } from "../heart-rate/heart-rate-panel"
import { eegSeconds, smooth, syncCorrection } from "./eeg-series"

// EEG from Muse headbands assigned on the join page (muse-eeg.js): band
// power per electrode per second. Charted per wearer, over the electrodes
// with good contact: relative alpha (alpha / total power; rises with
// relaxed, eyes-closed or internally focused states) and the engagement
// index beta / (alpha + theta). Both are coarse, population-level
// indicators from four dry electrodes, not per-person diagnostics.
//
// Timing: each headband's samples are moved by the offset its sync taps
// measured against the pod's audio (sync_taps.py), when there are any; the
// chips say which wearers were checked.

function syncNote(syncs) {
    const matched = syncs.filter((s) => s.status === "matched")
    if (!matched.length) {
        return syncs.length
            ? "timing not confirmed: sync taps not found in the audio"
            : "timing not checked against audio"
    }
    const offsets = matched.map((s) => s.offset_ms)
    const fmt = (v) => (v > 0 ? "+" : "") + v + " ms"
    return matched.length === 1
        ? "aligned to audio (" + fmt(offsets[0]) + ")"
        : "aligned to audio (" + offsets.map(fmt).join(" → ") + ")"
}

// rawStreams: [{sensor_kind, stream, seconds, rate, href}] from /raw.
const RAW_LABELS = { eeg: "Raw EEG", acc: "Raw accelerometer" }

function RawDownloads({ streams }) {
    if (!streams || !streams.length) return null
    return (
        <div className="flex flex-wrap items-center gap-2 text-xs text-tiilt-muted">
            <span>Download (CSV, one row per sample):</span>
            {streams.map((s) => (
                <a
                    key={s.sensor_kind + s.stream}
                    href={s.href}
                    download
                    className="rounded-lg border border-tiilt-line px-2.5 py-1 font-semibold text-tiilt-ink transition hover:bg-tiilt-ground"
                >
                    {(RAW_LABELS[s.stream] || s.stream) + " · " + s.rate + " Hz · " + s.seconds + " s"}
                </a>
            ))}
        </div>
    )
}

function EegPanel({ rows, speakers, syncs, rawStreams }) {
    applyChartTheme()
    const dark = useIsDark()
    const grouped = bySpeaker(rows, speakers)
    if (grouped.size === 0)
        return (
            <div className="py-6 text-center text-sm text-tiilt-muted">
                No EEG headbands streamed data for this pod.
            </div>
        )
    const allAliases = [...new Set([...(speakers || []).map((s) => s.alias), ...grouped.keys()])]
    const colorOf = (alias) => speakerColorFor(alias, allAliases)

    const series = [...grouped.entries()].map(([alias, list]) => {
        const speakerId = list[0].speaker_id
        const mine = (syncs || []).filter((s) => s.speaker_id === speakerId && s.sensor_kind === "muse")
        const { points, seconds, goodSeconds } = eegSeconds(list, syncCorrection(mine))
        return {
            alias,
            sensor: (list.find((r) => r.sensor_name) || {}).sensor_name || "",
            relAlpha: smooth(points, "relAlpha"),
            engagement: smooth(points, "engagement"),
            goodShare: seconds ? Math.round((100 * goodSeconds) / seconds) : 0,
            sync: syncNote(mine),
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
            <div className="grid w-full grid-cols-1 gap-3 sm:grid-cols-2">
                <ChartCard
                    title="Relative alpha"
                    note="Alpha (8-13 Hz) power as a share of all power (1-44 Hz), electrodes with good contact, 10-second averages."
                    yTitle="share"
                    dark={dark}
                    datasets={series.filter((s) => s.relAlpha.length > 0).map((s) => line(s.relAlpha, s.alias))}
                />
                <ChartCard
                    title="Engagement index β/(α+θ)"
                    note="Beta power over alpha plus theta (Pope et al., 1995), electrodes with good contact, 10-second averages. Higher generally reads as more task-engaged."
                    yTitle="ratio"
                    dark={dark}
                    datasets={series.filter((s) => s.engagement.length > 0).map((s) => line(s.engagement, s.alias))}
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
                            {s.sensor && " (" + s.sensor + ")"}
                            {" · good contact " + s.goodShare + "% of the time · " + s.sync}
                        </span>
                    </div>
                ))}
            </div>
            <RawDownloads streams={rawStreams} />
        </div>
    )
}

export { EegPanel }
