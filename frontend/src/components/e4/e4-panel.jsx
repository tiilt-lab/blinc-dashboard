// Empatica E4 wristbands paired on the join page (e4-wristband.js). Nothing
// is computed from them on the dashboard yet: the section offers each raw
// stream as a CSV (one row per sample, raw_signals.py) and the session's
// markers, where every press of a band's button is a "wristband_button" row.

const E4_STREAMS = {
    bvp: { label: "Blood volume pulse", note: "green/red light changes and their running totals (the pulse wave)" },
    eda: { label: "Skin conductance (EDA)", note: "raw count and µS" },
    temp: { label: "Skin temperature", note: "°C" },
    acc: { label: "Accelerometer", note: "x, y, z in g" },
}
const ORDER = ["bvp", "eda", "temp", "acc"]

// streams: [{sensor_kind: "e4", stream, seconds, rate, href}] from /raw;
// markersHref: the session's markers CSV.
function E4Panel({ streams, markersHref }) {
    const sorted = [...(streams || [])].sort((a, b) => ORDER.indexOf(a.stream) - ORDER.indexOf(b.stream))
    return (
        <div className="flex w-full flex-col gap-3 text-xs text-tiilt-muted">
            <p>
                Raw signals as the wristbands sent them, on the session clock, one row per sample. Button presses
                are in the markers file as <span className="font-ahamono">wristband_button</span>, with the
                wearer in the sensors column.
            </p>
            <div className="flex flex-wrap items-center gap-2">
                {sorted.map((s) => {
                    const info = E4_STREAMS[s.stream] || { label: s.stream, note: "" }
                    return (
                        <a
                            key={s.stream}
                            href={s.href}
                            download
                            title={info.note}
                            className="rounded-lg border border-tiilt-line px-2.5 py-1 font-semibold text-tiilt-ink transition hover:bg-tiilt-ground"
                        >
                            {info.label + " · " + s.rate + " Hz · " + s.seconds + " s"}
                        </a>
                    )
                })}
                {markersHref && (
                    <a
                        href={markersHref}
                        download
                        title="Every pod's markers for the session, including E4 button presses"
                        className="rounded-lg border border-tiilt-line px-2.5 py-1 font-semibold text-tiilt-ink transition hover:bg-tiilt-ground"
                    >
                        Markers · whole session
                    </a>
                )}
            </div>
        </div>
    )
}

export { E4Panel }
