import { useEffect, useRef, useState } from "react"
import { btnPrimaryTall } from "../components/dialog-styles"
import { TIMED_STEPS, advance, cancel, countdownNumber } from "./baseline-protocol"

// Runs the baseline (baseline-protocol.js) for the join page: the current
// step, its timers, and the markers each step boundary sends through
// onMarker(label). The tone at the end of the eyes-closed rest tells the
// group to open their eyes; its AudioContext is made inside the "Record
// baseline" tap, the gesture browsers require before audio may play.
export function useBaseline(onMarker) {
    const [step, setStep] = useState(null)
    const [done, setDone] = useState(false)
    const [stepStart, setStepStart] = useState(0)
    const [now, setNow] = useState(0)
    const stepRef = useRef(null)
    const onMarkerRef = useRef(onMarker)
    onMarkerRef.current = onMarker
    const audio = useRef(null)

    const go = ({ next, markers }) => {
        markers.forEach((label) => onMarkerRef.current(label))
        stepRef.current = next
        const t = performance.now()
        setStep(next)
        setStepStart(t)
        setNow(t)
        if (next === null) {
            if (markers.includes("discussion_start")) setDone(true)
            leaveFullscreen()
        }
    }

    const start = () => {
        if (stepRef.current !== null) return
        try {
            const Ctx = window.AudioContext || window.webkitAudioContext
            if (Ctx && !audio.current) audio.current = new Ctx()
            audio.current?.resume?.().catch(() => {})
        } catch {
            audio.current = null
        }
        enterFullscreen()
        go(advance(null))
    }
    const next = () => {
        if (stepRef.current !== null) go(advance(stepRef.current))
    }
    const stop = () => go(cancel(stepRef.current))

    useEffect(() => {
        const seconds = TIMED_STEPS[step]
        if (!seconds) return undefined
        const timeout = setTimeout(() => {
            if (stepRef.current !== step) return
            if (step === "eyes_closed") playTone(audio.current)
            go(advance(step))
        }, seconds * 1000)
        const tick = setInterval(() => setNow(performance.now()), 200)
        return () => {
            clearTimeout(timeout)
            clearInterval(tick)
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [step])

    useEffect(
        () => () => {
            audio.current?.close?.().catch(() => {})
        },
        [],
    )

    const seconds = TIMED_STEPS[step]
    const remainingMs = seconds ? Math.max(0, seconds * 1000 - (now - stepStart)) : 0
    return { step, done, remainingMs, elapsedMs: now - stepStart, start, next, stop }
}

function enterFullscreen() {
    try {
        const el = document.documentElement
        if (!document.fullscreenElement && el.requestFullscreen) el.requestFullscreen().catch(() => {})
    } catch {
        // not supported (iPhone Safari): the overlay still covers the page
    }
}

function leaveFullscreen() {
    try {
        if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => {})
    } catch {
        // nothing to undo
    }
}

// A soft two-note chime, about a second long.
function playTone(ctx) {
    if (!ctx) return
    try {
        const t0 = ctx.currentTime
        ;[660, 880].forEach((freq, i) => {
            const osc = ctx.createOscillator()
            const gain = ctx.createGain()
            osc.frequency.value = freq
            gain.gain.setValueAtTime(0.0001, t0 + i * 0.35)
            gain.gain.exponentialRampToValueAtTime(0.3, t0 + i * 0.35 + 0.03)
            gain.gain.exponentialRampToValueAtTime(0.0001, t0 + i * 0.35 + 0.6)
            osc.connect(gain).connect(ctx.destination)
            osc.start(t0 + i * 0.35)
            osc.stop(t0 + i * 0.35 + 0.65)
        })
    } catch {
        // audio blocked: the screen change still shows the rest is over
    }
}

function formatElapsed(ms) {
    const s = Math.floor(ms / 1000)
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`
}

// The full-screen baseline view over the join page (the recording keeps
// running underneath). baseline: what useBaseline returns.
export function BaselineOverlay({ baseline }) {
    const { step, remainingMs, elapsedMs, next, stop } = baseline
    if (!step) return null
    const dark = step === "eyes_closed"
    const shell =
        "fixed inset-0 z-[3000] flex flex-col items-center justify-center px-6 text-center " +
        (dark ? "bg-neutral-900 text-neutral-500" : "bg-white text-tiilt-ink")
    const prompt = "max-w-xl text-xl leading-relaxed sm:text-2xl"
    const big = "font-ahamono text-[9rem] leading-none font-semibold"

    let body = null
    if (step === "countdown_open") {
        body = (
            <>
                <div className={prompt}>Look at the + in the middle of the screen until it disappears.</div>
                <div className={big + " mt-8"}>{countdownNumber(remainingMs)}</div>
            </>
        )
    } else if (step === "eyes_open") {
        body = (
            <div aria-label="Fixation cross" className="text-[8rem] leading-none font-light text-black select-none">
                +
            </div>
        )
    } else if (step === "countdown_closed") {
        body = (
            <>
                <div className={prompt}>
                    Close your eyes when the countdown ends. Keep them closed until you hear a tone.
                </div>
                <div className={big + " mt-8"}>{countdownNumber(remainingMs)}</div>
            </>
        )
    } else if (step === "eyes_closed") {
        body = <div className="text-lg">Eyes closed. A tone will sound when it is time to open them.</div>
    } else if (step === "game_intro") {
        body = (
            <>
                <div className="text-3xl font-semibold">Group game</div>
                <div className={prompt + " mt-4"}>
                    Take turns naming a fruit that nobody in your group has said yet, as quickly as you can.
                </div>
                <button type="button" className={btnPrimaryTall + " mt-8 w-72"} onClick={next}>
                    Start game
                </button>
            </>
        )
    } else if (step === "game") {
        body = (
            <>
                <div className="text-3xl font-semibold">Name a fruit!</div>
                <div className={prompt + " mt-3"}>No repeats. Keep going as fast as you can.</div>
                <div className="font-ahamono mt-6 text-4xl text-tiilt-muted">{formatElapsed(elapsedMs)}</div>
                <button type="button" className={btnPrimaryTall + " mt-8 w-72"} onClick={next}>
                    End game
                </button>
            </>
        )
    } else if (step === "discussion_ready") {
        body = (
            <>
                <div className="text-3xl font-semibold">Thanks!</div>
                <div className={prompt + " mt-3"}>When your group is ready, start the discussion.</div>
                <button type="button" className={btnPrimaryTall + " mt-8 w-72"} onClick={next}>
                    Start discussion
                </button>
            </>
        )
    }

    return (
        <div className={shell} role="dialog" aria-modal="true" aria-label="Baseline">
            {body}
            <button
                type="button"
                className={
                    "absolute top-3 right-3 rounded-md px-3 py-1.5 text-xs opacity-40 transition hover:opacity-100 " +
                    (dark ? "text-neutral-400" : "text-tiilt-muted")
                }
                onClick={stop}
            >
                Stop baseline
            </button>
        </div>
    )
}
