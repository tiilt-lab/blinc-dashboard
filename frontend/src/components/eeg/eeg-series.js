import { contactQuality } from "../../services/muse-eeg"

// EEG rows are speaker_eeg_metrics.json(): one per electrode per second,
// log10 band power (µV²) per band, std_uv for contact. These helpers turn
// them into per-second indices and put them on the audio's clock.

const BANDS = ["delta", "theta", "alpha", "beta", "gamma"]
// Seconds averaged per charted point.
export const EEG_SMOOTH_S = 10

// The correction for one headband from its matched sync taps
// (/sync: sync_taps.report): {at: session seconds, offset: seconds to add}
// points, constant with one, interpolated between two or more and held
// beyond them. Returns t => seconds to add (0 without any).
export function syncCorrection(syncs) {
    const pts = (syncs || [])
        .filter((s) => s.status === "matched" && s.offset_ms != null && s.tap_ms && s.tap_ms.length)
        .map((s) => ({ at: s.tap_ms.reduce((a, b) => a + b, 0) / s.tap_ms.length / 1000, offset: s.offset_ms / 1000 }))
        .sort((a, b) => a.at - b.at)
    if (!pts.length) return () => 0
    return (t) => {
        if (t <= pts[0].at) return pts[0].offset
        for (let i = 1; i < pts.length; i++) {
            if (t <= pts[i].at) {
                const f = (t - pts[i - 1].at) / (pts[i].at - pts[i - 1].at)
                return pts[i - 1].offset + f * (pts[i].offset - pts[i - 1].offset)
            }
        }
        return pts[pts.length - 1].offset
    }
}

// Per second: relative alpha (alpha / total power) and the engagement
// index beta / (alpha + theta) (Pope et al., 1995), averaged over the
// electrodes with good contact that second; seconds with none are counted
// but not charted. Returns {points: [{t, relAlpha, engagement, good}],
// seconds, goodSeconds}.
export function eegSeconds(rows, correction = () => 0) {
    const bySecond = new Map()
    for (const r of rows) {
        const ms = r.time_ms != null ? r.time_ms : r.time_stamp * 1000
        const k = Math.floor(ms / 1000)
        if (!bySecond.has(k)) bySecond.set(k, [])
        bySecond.get(k).push(r)
    }
    const points = []
    let goodSeconds = 0
    for (const [k, list] of [...bySecond.entries()].sort((a, b) => a[0] - b[0])) {
        const good = list.filter(
            (r) => contactQuality(r.std_uv) === "good" && BANDS.every((b) => r[b] != null),
        )
        if (!good.length) continue
        goodSeconds += good.length >= 3 ? 1 : 0
        let relAlpha = 0
        let engagement = 0
        for (const r of good) {
            const p = Object.fromEntries(BANDS.map((b) => [b, 10 ** r[b]]))
            const total = BANDS.reduce((s, b) => s + p[b], 0)
            relAlpha += p.alpha / total
            engagement += p.beta / (p.alpha + p.theta)
        }
        const t = k + correction(k)
        points.push({ t, relAlpha: relAlpha / good.length, engagement: engagement / good.length, good: good.length })
    }
    return { points, seconds: bySecond.size, goodSeconds }
}

// Mean of a point field over EEG_SMOOTH_S buckets, as chart {x, y}.
export function smooth(points, field) {
    const out = []
    let bucket = null
    for (const p of points) {
        const b = Math.floor(p.t / EEG_SMOOTH_S)
        if (!bucket || bucket.b !== b) {
            if (bucket) out.push({ x: bucket.t / bucket.n, y: bucket.v / bucket.n })
            bucket = { b, t: 0, v: 0, n: 0 }
        }
        bucket.t += p.t
        bucket.v += p[field]
        bucket.n += 1
    }
    if (bucket) out.push({ x: bucket.t / bucket.n, y: bucket.v / bucket.n })
    return out
}
