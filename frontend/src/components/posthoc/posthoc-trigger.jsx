import { useRef, useState, useEffect } from "react"
import { Refresh } from "@/Icons"
import { StatusPill } from "../status-pill"
import { ApiService } from "../../services/api-service"
import { SessionService } from "../../services/session-service"
import { PosthocService } from "../../services/posthoc-service"

// Post-hoc re-analysis actions, faithfully ported from the videodev
// pod-component's dual-socket protocol. Three operations:
//
//   full  — re-read the stored recording off disk and regenerate transcript +
//           metrics from scratch (runs ASR). Audio + video sockets, no
//           transcript payload.
//   pi    — re-compute Participation & Impact style from the EXISTING
//           transcript (skips ASR). Audio socket, carries the transcript.
//   et    — re-compute Expressing & Thinking style from the existing
//           transcript. Audio socket, carries the transcript.
//
// Every operation follows: connect -> Initialize_* -> on a readiness ack send
// the matching start_* -> track started -> process_completed, with a 20s
// heartbeat while running. Every message that names a pod carries a
// short-lived `ticket` minted by the API (see PosthocService); without it the
// services answer {type: "error", message: "Not authorised for this pod."}.

// Server readiness acks (→ send the operation's start message).
const READY_ACKS = new Set([
    "init posthoc analytics completed",
    "init participation and impact style completed",
    "init expression and thinking style completed",
])
// Server "processing has begun" signals.
const STARTED_ACKS = new Set([
    "audio posthoc analytics started",
    "video posthoc analytics started",
    "speaker metric computation started",
])

// Selectable backends per module. Only transcription and E&T scoring have
// alternative implementations today; the rest are shown (single option,
// locked) so it is explicit what computes each result.
const ASR_OPTIONS = [
    { id: "google-cloud-speech", label: "Google Cloud Speech-to-Text" },
    { id: "whisperx", label: "WhisperX (open SOTA: batched + word-aligned)" },
    { id: "qwen3", label: "Qwen3-ASR 1.7B (open; leaderboard-best WER, slower)" },
    { id: "qwen3-0.6b", label: "Qwen3-ASR 0.6B (open; fast variant)" },
    { id: "crisperwhisper", label: "CrisperWhisper 2.0 (verbatim: fillers, stutters, pause-true timing)" },
]
const DIARIZER_OPTIONS = [
    { id: "fingerprint", label: "ECAPA fingerprint matching (enrolled voices)" },
    { id: "pyannote", label: "pyannote 3.1 clustering (open SOTA; WhisperX only)" },
    { id: "sortformer", label: "NVIDIA Sortformer streaming 4-spk v2.1 (WhisperX only)" },
]
const EMBEDDER_OPTIONS = [
    { id: "bge-large-en-v1.5", label: "BGE large v1.5 (open SOTA embedder)" },
    { id: "all-mpnet-base-v2", label: "all-mpnet-base-v2 (matches historical metrics)" },
]
const SCORER_OPTIONS = [
    { id: "liwc", label: "LIWC & Harvard General Inquirer lexicons" },
    { id: "open", label: "Harvard General Inquirer (open)" },
    { id: "llm", label: "Google Gemini (LLM)" },
]
const ATTENTION_OPTIONS = [
    { id: "gazelle", label: "Gaze-LLE (DINOv2; CVPR 2025)" },
    { id: "page-vith+", label: "PAGE ViT-H+ (claimed SOTA, arXiv 2026 — experimental)" },
]

function PosthocTrigger({ session, sessionDeviceId, speakers, transcripts, models, lastAnalyzed, sessionDevice }) {
    const api = new ApiService()
    // Per-pod ticket the sockets require. Reusable until it expires, so it is
    // cached and re-minted a minute early (the status probe re-asks every
    // 15 s and can outlive one ticket). Keyed to the pod it was minted for.
    const ticket = useRef({ value: null, expiresAt: 0, forDevice: null })
    const getTicket = () => {
        const t = ticket.current
        if (t.value && t.forDevice === sessionDeviceId && Date.now() < t.expiresAt)
            return Promise.resolve(t.value)
        return new PosthocService().getTicket(session.id, sessionDeviceId).then((d) => {
            ticket.current = {
                value: d.ticket,
                forDevice: sessionDeviceId,
                expiresAt: Date.now() + Math.max(30, (d.ttl || 900) - 60) * 1000,
            }
            return d.ticket
        })
    }
    const sockets = useRef([])
    const heartbeat = useRef(null)
    // Per-stream state for the currently running action, keyed by a label
    // ("Audio"/"Video"). idle|connecting|running|done|error
    const [streams, setStreams] = useState({})
    const streamsRef = useRef({})
    // Per-stream detail: { message, percent, startedAt, endedAt } for the live
    // progress view, keyed by the same labels as `streams`.
    const [streamMeta, setStreamMeta] = useState({})
    const streamMetaRef = useRef({})
    const [, setTick] = useState(0) // re-render for the live elapsed timers
    const [action, setAction] = useState(null) // "full" | "pi" | "et" | null
    const [, setMessage] = useState("")
    const updateMeta = (label, patch) => {
        streamMetaRef.current = {
            ...streamMetaRef.current,
            [label]: { ...(streamMetaRef.current[label] || {}), ...patch },
        }
        setStreamMeta(streamMetaRef.current)
    }
    // Model choices for the next run; default to the active deployment config
    // once /api/v1/models arrives.
    const [asrChoice, setAsrChoice] = useState(null)
    const [scorerChoice, setScorerChoice] = useState(null)
    // Selectors prefill from the pod's per-run provenance (what produced the
    // data on screen), so the panel reads as current -> next; the open SOTA
    // stack is the fallback for pods that predate provenance stamping.
    const podModels = (sessionDevice && sessionDevice.posthoc_models) || {}
    const asr = asrChoice || podModels.asr || "whisperx"
    const scorer = scorerChoice || podModels.scorer || (models && models.scoring && models.scoring.id) || "liwc"
    const [diarizerChoice, setDiarizerChoice] = useState(null)
    const diarizer = diarizerChoice || podModels.diarizer || "pyannote"
    const [embedderChoice, setEmbedderChoice] = useState(null)
    const embedder = embedderChoice || podModels.embedder || "bge-large-en-v1.5"
    // Optional video active-speaker detection (TalkNCE trained on UniTalk):
    // per-face speaking scores fused with transcripts after analysis. Off by
    // default — it adds a full-video face-tracking pass to the run.
    const [asdChoice, setAsdChoice] = useState(null)
    const asd = asdChoice || (podModels.asd_model ? "talknce" : "none")
    const [attentionChoice, setAttentionChoice] = useState(null)
    const attention =
        attentionChoice ||
        podModels.attention ||
        (models && models.attention && models.attention.id) ||
        "gazelle"

    const running = Object.values(streams).some(
        (s) => s === "connecting" || s === "running",
    )

    useEffect(() => {
        if (!running) return
        const id = setInterval(() => setTick((t) => t + 1), 1000)
        return () => clearInterval(id)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [running])

    // On mount, ask each post-hoc service whether a run for this pod is already
    // active (e.g. started before a page refresh — runs survive disconnects).
    // If so, restore the card and poll until it completes.
    useEffect(() => {
        const api = new ApiService()
        const probes = []
        const probe = (endpoint, label) => {
            try {
                const ws = new WebSocket(endpoint)
                const ask = () =>
                    getTicket()
                        .then((t) => {
                            if (ws.readyState !== WebSocket.OPEN) return
                            ws.send(
                                JSON.stringify({
                                    type: "query_posthoc_status",
                                    sessiondeviceid: sessionDeviceId,
                                    ticket: t,
                                }),
                            )
                        })
                        .catch(() => {
                            // Ticket refused mid-probe: nothing to ask.
                            try {
                                ws.close()
                            } catch {
                                /* already closed */
                            }
                        })
                let timer = null
                ws.onopen = ask
                ws.onmessage = (e) => {
                    let msg
                    try {
                        msg = JSON.parse(e.data)
                    } catch {
                        return
                    }
                    // After a status match, the server re-attaches the running
                    // processor to this socket, so live progress/completion
                    // stream here too.
                    if (msg.type === "progress") {
                        updateMeta(label, {
                            startedAt: msg.started_at
                                ? msg.started_at * 1000
                                : (streamMetaRef.current[label] || {})
                                      .startedAt || Date.now(),
                            message: msg.message,
                            percent: msg.percent,
                        })
                        return
                    }
                    if (msg.type === "process_completed") {
                        setStream(label, "done")
                        updateMeta(label, {
                            endedAt: Date.now(),
                            percent: 100,
                            message: "Complete",
                        })
                        if (timer) clearInterval(timer)
                        ws.close()
                        return
                    }
                    if (msg.type !== "posthoc_status") return
                    if (msg.running) {
                        if (streamsRef.current[label] !== "running") {
                            setAction("full")
                            setStream(label, "running")
                            updateMeta(label, {
                                startedAt:
                                    (streamMetaRef.current[label] || {})
                                        .startedAt || Date.now(),
                                message:
                                    "Running in the background (started before this page load)",
                            })
                        }
                        if (!timer) timer = setInterval(ask, 15000)
                    } else if (streamsRef.current[label] === "running") {
                        setStream(label, "done")
                        updateMeta(label, {
                            endedAt: Date.now(),
                            message: "Complete",
                        })
                        if (timer) clearInterval(timer)
                        ws.close()
                    } else {
                        ws.close()
                    }
                }
                ws.onerror = () => {}
                probes.push(() => {
                    if (timer) clearInterval(timer)
                    try {
                        ws.close()
                    } catch {
                        /* already closed */
                    }
                })
            } catch {
                /* service unreachable */
            }
        }
        // Mint the ticket BEFORE opening any socket. Without one (read-only
        // viewer, or the API refused) skip the probes: the HTTP
        // posthoc_running poll below still reports background runs.
        let unmounted = false
        getTicket()
            .then(() => {
                if (unmounted) return
                probe(api.getAudioPosthocWebsocketEndpoint(), "Audio")
                probe(api.getVideoPosthocWebsocketEndpoint(), "Video")
            })
            .catch(() => {})
        return () => {
            unmounted = true
            probes.forEach((f) => f())
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [sessionDeviceId])

    // Reliable status fallback: the websocket probes above ask the processing
    // services directly, but a busy run starves their event loops for minutes
    // at a time, so probes time out and an active run shows nothing. The
    // capture server tracks run state from the services' HTTP callbacks —
    // poll that, and only fill in streams the probes left idle.
    useEffect(() => {
        let stopped = false
        const check = () =>
            new ApiService()
                .httpRequestCall(
                    `api/v1/sessions/${session.id}/device/${sessionDeviceId}/posthoc_running`,
                    "GET",
                    {},
                )
                .then((r) => (r.status === 200 ? r.json() : null))
                .then((d) => {
                    if (stopped || !d) return
                    const scopes = d.scopes || []
                    for (const [scope, label] of [
                        ["audio", "Audio"],
                        ["video", "Video"],
                    ]) {
                        const cur = streamsRef.current[label]
                        const meta = streamMetaRef.current[label] || {}
                        if (scopes.includes(scope)) {
                            if (!cur || cur === "idle") {
                                setAction("full")
                                setStream(label, "running")
                                updateMeta(label, {
                                    startedAt: d.started_at
                                        ? d.started_at * 1000
                                        : Date.now(),
                                    message: "Running in the background",
                                })
                            }
                        } else if (
                            cur === "running" &&
                            meta.message === "Running in the background"
                        ) {
                            // Only complete runs this poll discovered; the
                            // websocket path owns its own completion events.
                            setStream(label, "done")
                            updateMeta(label, {
                                endedAt: Date.now(),
                                message: "Complete",
                            })
                        }
                    }
                })
                .catch(() => {})
        check()
        const t = setInterval(check, 15000)
        return () => {
            stopped = true
            clearInterval(t)
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [sessionDeviceId])

    const speakerList = (speakers || []).map((sp) => ({
        id: sp.id,
        alias: sp.alias,
    }))
    const baseInit = {
        sessionid: session.id,
        server_start: session.creation_date,
        keywords: session.keywords,
        sessiondeviceid: sessionDeviceId,
        speakers: speakerList,
    }

    const marked = useRef(false)
    const setStream = (label, value) => {
        streamsRef.current = { ...streamsRef.current, [label]: value }
        setStreams(streamsRef.current)
        const active = Object.values(streamsRef.current)
        const allSettled = active.every(
            (s) => s === "done" || s === "error" || s === "idle",
        )
        if (allSettled && heartbeat.current) {
            clearInterval(heartbeat.current)
            heartbeat.current = null
        }
        // When at least one stream completed successfully, record that this pod
        // has had a post-hoc re-analysis so the sessions list can flag it.
        if (allSettled && !marked.current && active.some((s) => s === "done")) {
            marked.current = true
            // Record which models produced this re-analysis (provenance).
            const modelChoices = {
                asr,
                scorer,
                diarizer,
                embedder,
                emotion: models && models.emotion && models.emotion.id,
                attention,
                objects: models && models.objects && models.objects.id,
                keywords: models && models.keywords && models.keywords.id,
            }
            new SessionService()
                .markPosthocCompleted(session.id, sessionDeviceId, modelChoices)
                .catch(() => {})
        }
    }

    const startHeartbeat = () => {
        if (heartbeat.current) return
        heartbeat.current = setInterval(() => {
            const hb = JSON.stringify({
                type: "heartbeat_from_posthoc_processing",
                key: "no key (posthoc processing)",
            })
            for (const ws of sockets.current) {
                if (ws && ws.readyState === WebSocket.OPEN) ws.send(hb)
            }
        }, 20000)
    }

    // Open one socket for one operation on one stream; `t` is the pod ticket.
    const openSocket = ({ endpoint, label, init, startType }, t) => {
        const ws = new WebSocket(endpoint)
        ws.binaryType = "arraybuffer"
        sockets.current.push(ws)
        setStream(label, "connecting")
        ws.onopen = () => ws.send(JSON.stringify({ ...init, ticket: t }))
        ws.onmessage = (e) => {
            if (typeof e.data !== "string") return
            let msg
            try {
                msg = JSON.parse(e.data)
            } catch {
                return
            }
            if (READY_ACKS.has(msg.type)) {
                ws.send(JSON.stringify({ type: startType, ticket: t }))
                startHeartbeat()
            } else if (STARTED_ACKS.has(msg.type)) {
                setStream(label, "running")
                updateMeta(label, {
                    startedAt:
                        (streamMetaRef.current[label] || {}).startedAt ||
                        Date.now(),
                    message: msg.message || "Processing…",
                })
            } else if (msg.type === "progress") {
                if (streamsRef.current[label] !== "running")
                    setStream(label, "running")
                updateMeta(label, {
                    startedAt: msg.started_at
                        ? msg.started_at * 1000
                        : (streamMetaRef.current[label] || {}).startedAt ||
                          Date.now(),
                    message: msg.message,
                    percent: msg.percent,
                })
            } else if (msg.type === "process_completed") {
                setStream(label, "done")
                updateMeta(label, {
                    endedAt: Date.now(),
                    percent: 100,
                    message: "Complete",
                })
            } else if (msg.type === "error") {
                setMessage(msg.message || "Analysis failed.")
                setStream(label, "error")
                updateMeta(label, {
                    endedAt: Date.now(),
                    message: msg.message || "Failed",
                })
            }
        }
        ws.onclose = () => {
            const prev = streamsRef.current[label]
            if (prev === "running" || prev === "connecting")
                setStream(label, "error")
        }
    }

    // Unmount: close run sockets and stop the heartbeat. The run itself
    // continues server-side (completion is recorded via the server-side
    // callback), but the orphaned sockets used to keep heartbeating and
    // firing setState on an unmounted component after navigation.
    useEffect(() => {
        return () => {
            if (heartbeat.current) {
                clearInterval(heartbeat.current)
                heartbeat.current = null
            }
            for (const ws of sockets.current) {
                if (!ws) continue
                ws.onmessage = null
                ws.onerror = null
                ws.onclose = null
                try {
                    ws.close()
                } catch {
                    /* noop */
                }
            }
            sockets.current = []
        }
    }, [])

    const begin = (kind, defs) => {
        // Reset for a fresh run.
        for (const ws of sockets.current) {
            try {
                ws.close()
            } catch {
                /* noop */
            }
        }
        sockets.current = []
        streamsRef.current = {}
        streamMetaRef.current = {}
        marked.current = false
        setStreams({})
        setStreamMeta({})
        setMessage("")
        setAction(kind)
        // Ticket first, then the sockets: without one the services refuse
        // every pod-naming message, so surface the refusal here instead of a
        // bare socket error.
        getTicket()
            .then((t) => defs.forEach((d) => openSocket(d, t)))
            .catch((e) => {
                const reason = `Not authorised to re-run analysis on this pod (${e.message}).`
                setMessage(reason)
                for (const d of defs) {
                    setStream(d.label, "error")
                    updateMeta(d.label, { endedAt: Date.now(), message: reason })
                }
            })
    }

    const runFull = () =>
        begin("full", [
            {
                endpoint: api.getAudioPosthocWebsocketEndpoint(),
                label: "Audio",
                startType: "start_posthoc_audio_processing",
                init: {
                    ...baseInit,
                    type: "Initialize_audio_processing_analytics",
                    asr,
                    scorer,
                    diarizer,
                    embedder,
                },
            },
            {
                endpoint: api.getVideoPosthocWebsocketEndpoint(),
                label: "Video",
                startType: "start_posthoc_video_processing",
                init: {
                    ...baseInit,
                    type: "Initialize_video_processing_analytics",
                    asd: asd === "none" ? null : asd,
                    attention_model: attention,
                },
            },
        ])

    const label = (s) =>
        s === "connecting"
            ? "Starting…"
            : s === "running"
              ? "Processing…"
              : s === "done"
                ? "Complete"
                : s === "error"
                  ? "Failed"
                  : "—"
    const color = (s) =>
        s === "done"
            ? "text-tiilt-teal-text"
            : s === "error"
              ? "text-tiilt-danger"
              : "text-tiilt-muted"

    const fmtElapsed = (meta) => {
        if (!meta || !meta.startedAt) return null
        const end = meta.endedAt || Date.now()
        const s = Math.max(0, Math.floor((end - meta.startedAt) / 1000))
        const m = Math.floor(s / 60)
        return m > 0 ? `${m}m ${s % 60}s` : `${s}s`
    }

    // "9m 12s" from a plain seconds count (persisted module durations).
    const fmtSecs = (s) => {
        const m = Math.floor(s / 60)
        return m > 0 ? `${m}m ${s % 60}s` : `${s}s`
    }
    // Persisted per-module durations of the last completed run, stamped
    // server-side at each scope's completion callback.
    const lastDurations = podModels.durations || {}
    const durationSummary = ["audio", "video"]
        .filter((s) => lastDurations[s])
        .map((s) => `${s} ${fmtSecs(lastDurations[s])}`)
        .join(" · ")

    // tqdm-style remaining-time estimate from elapsed and percent complete.
    const fmtEta = (meta) => {
        if (
            !meta ||
            !meta.startedAt ||
            meta.endedAt ||
            typeof meta.percent !== "number" ||
            meta.percent <= 2 ||
            meta.percent >= 100
        )
            return null
        const elapsed = (Date.now() - meta.startedAt) / 1000
        const remaining = Math.round((elapsed * (100 - meta.percent)) / meta.percent)
        const m = Math.floor(remaining / 60)
        return m > 0 ? `~${m}m ${remaining % 60}s left` : `~${remaining}s left`
    }

    // Wall-clock estimate for a full re-run, measured on this instance:
    // a 5.5-min recording took ~6.3 min end-to-end (WhisperX transcribes in
    // seconds; the per-utterance semantic/diarization/keyword processing
    // dominates), so estimate ~1.2x the recording length. The pod's recording
    // is often much shorter than the session, so use the transcript extent as
    // the recording-length proxy and fall back to the session length.
    let recordingSeconds = (session && session.length) || 0
    if (transcripts && transcripts.length > 0) {
        const times = transcripts.map((t) => t.start_time)
        recordingSeconds = Math.max(...times) - Math.min(...times) + 30
    }
    // Measured: audio-only ~1.2x recording length; with video ~1.8x (the CV
    // pass runs in parallel but finishes later).
    const estimateFactor = session && session.has_video ? 1.8 : 1.2
    const fullEstimateMin = Math.max(1, Math.ceil((recordingSeconds / 60) * estimateFactor) || 5)

    const btn =
        "h-10 rounded-lg px-4 text-sm font-semibold transition active:translate-y-px disabled:opacity-50"
    const selectCls =
        "w-full cursor-pointer rounded-lg border border-tiilt-line bg-white py-1.5 pr-8 pl-3 text-sm text-tiilt-ink transition outline-none focus-visible:border-tiilt focus-visible:ring-[3px] focus-visible:ring-tiilt/30 disabled:cursor-default disabled:bg-tiilt-ground disabled:text-tiilt-muted"

    // Fixed-implementation modules, shown read-only in the disclosure below.
    // Labels come from the (provenance-merged) `models` prop when loaded.
    const fixedModules = [
        ["Facial emotion", "emotion", "HSEmotion EfficientNet-B2 (AffectNet-8)"],
        ["Head / person detector", "head_detector", "YOLOv5m (CrowdHuman, 2021)"],
        ["Object of focus", "objects", "YOLO11m (COCO)"],
        ["Keyword matching", "keywords", "SentenceTransformer (BGE)"],
        ["Face recognition", "face", "dlib ResNet (128-D)"],
    ]

    // Marks a selector whose value differs from what produced the current
    // data — the panel reads as "current -> next".
    const willChange = (field, value) =>
        podModels[field] !== undefined && podModels[field] !== value

    const SelectorRow = ({ name, field, value, children }) => (
        <div className="grid grid-cols-1 items-center gap-1 sm:grid-cols-[10rem_1fr] sm:gap-3">
            <span className="flex items-center gap-2 text-sm font-semibold text-tiilt-ink">
                {name}
                {field && willChange(field, value) ? (
                    <span className="rounded-full bg-tiilt-orange/15 px-1.5 py-0.5 text-[10px] font-semibold whitespace-nowrap text-tiilt-orange-text">
                        will change
                    </span>
                ) : null}
            </span>
            {children}
        </div>
    )

    const ActionCard = ({ title, time, tone, desc, children }) => (
        <div className="flex flex-col gap-3 rounded-xl border border-tiilt-line bg-white p-4">
            <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold text-tiilt-ink">{title}</span>
                <StatusPill tone={tone || "neutral"}>{time}</StatusPill>
            </div>
            <div className="text-xs leading-relaxed text-tiilt-muted">{desc}</div>
            {children}
        </div>
    )

    // Short names for the "current data" summary chips.
    const shortName = (id) =>
        ({
            "google-cloud-speech": "Google STT",
            whisperx: "WhisperX",
            whisper: "Whisper",
            qwen3: "Qwen3-ASR",
            "qwen3-0.6b": "Qwen3-ASR 0.6B",
            pyannote: "pyannote",
            fingerprint: "voice prints",
            sortformer: "Sortformer",
            liwc: "LIWC",
            open: "Inquirer",
            llm: "Gemini LLM",
            "bge-large-en-v1.5": "BGE",
            "all-mpnet-base-v2": "MPNet",
        })[id] || id

    return (
        <div className="flex w-full flex-col gap-4">
            <div>
                {podModels.asr || podModels.scorer ? (
                    <div className="flex flex-wrap items-center gap-1.5 text-xs text-tiilt-muted">
                        <span>Current data was produced by</span>
                        {["asr", "diarizer", "scorer", "embedder"]
                            .filter((f) => podModels[f])
                            .map((f) => (
                                <StatusPill key={f} tone="neutral">
                                    {shortName(podModels[f])}
                                </StatusPill>
                            ))}
                        {podModels.asd_model ? (
                            <StatusPill tone="neutral">ASD</StatusPill>
                        ) : null}
                        {lastAnalyzed ? <span>on {lastAnalyzed}</span> : null}
                    </div>
                ) : lastAnalyzed && !running ? (
                    <div className="flex items-center gap-1.5 text-xs font-semibold text-tiilt-teal-text">
                        <Refresh />
                        Last full analysis: {lastAnalyzed}
                    </div>
                ) : null}
                {durationSummary && !running ? (
                    <div className="mt-1 text-xs text-tiilt-muted">
                        Previous run took {durationSummary}
                    </div>
                ) : null}
                <details className="mt-1.5">
                    <summary className="cursor-pointer text-xs text-tiilt-muted hover:text-tiilt">
                        All models used (fixed modules)
                    </summary>
                    <div className="mt-2 flex flex-col gap-1 rounded-lg bg-tiilt-ground/50 px-3 py-2">
                        {fixedModules.map(([name, key, fallback]) => (
                            <div
                                key={key}
                                className="grid grid-cols-1 gap-0.5 text-xs sm:grid-cols-[10rem_1fr]"
                            >
                                <span className="font-semibold text-tiilt-ink">{name}</span>
                                <span className="text-tiilt-muted">
                                    {(models && models[key] && models[key].label) || fallback}
                                </span>
                            </div>
                        ))}
                    </div>
                </details>
            </div>

            <ActionCard
                title="Full re-analysis"
                time={`~${fullEstimateMin} min`}
                tone="orange"
                desc={
                    <>
                        Re-transcribes the stored recording from scratch and rebuilds
                        every metric.{" "}
                        <span className="font-semibold text-tiilt-orange-text">
                            Replaces the transcript and all analytics for this pod.
                        </span>{" "}
                        Uses the models selected below.
                    </>
                }
            >
                <SelectorRow name="Transcription" field="asr" value={asr}>
                    <select
                        value={asr}
                        onChange={(e) => setAsrChoice(e.target.value)}
                        disabled={running}
                        className={selectCls}
                    >
                        {ASR_OPTIONS.map((o) => (
                            <option key={o.id} value={o.id}>
                                {o.label}
                            </option>
                        ))}
                    </select>
                </SelectorRow>
                <SelectorRow name="Speaker identification" field="diarizer" value={diarizer}>
                    <select
                        value={diarizer}
                        onChange={(e) => setDiarizerChoice(e.target.value)}
                        disabled={running}
                        className={selectCls}
                    >
                        {DIARIZER_OPTIONS.map((o) => (
                            <option key={o.id} value={o.id}>
                                {o.label}
                            </option>
                        ))}
                    </select>
                </SelectorRow>
                <SelectorRow name="Active speaker detection" field={null} value={asd}>
                    <select
                        value={asd}
                        onChange={(e) => setAsdChoice(e.target.value)}
                        disabled={running}
                        className={selectCls}
                    >
                        <option value="none">Off</option>
                        <option value="talknce">
                            TalkNCE trained on UniTalk (who is talking on camera)
                        </option>
                    </select>
                </SelectorRow>
                <SelectorRow name="Attention / gaze" field="attention" value={attention}>
                    <select
                        value={attention}
                        onChange={(e) => setAttentionChoice(e.target.value)}
                        disabled={running}
                        className={selectCls}
                    >
                        {ATTENTION_OPTIONS.map((o) => (
                            <option key={o.id} value={o.id}>
                                {o.label}
                            </option>
                        ))}
                    </select>
                </SelectorRow>
                <SelectorRow name="Scoring method" field="scorer" value={scorer}>
                    <select
                        value={scorer}
                        onChange={(e) => setScorerChoice(e.target.value)}
                        disabled={running}
                        className={selectCls}
                    >
                        {SCORER_OPTIONS.map((o) => (
                            <option key={o.id} value={o.id}>
                                {o.label}
                            </option>
                        ))}
                    </select>
                </SelectorRow>
                <SelectorRow name="Sentence embedder" field="embedder" value={embedder}>
                    <select
                        value={embedder}
                        onChange={(e) => setEmbedderChoice(e.target.value)}
                        disabled={running}
                        className={selectCls}
                    >
                        {EMBEDDER_OPTIONS.map((o) => (
                            <option key={o.id} value={o.id}>
                                {o.label}
                            </option>
                        ))}
                    </select>
                </SelectorRow>
                <div>
                    <button
                        onClick={runFull}
                        disabled={running}
                        className={btn + " bg-tiilt text-white hover:bg-tiilt-deep"}
                    >
                        Re-run full analysis
                    </button>
                </div>
            </ActionCard>

            {action && Object.keys(streams).length > 0 ? (
                <div className="flex flex-col gap-2 text-sm">
                    {Object.keys(streams).length > 1 ? (
                        <div className="text-xs text-tiilt-muted">
                            {Object.keys(streams).join(" and ")} run{" "}
                            <span className="font-semibold">concurrently</span>.
                        </div>
                    ) : null}
                    {Object.entries(streams).map(([name, state]) => {
                        const meta = streamMeta[name] || {}
                        const elapsed = fmtElapsed(meta)
                        return (
                            <div
                                key={name}
                                className="rounded-lg border border-tiilt-line bg-white p-2.5"
                            >
                                <div className="flex items-center justify-between gap-2">
                                    <div className="flex items-center gap-2">
                                        <span className="font-semibold text-tiilt-ink">
                                            {name}
                                        </span>
                                        <span className={"text-xs " + color(state)}>
                                            {label(state)}
                                        </span>
                                    </div>
                                    {elapsed ? (
                                        <span className="font-ahamono text-xs tabular-nums text-tiilt-muted">
                                            {elapsed}
                                            {typeof meta.percent === "number"
                                                ? ` · ${Math.round(meta.percent)}%`
                                                : ""}
                                            {fmtEta(meta)
                                                ? ` · ${fmtEta(meta)}`
                                                : ""}
                                        </span>
                                    ) : null}
                                </div>
                                {typeof meta.percent === "number" ? (
                                    <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-tiilt-ground">
                                        <div
                                            className={
                                                "h-full rounded-full transition-all " +
                                                (state === "error"
                                                    ? "bg-tiilt-danger"
                                                    : state === "done"
                                                      ? "bg-tiilt-teal"
                                                      : "bg-tiilt")
                                            }
                                            style={{
                                                width: `${Math.max(2, Math.min(100, meta.percent))}%`,
                                            }}
                                        />
                                    </div>
                                ) : state === "running" ? (
                                    <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-tiilt-ground">
                                        <div className="h-full w-1/3 animate-pulse rounded-full bg-tiilt/60" />
                                    </div>
                                ) : null}
                                {meta.message ? (
                                    <div className="mt-1 text-xs text-tiilt-muted">
                                        {meta.message}
                                    </div>
                                ) : null}
                            </div>
                        )
                    })}
                    {Object.values(streams).length > 0 &&
                    Object.values(streams).every((s) => s === "done") ? (
                        <div className="text-xs text-tiilt-muted">
                            Reload the page to see the updated analytics.
                        </div>
                    ) : null}
                </div>
            ) : (
                <></>
            )}
        </div>
    )
}

export { PosthocTrigger }
