import { useReducer, useEffect, useState, useRef, useCallback } from "react"
import { POD_ON_COLOR as POD_COLOR } from "../components/pod-colors"
import { useNavigate, useParams } from "react-router-dom"
import { SessionService } from "../services/session-service"
import { ByodJoinPage } from "./html-pages"
import { dimsForMode, SESSION_FACING } from "./device-check"
import {
    neededRotation,
    correctedStream,
    rotationForMode,
} from "./orientation-correct"
import { deriveJoinPhase } from "./join-machine"
import { SessionModel } from "../models/session"
import { SessionDeviceModel } from "../models/session-device"
import { SpeakerModel } from "../models/speaker"
import { ApiService, setClientProcessingKey } from "../services/api-service"
import { AuthService } from "../services/auth-service"
import { PolarConnection, isBluetoothSupported } from "../services/polar-hr"
import fixWebmDuration from "fix-webm-duration"
import { FEATURE_LABELS, BOX_LABELS, buildChecklist } from "../utilities/checklist"
import { ensureGetUserMedia } from "../utilities/media"
import { backoffDelay, mergeById, maxId, startPolling } from "../globals"

// Reconnect policy (audit G.1 / E.2): exponential backoff with full jitter
// 1 s -> 30 s, giving up after ~3 min (an audio-processor restart is
// ~70 s). One attempt may hang for at most RECONNECT_ATTEMPT_TIMEOUT_MS.
const RECONNECT_BASE_MS = 1000
const RECONNECT_MAX_MS = 30000
const RECONNECT_DEADLINE_MS = 3 * 60 * 1000
const RECONNECT_ATTEMPT_TIMEOUT_MS = 25000
// Backpressure caps: past these a chunk is dropped (and counted) instead
// of joining a socket queue that only exists in this tab's memory.
const AUDIO_SEND_CAP_BYTES = 1 * 1024 * 1024
const VIDEO_SEND_CAP_BYTES = 8 * 1024 * 1024
// Audio captured while the sockets are down is kept (bounded) and re-sent
// with its offset on reconnect: ~3 min of 16 kHz float32 mono.
const AUDIO_BACKLOG_CAP_BYTES = 12 * 1024 * 1024
// Live poll: 2 s keeps the pod screen live; with after_id most responses
// are empty. A full re-fetch every FULL_REFRESH_EVERY polls picks up rows
// changed after insertion (speaker metrics attach later).
const LIVE_POLL_MS = 2000
const FULL_REFRESH_EVERY = 15

/*
BYOD Connection Order

1. VerifyInputAndAudio
2. RequestAccessKey
3. ConnectToProcessors
4. DetermineSpeakers*
5. requestStartToProcessing
6. Media Socket Worklets
*/

function JoinPage() {
    const sessionService = new SessionService()
    const apiService = new ApiService()
    // Audio connection data
    const audiows = useRef(null)
    const videows = useRef(null)
    const streamReference = useRef(null)
    // Camera stream as delivered by getUserMedia, kept separately when the
    // recorded stream is the rotated-canvas correction of it (sideways
    // phone under rotation lock) — both need stopping on leave.
    const rawStreamReference = useRef(null)
    const orientationFix = useRef(null)
    // Orientation mode chosen in the device-check preview, captured at
    // join time for handleStream. Default "wide": ask the camera itself
    // for a landscape frame — nothing between the camera and the recorder.
    // ("auto" = gravity heuristic, which can put the rotation canvas in
    // the record path; see the watchdog in videoPlay.)
    const orientationMode = useRef("wide")
    // MediaRecorder options, kept so the watchdog can rebuild the recorder
    // on the raw camera stream if the rotated-canvas source stalls.
    const recorderOptions = useRef(null)
    const audioContext = useRef(null)
    const mediaRecorder = useRef(null)
    const source = useRef(null)
    const ending = useRef(false)
    // Reconnect episode: attempt count, when it started (deadline), the
    // pending retry timer and the per-attempt watchdog.
    const reconnect = useRef({ attempt: 0, since: null, timer: null, watchdog: null })
    // The audio socket came up at least once this join. A ref, not a
    // per-socket local: a retry whose handshake fails must keep retrying,
    // not fall to the "couldn't reach the server" dead end.
    const everOpened = useRef(false)
    // Kept across reconnects with the AudioContext; frames flow through
    // sendAudioFrame, which queues them in the backlog while disconnected.
    const workletNode = useRef(null)
    const audioBacklog = useRef({ chunks: [], bytes: 0, drainTimer: null })
    // Mirrors of state for socket callbacks / the worklet (state there is a
    // stale snapshot).
    const streamingRef = useRef(false)
    const readyRef = useRef(false)
    const dropStats = useRef({ audio: 0, video: 0, lastAt: 0, noticeAt: 0 })
    // after_id cursors for the live polls.
    const lastTranscriptId = useRef(0)
    const lastVideoMetricId = useRef(0)
    const frameBuffer = useRef([]); // Buffer for cartoonized frames
    const playbackIntervalRef = useRef(null);
    const isPlayingBatchRef = useRef(false);
    const key = useRef(null)
    const transcripts = useRef([])
    const videoMetrics = useRef([])
    const timeRange = useRef([0, 1])
    const joinwith = useRef("")
    const name = useRef("")
    // The speaker roster lives in a ref (socket callbacks and the replay
    // read it without re-binding) mirrored into state for rendering. All
    // mutations go through setSpeakers so the two can never drift.
    const speakers = useRef([])
    const [roster, setRoster] = useState([])
    const setSpeakers = (next) => {
        speakers.current = next
        setRoster(next)
    }
    const numSpeakers = useRef(0)
    const currBlob = useRef(null)
    const heartbeatIntervalRef = useRef(null)




    // Cartoonification buffer and streaming states
    const [frameBufferLength, setFrameBufferLength] = useState(0);
    const [cartoonImgUrl, setCartoonImgUrl] = useState("");
    const [cartoonImgBatch, setCartoonImgBatch] = useState(1);

    // UI states
    const [sessionDevice, setSessionDevice] = useState(null)
    const [session, setSession] = useState(null)
    const [startTime, setStartTime] = useState(0)
    const [endTime, setEndTime] = useState(0)
    const [displayTranscripts, setDisplayTranscripts] = useState([])
    const [displayVideoMetrics, setDisplayVideoMetrics] = useState([])
    const [currentTranscript, setCurrentTranscript] = useState({})
    const [selectedSpkrId1, setSelectedSpkrId1] = useState(-1)
    const [selectedSpkrId2, setSelectedSpkrId2] = useState(-1)
    const [spkr1Transcripts, setSpkr1Transcripts] = useState([])
    const [spkr2Transcripts, setSpkr2Transcripts] = useState([])
    const [spkr1VideoMetrics, setSpkr1VideoMetrics] = useState([])
    const [spkr2VideoMetrics, setSpkr2VideoMetrics] = useState([])
    const [details, setDetails] = useState("Group")
    const [currentForm, setCurrentForm] = useState("")
    // Watchdog: "Connecting..." must never spin forever. If the join hasn't
    // progressed (socket handshake, permissions, anything) within 30s, fail
    // visibly instead of leaving the user staring at the spinner.
    useEffect(() => {
        if (currentForm !== "Connecting") return
        const t = setTimeout(() => {
            // A reconnect episode has its own deadline (scheduleReconnect).
            if (reconnect.current.since !== null) return
            setDisplayText(
                "Couldn't connect to the session. Check your internet connection and microphone permission, then try again.",
            )
            setCurrentForm("JoinError")
            disconnect(true)
        }, 30000)
        return () => clearTimeout(t)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [currentForm])
    const [displayText, setDisplayText] = useState("")
    const [pageTitle, setPageTitle] = useState("Join Session")
    const [prevSessionId, setPrevSessionId] = useState(-1)
    const [pcode, setPcode] = useState("")
    // Yellkey-style join links: /join/CODE (or /join?code=CODE) prefills the
    // passcode so instructors can share a single URL.
    const { joinCode } = useParams()
    useEffect(() => {
        const fromQuery = new URLSearchParams(window.location.search).get("code")
        const code = joinCode || fromQuery
        if (code) setPcode(code.toUpperCase().slice(0, 16))
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [joinCode])
    const [constraintObj, setConstraintObj] = useState(null)
    const [mimetype, setMimeType] = useState(null)
    const [mimeExtension, setMimeExtension] = useState(null);
    const [wrongInput, setWrongInput] = useState(false)
    // Camera preview defaults ON for video joins so the group can see what
    // is being recorded; the header Options dialog can still hide it.
    const [preview, setPreview] = useState(true)
    const [previewLabel, setPreviewLabel] = useState("Turn Off Preview")
    const [showFeatures, setShowFeatures] = useState([])
    const [showBoxes, setShowBoxes] = useState([])
    const [selectedSpeaker, setSelectedSpeaker] = useState(null)
    const [fingerprintRecordError, setFingerprintRecordError] = useState("")

    // const [sessionClosing, setSessionClosing] = useState(false)

    // Audio/video Fingerprint registration states

    // Pre-join device check: pending join params while the check page is
    // shown, and the confirmed device/channel selection used for capture.
    const deviceSelection = useRef({ audioDeviceId: null, videoDeviceId: null, channelIndex: null })
    // Continuously mirrored by the join form's InlineDeviceCheck (the old
    // separate "Check your devices" page is folded into the form).
    const inlineSelection = useRef({})
    // Deferred connection: Connect-to-server only does the REST join. The
    // media constraints/mimetype computed at that point wait here, and
    // fingerprints recorded on the speaker page queue here; confirming the
    // roster opens the devices + sockets and replays the queue. Until then
    // NOTHING is capturing or connected, so backing out is trivially safe.
    const pendingMedia = useRef(null)
    const pendingFingerprints = useRef([])
    const replaying = useRef(false)
    // Latch: fingerprints replay at most once per connection (the socket
    // flags can flip more than once while both channels come up).
    const replayDone = useRef(false)

    // Recording is started explicitly: after speaker validation the client
    // holds (heartbeating) until "Start recording" is pressed. armed stays
    // true across transient reconnects so recording resumes.
    const [armed, setArmed] = useState(false)
    // Seconds actually streamed (ticks only while streaming is live, so it
    // doubles as visible confirmation that data is flowing). The tick
    // effect lives below the reducer declaration.
    const [recSeconds, setRecSeconds] = useState(0)

    // Polar heart-rate straps (Web Bluetooth), one connection per speaker.
    // polarInfo drives the speaker-card UI; raw samples buffer in a ref and
    // flush to the server in batches so a strap can't spam the network.
    const polarConns = useRef({})
    const hrBuffer = useRef([])
    const [polarInfo, setPolarInfo] = useState({})
    // Advanced-options toggle: when on, each speaker card on the
    // participants page shows a Polar H10 ID field + pair button.
    const [polarEnabled, setPolarEnabled] = useState(false)
    // Live video analytics (gaze/emotion/attention) per pod, default on.
    // Mirrored into a ref because the value is read when the video socket's
    // start message is built, long after the join form set it.
    const [liveAnalytics, setLiveAnalyticsState] = useState(true)
    // Server-side watchdog verdicts (silent video / upload lagging): the
    // recording looks fine from the pod, so the pod's own screen is the
    // only place a warning reaches the people who can act on it.
    const [streamWarning, setStreamWarning] = useState(null)
    // Small non-blocking notices: chunks dropped for backpressure, and the
    // audio server reporting degraded transcription (asr_status).
    const [netNotice, setNetNotice] = useState(null)
    const [asrNotice, setAsrNotice] = useState(null)
    // iOS pins the capture shape to the WINDOW orientation, not the phone's
    // physical one: with the portrait rotation lock on, a phone propped
    // sideways still delivers an upright portrait crop — the wide field of
    // view is gone at capture and nothing downstream can restore it
    // (session 412, pods 1310/1311). So in "wide" mode the Start-recording
    // tap gates on the track actually being landscape-shaped, holding with
    // this prompt until it is (or the group explicitly picks portrait).
    const [rotatePrompt, setRotatePrompt] = useState(false)
    const rotateGateActive = useRef(false)
    const portraitOverride = useRef(false)
    const liveAnalyticsRef = useRef(true)
    const setLiveAnalytics = (v) => {
        liveAnalyticsRef.current = v
        setLiveAnalyticsState(v)
    }

    const navigate = useNavigate()

    // Reducer and state for managing connection and streaming status
    const initialState = {
        audioSocketOpen: false,
        videoSocketOpen: false,
        audioReady: false,
        videoReady: false,
        speakersValidated: false,
        startDiscussionStreaming: false,
    };

    function reducer(state, action) {
        switch (action.type) {
            case "AUDIO_SOCKET_OPEN":
                return { ...state, audioSocketOpen: action.payload };
            case "VIDEO_SOCKET_OPEN":
                return { ...state, videoSocketOpen: action.payload };
            case "AUDIO_READY":
                return { ...state, audioReady: action.payload };
            case "VIDEO_READY":
                return { ...state, videoReady: action.payload };
            case "SPEAKERS_VALIDATED":
                return { ...state, speakersValidated: action.payload };
            case "START_STREAMING":
                return { ...state, startDiscussionStreaming: action.payload };
            case "STOP_STREAMING":
                return { ...state, startDiscussionStreaming: false };
            default:
                return state;
        }
    }
    const [state, dispatch] = useReducer(reducer, initialState);

    // Single entry point for the forward-progress transitions (join-machine
    // step 3). Routing them through one named function centralizes what used
    // to be scattered dispatch() calls and gives one place to add the legal-
    // transition guard and logging as the migration continues. Teardown and
    // the streaming-start decision still dispatch directly for now.
    const JOIN_EVENTS = {
        audio_socket_open: { type: "AUDIO_SOCKET_OPEN", payload: true },
        video_socket_open: { type: "VIDEO_SOCKET_OPEN", payload: true },
        audio_ready: { type: "AUDIO_READY", payload: true },
        video_ready: { type: "VIDEO_READY", payload: true },
        speakers_validated: { type: "SPEAKERS_VALIDATED", payload: true },
    }
    const next = (event) => {
        const action = JOIN_EVENTS[event]
        if (!action) {
            console.warn("join-machine: unknown transition", event)
            return
        }
        dispatch(action)
    }

    useEffect(() => {
        if (!state.startDiscussionStreaming) return
        const t = setInterval(() => setRecSeconds((s) => s + 1), 1000)
        return () => clearInterval(t)
    }, [state.startDiscussionStreaming])

    // Every channel ready (start acks in) ends a reconnect episode: the
    // attempt count and deadline reset for the next one.
    useEffect(() => {
        const isVideo =
            joinwith.current === "Video" || joinwith.current === "Videocartoonify"
        const ready = state.audioReady && (!isVideo || state.videoReady)
        readyRef.current = ready
        if (ready) cancelReconnect()
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [state.audioReady, state.videoReady])

    // Streaming resumed after a reconnect: ship the audio captured while
    // the sockets were down before any live frame (declared before the
    // worklet effect below so the ref is set when that effect runs).
    useEffect(() => {
        streamingRef.current = state.startDiscussionStreaming
        if (state.startDiscussionStreaming) drainAudioBacklog()
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [state.startDiscussionStreaming])

    // Mic watchdog: iOS silently mutes the mic track on audio-route changes
    // (AirPods connecting, Siri, calls) — recording continues but captures
    // silence. Watch the live level and the track's mute events, and surface
    // it, so the group knows the moment their audio stops being captured.
    const [micSilent, setMicSilent] = useState(false)
    useEffect(() => {
        if (!state.startDiscussionStreaming) {
            setMicSilent(false)
            return
        }
        const ctx = audioContext.current
        const src = source.current
        if (!ctx || !src) return
        let raf = null
        let silentSince = null
        let analyser
        try {
            analyser = ctx.createAnalyser()
            analyser.fftSize = 512
            src.connect(analyser)
        } catch (ex) {
            console.error("mic watchdog unavailable", ex)
            return
        }
        const buf = new Float32Array(analyser.fftSize)
        const tick = () => {
            analyser.getFloatTimeDomainData(buf)
            let sum = 0
            for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i]
            const rms = Math.sqrt(sum / buf.length)
            const now = performance.now()
            // A dead/muted track produces exact zeros; even a quiet room
            // gives noticeably more than this.
            if (rms < 0.0001) {
                if (silentSince === null) silentSince = now
                if (now - silentSince > 10000) setMicSilent(true)
            } else {
                silentSince = null
                setMicSilent(false)
            }
            raf = requestAnimationFrame(tick)
        }
        tick()
        const track = streamReference.current?.getAudioTracks?.()[0]
        const onMute = () => setMicSilent(true)
        const onUnmute = () => {
            silentSince = null
            setMicSilent(false)
        }
        track?.addEventListener?.("mute", onMute)
        track?.addEventListener?.("unmute", onUnmute)
        return () => {
            if (raf) cancelAnimationFrame(raf)
            try { src.disconnect(analyser) } catch { /* context may be closed */ }
            track?.removeEventListener?.("mute", onMute)
            track?.removeEventListener?.("unmute", onUnmute)
        }
    }, [state.startDiscussionStreaming])

    const interval = 10000

    // Refs, not per-render `let`s: disconnect runs from a later render whose
    // `let wakeLock` binding was always null, so the lock was never released
    // — and each acquire added another permanent visibilitychange listener
    // that re-requested the lock forever after leaving the session.
    const wakeLock = useRef(null)
    const wakeLockVisListener = useRef(null)

    // FIRST EFFECT THAT RENDERS THE PAGE WITH METRIC OPTIONS INITIALIZATION
    useEffect(() => {
        setShowFeatures(buildChecklist(FEATURE_LABELS))
        setShowBoxes(buildChecklist(BOX_LABELS))
    }, [])


    // SECOND LEVEL: THIS IS TRIGGERED WHEN THE USER CLICKS ON THE JOIN BUTTON AND TRIGGERS THE VALIDATION OF THE INPUTS AND AUDIO DEVICES. THIS THEN TRIGGERS THE REQUEST FOR THE ACCESS KEY FROM THE SERVER
    useEffect(() => {
        if (constraintObj !== null && mimetype !== null && pcode !== "" && joinwith.current !== "") handleStreamRef.current()
    }, [constraintObj, pcode, mimetype])


   // THIRD LEVEL: THIS EFFECT IS TRIGGERED ONCE THE CONNECTION TO THE AUDIO AND VIDEO WEBSOCKET SERVERS ARE OPENED. THIS THEN TRIGGERS THE START OF THE AUDIO AND VIDEO PROCESSING BY SENDING A MESSAGE TO THE SERVER TO START THE PROCESSING
    useEffect(() => {
        if (joinwith.current === "Audio" && state.audioSocketOpen) {
            requestStartAudioProcessing()
            // The roster was confirmed before any socket existed: replay the
            // queued fingerprints right behind the start message (the
            // service processes each connection in order) and validate.
            replayFingerprintsAndValidate()
        }
        if ((joinwith.current === "Video" || joinwith.current === "Videocartoonify") && state.audioSocketOpen && state.videoSocketOpen) {
            requestStartAudioProcessing()
            requestStartVideoProcessing()
            replayFingerprintsAndValidate()
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [state.audioSocketOpen, state.videoSocketOpen])

    //FOURTH LEVEL: THIS IS TRIGGERED ONCE AUDIO AND VIDEO IS OPEN AND READY AND WHEN THE SPEAKERS ARE ENROLLING THEIR FINGERPRINTS, 
    // THIS THEN STARTS THE HEARTBEAT TO KEEP THE CONNECTION TO THE AUDIO AND VIDEO WEBSOCKET SERVERS ALIVE UNTIL THE SPEAKERS ARE VALIDATED, ONCE VALIDATED, THE HEARTBEAT STOPS AND THE STREAMING STARTS
    useEffect(() => {
        const clearHeartbeat = () => {
            if (heartbeatIntervalRef.current) {
                clearInterval(heartbeatIntervalRef.current);
                heartbeatIntervalRef.current = null;
            }
        };

        const sendAudioHeartbeat = () => {
            if (audiows.current?.readyState === WebSocket.OPEN) {
                audiows.current.send(
                    JSON.stringify({ type: "heartbeat", key: key.current })
                );
            } else {
                clearHeartbeat();
            }
        };

        const sendAudioVideoHeartbeat = () => {
            if (
                audiows.current?.readyState === WebSocket.OPEN &&
                videows.current?.readyState === WebSocket.OPEN
            ) {
                audiows.current.send(
                    JSON.stringify({ type: "heartbeat", key: key.current })
                );
                videows.current.send(
                    JSON.stringify({ type: "heartbeat", key: key.current })
                );
            } else {
                clearHeartbeat();
            }
        };

        // Once validation is complete, streaming waits for the explicit
        // "Start recording" press (armed). Until then keep heartbeating so
        // nginx/servers don't drop the idle sockets.
        if (state.speakersValidated) {
            if (!armed) {
                if (joinwith.current === "Audio") {
                    sendAudioHeartbeat()
                    heartbeatIntervalRef.current = setInterval(sendAudioHeartbeat, 20000)
                } else {
                    sendAudioVideoHeartbeat()
                    heartbeatIntervalRef.current = setInterval(sendAudioVideoHeartbeat, 20000)
                }
                return () => clearHeartbeat()
            }
            if (joinwith.current === "Audio") {
                dispatch({ type: "START_STREAMING", payload: (state.audioReady && state.audioSocketOpen  && state.speakersValidated) })
            } else if (joinwith.current === "Video" || joinwith.current === "Videocartoonify") {
                dispatch({ type: "START_STREAMING", payload: (state.audioReady && state.videoReady && state.audioSocketOpen && state.videoSocketOpen && state.speakersValidated) })
            }
            clearHeartbeat();
            return;
        }

        if (joinwith.current === "Audio") {
            if (state.audioSocketOpen && state.audioReady) {
                setCurrentForm("");
                sendAudioHeartbeat(); // send immediately
                heartbeatIntervalRef.current = setInterval(sendAudioHeartbeat, 20000);

            }

        } else if (joinwith.current === "Video" || joinwith.current === "Videocartoonify") {
            if (state.audioSocketOpen && state.videoSocketOpen && state.audioReady && state.videoReady) {
                setCurrentForm("");
                sendAudioVideoHeartbeat(); // send immediately
                heartbeatIntervalRef.current = setInterval(sendAudioVideoHeartbeat, 20000);
            }
        }

        return () => {
            clearHeartbeat();
        };
        // }
    }, [state.audioSocketOpen, state.videoSocketOpen, state.audioReady, state.videoReady, state.speakersValidated, armed]);




    // Between validation and Start recording, show the camera on the live
    // screen's preview so the group can frame the shot before recording.
    useEffect(() => {
        if (
            state.speakersValidated &&
            !state.startDiscussionStreaming &&
            (joinwith.current === "Video" || joinwith.current === "Videocartoonify")
        ) {
            // Never grab the orientation pipeline's hidden decoder element —
            // assigning the preview stream to it starves the rotation canvas.
            const video = document.querySelector(
                "video:not([data-orient-decoder])",
            )
            if (video && streamReference.current && !video.srcObject) {
                video.srcObject = streamReference.current
                video.play().catch(() => {})
            }
        }
    }, [state.speakersValidated, state.startDiscussionStreaming])

    //FIFTH LEVEL: THIS IS TRIGGERED ONCE THE SPEAKERS ARE VALIDATED, THIS THEN STARTS THE STREAMING OF AUDIO AND VIDEO DATA TO THE SERVERS BY CONNECTING
    // THE AUDIO NODES TO THE AUDIO WORKLET PROCESSOR AND STARTING THE MEDIA RECORDER FOR VIDEO
    useEffect(() => {
        if (state.startDiscussionStreaming) {
            const loadWorklet = async () => {
                // iOS Safari starts AudioContexts created outside a direct
                // user gesture in the "suspended" state — the worklet then
                // never processes a frame and no audio reaches the server.
                try {
                    await audioContext.current.resume()
                } catch (ex) {
                    console.error("audio context resume failed", ex)
                }
                // Reconnects keep the AudioContext and its worklet (the
                // graph below is built once per join); frames queued while
                // the sockets were down drain first.
                if (workletNode.current !== null) {
                    drainAudioBacklog()
                    return
                }
                // Absolute path: a relative one resolves under /join/<code>
                // on deep links, 404s into the SPA fallback HTML, and the
                // worklet silently never loads — no audio ever reached the
                // server from QR/passcode joins.
                await audioContext.current.audioWorklet.addModule(
                    "/audio-sender-processor.js",
                )
                const workletProcessor = new AudioWorkletNode(
                    audioContext.current,
                    "audio-sender-processor",
                )
                workletNode.current = workletProcessor
                // 4096 float32 samples per message (~4/s at 16 kHz).
                workletProcessor.port.onmessage = (data) =>
                    sendAudioFrame(data.data.buffer)
                // When a specific channel was picked on the device check
                // page, feed only that channel to the sender (the worklet
                // forwards its first input channel); otherwise send the
                // source as-is (browser default mix).
                const ch = deviceSelection.current?.channelIndex
                if (ch !== null && ch !== undefined && source.current.channelCount > 1) {
                    const splitter = audioContext.current.createChannelSplitter(
                        source.current.channelCount,
                    )
                    source.current.connect(splitter)
                    splitter.connect(workletProcessor, Math.min(ch, source.current.channelCount - 1), 0)
                    workletProcessor.connect(audioContext.current.destination)
                } else {
                    source.current.connect(workletProcessor).connect(audioContext.current.destination)
                }
            }

            const videoPlay = () => {
                // Exclude the orientation pipeline's hidden decoder element:
                // pointing the preview at it would starve the canvas it feeds.
                let video =
                    document.querySelector("video:not([data-orient-decoder])") ||
                    document.querySelector("video")
                video.srcObject = streamReference.current
                let started = false
                let gotChunk = false
                const begin = () => {
                    // The 3s backstop can fire after disconnect nulled the
                    // recorder — that was an uncaught TypeError in a timer.
                    if (started || mediaRecorder.current === null) return
                    started = true
                    video.play().catch(() => {})
                    mediaRecorder.current.start(interval)
                    armVideoWatchdog()
                }
                // The pre-start preview may have attached this stream
                // already; loadedmetadata won't refire then. The timer is the
                // backstop: a canvas-sourced stream that never delivers a
                // frame never fires loadedmetadata either, and recording
                // must not hinge on that event arriving.
                if (video.readyState >= 1) {
                    begin()
                } else {
                    video.onloadedmetadata = begin
                    setTimeout(begin, 3000)
                }

                // If the recorder produces NO chunk at all, the source is
                // dead — the rotated-canvas path has done this repeatedly on
                // real hardware (pod 1221's one-byte file, pods 1244/1245
                // and 1262/1263 recording nothing while their previews
                // looked normal). Rebuild on the raw camera stream:
                // sideways but complete beats upright but empty.
                function armVideoWatchdog() {
                    // Bind to the recorder being watched: a reconnect within
                    // the watchdog window builds a NEW recorder, and the old
                    // timer must not stop the healthy new one.
                    const armedRecorder = mediaRecorder.current
                    setTimeout(() => {
                        if (gotChunk || !orientationFix.current) return
                        if (mediaRecorder.current === null || mediaRecorder.current !== armedRecorder) return
                        const raw = rawStreamReference.current
                        if (!raw || !recorderOptions.current) return
                        console.error(
                            "no video data after start — falling back to the raw camera stream",
                        )
                        try {
                            mediaRecorder.current.stop()
                        } catch (e) {
                            console.warn("watchdog: recorder stop failed", e)
                        }
                        try {
                            orientationFix.current.stop()
                        } catch (e) {
                            console.warn("watchdog: canvas stop failed", e)
                        }
                        orientationFix.current = null
                        streamReference.current = raw
                        video.srcObject = raw
                        video.play().catch(() => {})
                        const rec = new MediaRecorder(raw, recorderOptions.current)
                        mediaRecorder.current = rec
                        rec.ondataavailable = onVideoChunk
                        rec.start(interval)
                    }, Math.max(8000, interval * 2))
                }

                const onVideoChunk = async function (ev) {
                    gotChunk = true

                    await ev.data.arrayBuffer()

                    if (ev.data && ev.data.size !== 0) {
                        if (ev.data.type.startsWith('video/webm')) {
                            fixWebmDuration(
                                ev.data,
                                interval * 6 * 60 * 24,
                                (fixedblob) => sendVideoChunk(fixedblob),
                            )
                        } else if (ev.data.type.startsWith('video/mp4')) {
                            // Sent as-is: the server-side remux cache fixes
                            // duration/cues on playback. (An MP4Box-based
                            // client-side "repair" used to live here; it had
                            // never worked — its dependency was never loaded
                            // — and silently dropped every mp4 chunk.)
                            sendVideoChunk(ev.data)
                        }

                    }

                }
                mediaRecorder.current.ondataavailable = onVideoChunk
            }

            if (joinwith.current === "Audio") {
                loadWorklet().catch(console.error)
            } else if (joinwith.current === "Video" || joinwith.current === "Videocartoonify") {
                loadWorklet().catch(console.error)
                videoPlay()
            }
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [state.startDiscussionStreaming])


    // SIXTH LEVEL: THIS EFFECT IS TRIGGERED ONCE THE SPEAKERS ARE VALIDATED AND THE STREAMING HAS STARTED, THIS THEN STARTS THE INTERVAL 
    // TO FETCH THE TRANSCRIPTS AND VIDEO METRICS FROM THE SERVER EVERY 2 SECONDS AND UPDATE THE DISPLAY
    useEffect(() => {
        // Gate on streaming: this used to poll every 2s the whole time the
        // group idled on the (offline-by-design) speaker page, where no
        // transcripts can exist yet. Chained (never overlapping),
        // incremental via after_id, paused while hidden, backed off on
        // errors, aborted on cleanup (audit G.2).
        if (session === null || sessionDevice === null || !state.startDiscussionStreaming) {
            return undefined
        }
        const deviceId = sessionDevice.id
        let polls = 0
        return startPolling(async (signal) => {
            const full = polls++ % FULL_REFRESH_EVERY === 0
            const [a, b] = await Promise.all([
                fetchTranscript(deviceId, signal, full),
                fetchVideoMetric(deviceId, signal, full),
            ])
            return a !== false && b !== false
        }, LIVE_POLL_MS)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [session, sessionDevice, state.startDiscussionStreaming])

    //SIXTH LEVEL: THIS EFFECT IS TRIGGERED ONCE THE TRANSCRIPTS AND VIDEO METRICS ARE FETCHED, 
    // THIS THEN GENERATES THE DISPLAY TRANSCRIPTS AND VIDEO METRICS BASED ON THE SELECTED TIME RANGE AND UPDATES THE DISPLAY
    useEffect(() => {
        if (session !== null && state.startDiscussionStreaming) {
            const sessionLen =
                Object.keys(session).length > 0 ? session.length : 0
            const sTime = Math.round(sessionLen * timeRange.current[0] * 100) / 100
            const eTime = Math.round(sessionLen * timeRange.current[1] * 100) / 100
            setStartTime(sTime)
            setEndTime(eTime)
            generateDisplayTranscripts(sTime, eTime)
            generateDisplayVideoMetrics(sTime, eTime)
        }
    }, [startTime, endTime, session, state.startDiscussionStreaming, timeRange])


    // SIXTH LEVEL: THIS EFFECT IS TRIGGERED ONCE THE USER SELECTS A SPEAKER TO VIEW THEIR TRANSCRIPTS AND VIDEO METRICS, 
    // THIS THEN UPDATES THE DISPLAY TO SHOW THE TRANSCRIPTS AND VIDEO METRICS FOR THE SELECTED SPEAKER
    useEffect(() => {
        if (displayTranscripts) {
            setSpeakerTranscripts()
        }
        if (displayVideoMetrics) {
            setSpeakerVideoMetrics()
        }
    }, [displayTranscripts, displayVideoMetrics, selectedSpkrId1, selectedSpkrId2, details])


    //EIGHTH LEVEL: THIS EFFECT IS TRIGGERED ONCE THE PREVIEW MODE IS TOGGLED, THIS THEN UPDATES THE LABEL FOR THE PREVIEW TOGGLE BUTTON
    useEffect(() => {
        if (preview) {
            setPreviewLabel("Turn Off Preview")
        } else {
            setPreviewLabel("Turn On Preview")
        }
    }, [preview])

    const openForms = (form, speaker = null) => {
        setCurrentForm(form)
        if (form === "fingerprintAudio") {
            setSelectedSpeaker(speaker)
            currBlob.current = null
            setFingerprintRecordError("")
        }
    }

    // Disconnects from websocket server and audio stream. keepMedia (the
    // transient reconnect path) drops only the sockets and the recorder:
    // the microphone/camera, AudioContext, worklet and wake lock stay up,
    // so a reconnect needs no new permission prompt and loses no audio.
    const disconnect = (permanent = false, { keepMedia = false } = {}) => {
        if (ending.current)
            return
        if (permanent && session !== null) {
            setPageTitle("Join Session")
            // Keep name.current and the passcode: a session ending must not
            // log the pod out — the device returns to the join screen with
            // its identity intact so it can rejoin the next session directly.
            ending.current = true
            setArmed(false)
            setStreamWarning(null)
            // The rotate-gate loop notices the stopped track and exits on
            // its own; dropping the overlay here just makes it immediate.
            setRotatePrompt(false)
            setRecSeconds(0)
            dispatch({ type: "SPEAKERS_VALIDATED", payload: false })
            setSpeakers(null)
            setPrevSessionId(session.id)
            setSession(null)
            setSessionDevice(null)
            key.current = null
            setClientProcessingKey(null)
        }

        if (permanent) {
            Object.values(polarConns.current).forEach((c) => c.close())
            polarConns.current = {}
            hrBuffer.current = []
            setPolarInfo({})
            // Cartoonify playback: stop the 33ms interval and free every
            // frame object URL (they are never revoked anywhere else).
            if (playbackIntervalRef.current) {
                clearInterval(playbackIntervalRef.current)
                playbackIntervalRef.current = null
            }
            isPlayingBatchRef.current = false
            frameBuffer.current.forEach((url) => {
                if (url) URL.revokeObjectURL(url)
            })
            frameBuffer.current = []
            setFrameBufferLength(0)
            // Nothing may reconnect after a permanent teardown; the audio
            // backlog and the notices belonged to the connection that ended.
            cancelReconnect()
            everOpened.current = false
            audioBacklog.current = { chunks: [], bytes: 0, drainTimer: null }
            dropStats.current = { audio: 0, video: 0, lastAt: 0, noticeAt: 0 }
            setNetNotice(null)
            setAsrNotice(null)
        }

        if (!keepMedia) {
            releaseWakeLock()

            if (source.current != null) {
                source.current.disconnect()
                source.current = null
            }
            if (audioContext.current != null) {
                audioContext.current.close()
                audioContext.current = null
            }
            workletNode.current = null
        }
        // The recorder always stops: a video fragment is only decodable
        // from its first chunk, so each connection gets a fresh recorder.
        if (mediaRecorder.current != null) {
            // endRecording stops the recorder before draining the socket;
            // stop() on an inactive recorder throws and would abort the
            // rest of this teardown.
            try {
                if (mediaRecorder.current.state !== "inactive") {
                    mediaRecorder.current.stop()
                }
            } catch (ex) {
                console.warn("recorder stop during disconnect failed", ex)
            }
            mediaRecorder.current = null
        }

        if (!keepMedia) {
            if (orientationFix.current != null) {
                orientationFix.current.stop()
                orientationFix.current = null
            }
            if (rawStreamReference.current != null) {
                rawStreamReference.current.getTracks().forEach((track) => track.stop())
                rawStreamReference.current = null
            }
            if (streamReference.current != null) {
                // ALL tracks — stopping only audio left the camera light on
                // after leaving a video pod.
                streamReference.current.getTracks().forEach((track) => track.stop())
                streamReference.current = null
            }
        }

        dispatch({ type: "AUDIO_SOCKET_OPEN", payload: false })
        dispatch({ type: "VIDEO_SOCKET_OPEN", payload: false })
        dispatch({ type: "AUDIO_READY", payload: false })
        dispatch({ type: "VIDEO_READY", payload: false })
        // Streaming must restart from scratch after a reconnect; armed is
        // kept so an in-progress recording resumes without re-pressing Start.
        dispatch({ type: "STOP_STREAMING" })

        // Detach the handlers first: these closes are deliberate, and the
        // other socket's onclose must not start a second reconnect.
        for (const w of [audiows.current, videows.current]) {
            if (w) {
                w.onclose = null
                w.onmessage = null
                w.onerror = null
            }
        }
        if (audiows.current != null) {
            audiows.current.close();
            audiows.current = null;
        }
        if (videows.current != null) {
            videows.current.close()
            videows.current = null
        }
    }

    const cancelReconnect = () => {
        const r = reconnect.current
        clearTimeout(r.timer)
        clearTimeout(r.watchdog)
        r.timer = null
        r.watchdog = null
        r.attempt = 0
        r.since = null
    }

    // Either socket dropped mid-session: drop both, keep the media, retry
    // with backoff until the deadline. Both channels are re-opened
    // together because the fingerprints and roster are replayed to both
    // services on every connection (video alone cannot be re-validated).
    const scheduleReconnect = (why) => {
        const r = reconnect.current
        if (ending.current || r.timer !== null) return
        if (r.since === null) r.since = Date.now()
        clearTimeout(r.watchdog)
        r.watchdog = null
        if (Date.now() - r.since > RECONNECT_DEADLINE_MS) {
            console.error("reconnect deadline passed (" + why + ")")
            setDisplayText("Connection to the session has been lost.")
            setCurrentForm("ClosedSession")
            disconnect(true)
            return
        }
        console.warn("session " + why + " socket closed; reconnect attempt " + (r.attempt + 1))
        disconnect(false, { keepMedia: true })
        // The new server connection starts with no speaker fingerprints —
        // they are all queued client-side, so the reconnect replays them
        // (no bouncing the group back to the speaker page).
        dispatch({ type: "SPEAKERS_VALIDATED", payload: false })
        replayDone.current = false
        setCurrentForm("Connecting")
        const delay = backoffDelay(r.attempt, RECONNECT_BASE_MS, RECONNECT_MAX_MS)
        r.attempt += 1
        r.timer = setTimeout(() => {
            r.timer = null
            if (ending.current) return
            handleStreamRef.current()
            // A handshake or start ack that hangs fires no onclose; force
            // the next attempt instead of stalling the loop.
            r.watchdog = setTimeout(() => {
                r.watchdog = null
                if (readyRef.current || ending.current) return
                console.warn("reconnect attempt timed out; retrying")
                const sockets = [audiows.current, videows.current].filter(Boolean)
                for (const w of sockets) {
                    try {
                        w.close()
                    } catch {
                        /* already closing */
                    }
                }
                // No socket to close (handleStream failed before opening
                // one) means no onclose will come: schedule directly.
                if (sockets.length === 0) scheduleReconnect("timeout")
            }, RECONNECT_ATTEMPT_TIMEOUT_MS)
        }, delay)
    }

    // Seconds of audio queued while the sockets were down. Sent as
    // start_time on the restart message: the server places this stream's
    // first sample that many seconds before "now" (start_offset =
    // now - server_start - start_time), so the re-sent gap lands where it
    // was spoken. 0 on a first connection, exactly as before.
    const audioBacklogSeconds = () => {
        const rate = audioContext.current ? audioContext.current.sampleRate : 16000
        return Math.round((audioBacklog.current.bytes / (4 * rate)) * 100) / 100
    }

    // Drop bookkeeping + the small non-blocking notice (throttled: audio
    // frames arrive ~4x per second).
    const noteDrop = (kind) => {
        const d = dropStats.current
        d[kind] += 1
        const now = Date.now()
        d.lastAt = now
        if (now - d.noticeAt < 2000) return
        d.noticeAt = now
        const n = d.audio + d.video
        setNetNotice(
            "Slow network: " + n + (n === 1 ? " chunk" : " chunks") +
                " of " + kind + " skipped so far. The session continues; " +
                "move closer to the Wi-Fi if this persists.",
        )
    }
    const clearDropNoticeIfQuiet = () => {
        const d = dropStats.current
        if (d.lastAt !== 0 && Date.now() - d.lastAt > 15000) {
            d.lastAt = 0
            setNetNotice(null)
        }
    }

    // Audio frames go straight out while the socket is open, streaming and
    // not backed up. Otherwise (reconnect in progress, or a backlog still
    // draining) they queue in the bounded backlog, oldest dropped and
    // counted past the cap so what remains is contiguous up to now — which
    // is what audioBacklogSeconds relies on.
    const sendAudioFrame = (buf) => {
        const ws = audiows.current
        const backlog = audioBacklog.current
        const live = !!ws && ws.readyState === WebSocket.OPEN && streamingRef.current
        if (live && backlog.chunks.length === 0) {
            if (ws.bufferedAmount > AUDIO_SEND_CAP_BYTES) {
                noteDrop("audio")
                return
            }
            ws.send(buf)
            clearDropNoticeIfQuiet()
            return
        }
        backlog.chunks.push(buf)
        backlog.bytes += buf.byteLength
        while (backlog.bytes > AUDIO_BACKLOG_CAP_BYTES && backlog.chunks.length) {
            backlog.bytes -= backlog.chunks.shift().byteLength
            noteDrop("audio")
        }
        if (live) drainAudioBacklog()
    }

    // Ships the backlog in order, pacing against the send cap.
    const drainAudioBacklog = () => {
        const backlog = audioBacklog.current
        clearTimeout(backlog.drainTimer)
        backlog.drainTimer = null
        const ws = audiows.current
        while (backlog.chunks.length) {
            if (!ws || ws.readyState !== WebSocket.OPEN || !streamingRef.current) return
            if (ws.bufferedAmount > AUDIO_SEND_CAP_BYTES) {
                backlog.drainTimer = setTimeout(drainAudioBacklog, 250)
                return
            }
            const chunk = backlog.chunks.shift()
            backlog.bytes -= chunk.byteLength
            ws.send(chunk)
        }
    }

    // Video chunks: sent unless the socket is backed up past the cap. On a
    // starved uplink the queue would otherwise grow without bound and die
    // with the page; drop and count instead (the notice says so).
    const sendVideoChunk = (blob) => {
        const ws = videows.current
        if (!ws || ws.readyState !== WebSocket.OPEN) return
        if (ws.bufferedAmount > VIDEO_SEND_CAP_BYTES) {
            noteDrop("video")
            return
        }
        ws.send(blob)
        clearDropNoticeIfQuiet()
    }

    // Unmount teardown. Browser back (or any route change) unmounts this
    // component without running navigateToLogin — before this, the started
    // recorder, worklet, and both websockets lived on in closures and the
    // phone kept streaming with the landing page on screen. The ref
    // indirection matters: an []-deps cleanup captures the FIRST render's
    // disconnect closure, whose session/socket state is stale.
    const disconnectRef = useRef(null)
    disconnectRef.current = disconnect
    useEffect(() => {
        return () => {
            // Detach handlers before closing (same reason as navigateToLogin):
            // onclose would mistake this for a mid-session drop and reconnect.
            for (const w of [audiows.current, videows.current]) {
                if (w) {
                    w.onclose = null
                    w.onmessage = null
                    w.onerror = null
                }
            }
            disconnectRef.current(true)
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    // While recording, refresh/tab-close must prompt: besides the current
    // chunk, everything queued in videows.bufferedAmount (the congested-
    // uplink backlog the end-of-recording drain protects) dies with the page.
    useEffect(() => {
        if (!armed) return
        const warn = (e) => {
            e.preventDefault()
            e.returnValue = ""
        }
        window.addEventListener("beforeunload", warn)
        return () => window.removeEventListener("beforeunload", warn)
    }, [armed])

    // Add a speaker slot after joining (needed when the group joined with
    // "detect automatically", i.e. zero pre-created slots).
    // Adds a speaker slot. When a name is given, enrolled speakers default
    // to their saved fingerprint: an exact username match renames the slot
    // and attaches the stored voice print automatically; anyone else just
    // gets the name (fingerprint recordable from the speaker menu).
    const addSpeakerSlot = async (name = "") => {
        if (!sessionDevice) return
        try {
            const response = await sessionService.addSpeaker(sessionDevice.id)
            if (response.status !== 200) return
            const speaker = SpeakerModel.fromJson(await response.json())
            setSpeakers([...(speakers.current || []), speaker])
            if ((name || "").trim()) {
                await attachIdentity(speaker, name)
            }
        } catch (apierror) {
            console.error("byod-join-component func: addSpeakerSlot ", apierror)
        }
    }

    // The one path for putting a typed name on a speaker slot, shared by
    // every entry point (add-with-name, the inline card editor, the dialogs).
    // An enrolled username (biometrics on file) renames the slot to the
    // canonical username, queues the saved-fingerprint attach for replay
    // after the roster is confirmed, and marks the card ready; a registered
    // account WITHOUT biometrics or an unknown name is a plain rename (the
    // red X stays). Returns the outcome so dialog callers can show errors:
    // "enrolled" | "renamed" | "not-enrolled" | "unknown" | "lookup-failed"
    // | "rename-failed" | "empty".
    const attachIdentity = async (speaker, rawName, { requireEnrolled = false } = {}) => {
        const username = (rawName || "").trim()
        if (!username) return "empty"
        let profile = null
        try {
            const resp = await new AuthService().getStudentProfileByID(username)
            if (resp.status === 200) profile = await resp.json()
        } catch (ex) {
            console.error("enrollment lookup failed", ex)
            // Only the enrolled-only path must stop here; a plain rename
            // shouldn't fail because the lookup was unreachable.
            if (requireEnrolled) return "lookup-failed"
        }
        const enrolled = !!(profile && profile.biometric_captured)
        if (requireEnrolled && !enrolled) {
            return profile ? "not-enrolled" : "unknown"
        }
        const alias = enrolled ? profile.username : username
        try {
            const r = await sessionService.updateCollaborator(speaker.id, alias)
            if (r.status !== 200) return "rename-failed"
            const renamed = SpeakerModel.fromJson(await r.json())
            setSpeakers(
                (speakers.current || []).map((s) =>
                    s.id === speaker.id
                        ? {
                              ...s,
                              alias: renamed.alias,
                              fingerprinted: enrolled ? true : s.fingerprinted,
                          }
                        : s,
                ),
            )
        } catch (ex) {
            console.error("speaker rename failed", ex)
            return "rename-failed"
        }
        if (!enrolled) return "renamed"
        // Re-attaching replaces any earlier queued fingerprint for this slot.
        pendingFingerprints.current = pendingFingerprints.current
            .filter((f) => f.id !== speaker.id)
            .concat({ type: "saved", id: speaker.id, alias })
        return "enrolled"
    }

    // Commit from the inline name editor on a speaker card.
    const inlineRenameSpeaker = async (speaker, rawName) => {
        const username = (rawName || "").trim()
        if (!username || username === speaker.alias) return
        await attachIdentity(speaker, username)
    }

    // Remove a mis-added slot. The server refuses once the speaker has any
    // recorded data; here that can only happen after a reconnect mid-session.
    const removeSpeakerSlot = async (speaker) => {
        if (!sessionDevice) return
        try {
            const r = await sessionService.removeSpeaker(sessionDevice.id, speaker.id)
            if (r.status !== 200) {
                console.error("remove speaker refused", r.status)
                return
            }
        } catch (ex) {
            console.error("remove speaker failed", ex)
            return
        }
        pendingFingerprints.current = pendingFingerprints.current.filter(
            (f) => f.id !== speaker.id,
        )
        unassignPolarSensor(speaker)
        setSpeakers((speakers.current || []).filter((s) => s.id !== speaker.id))
    }

    // Pair a Polar strap (or any BLE heart-rate sensor) with a speaker. The
    // browser chooser lists every advertising strap by its printed ID (e.g.
    // "Polar H10 8C0B2A2B"), so picking the strap IS the person↔sensor
    // assignment. Returns "paired" | "dismissed" | "failed" so the card can
    // explain an empty or cancelled chooser instead of silently doing nothing.
    const assignPolarSensor = async (speaker) => {
        if (!isBluetoothSupported()) return "failed"
        if (polarConns.current[speaker.id]) unassignPolarSensor(speaker)
        const conn = new PolarConnection({
            onSample: ({ hr, rr, t }) => {
                // Alias resolved at sample time so a later rename sticks.
                const cur = (speakers.current || []).find((s) => s.id === speaker.id)
                hrBuffer.current.push({
                    speaker_id: speaker.id,
                    alias: (cur && cur.alias) || speaker.alias,
                    sensor: (conn.device && conn.device.name) || "",
                    t,
                    hr,
                    rr,
                })
                setPolarInfo((p) =>
                    p[speaker.id]
                        ? { ...p, [speaker.id]: { ...p[speaker.id], bpm: hr } }
                        : p,
                )
            },
            onStatus: (status) => {
                setPolarInfo((p) =>
                    p[speaker.id]
                        ? { ...p, [speaker.id]: { ...p[speaker.id], status } }
                        : p,
                )
            },
        })
        try {
            const info = await conn.choose()
            polarConns.current[speaker.id] = conn
            setPolarInfo((p) => ({
                ...p,
                [speaker.id]: {
                    name: info.name,
                    battery: info.battery,
                    status: "connected",
                    bpm: null,
                },
            }))
            return "paired"
        } catch (ex) {
            conn.close()
            // NotFoundError just means the chooser was dismissed (possibly
            // because the ID filter matched no strap).
            if (ex && ex.name === "NotFoundError") return "dismissed"
            console.error("polar connect failed", ex)
            return "failed"
        }
    }

    const unassignPolarSensor = (speaker) => {
        const conn = polarConns.current[speaker.id]
        if (conn) conn.close()
        delete polarConns.current[speaker.id]
        setPolarInfo((p) => {
            const next = { ...p }
            delete next[speaker.id]
            return next
        })
    }

    // Drop Polar state that doesn't belong to the roster just returned by a
    // join. A device idling on the enrolling page has no sockets open, so it
    // never hears its old session end — without this, straps paired there
    // showed up as still paired in the next session, and their buffered
    // samples would flush into the new session's data. A reconnect to the
    // same session device returns the same speaker ids, so live straps
    // survive it untouched.
    const prunePolarToRoster = (freshSpeakers) => {
        const ids = new Set((freshSpeakers || []).map((s) => s.id))
        Object.entries(polarConns.current).forEach(([sid, conn]) => {
            if (!ids.has(Number(sid))) {
                conn.close()
                delete polarConns.current[sid]
            }
        })
        setPolarInfo((p) => {
            const next = {}
            Object.keys(p).forEach((sid) => {
                if (ids.has(Number(sid))) next[sid] = p[sid]
            })
            return next
        })
        hrBuffer.current = hrBuffer.current.filter((s) => ids.has(s.speaker_id))
    }

    // Ship buffered heart-rate samples every 5 s while joined. Streaming
    // isn't gated on "Start recording": pre-discussion baseline HR is
    // useful, and timestamps are session-relative server-side either way.
    useEffect(() => {
        if (sessionDevice === null) return undefined
        const timer = setInterval(() => {
            if (!hrBuffer.current.length || !key.current) return
            const batch = hrBuffer.current.splice(0, hrBuffer.current.length)
            sessionService
                .postHeartRateForClient(sessionDevice.id, batch, key.current)
                .catch((ex) => console.error("hr flush failed", ex))
        }, 5000)
        return () => clearInterval(timer)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [sessionDevice])

    // Bulk version for the speaker page's group-size picker (the join form
    // no longer asks for a count; groups size themselves here). Sequential
    // on purpose: parallel adds raced the server's per-device numbering and
    // every slot came back named "Speaker 1".
    const addSpeakerSlots = async (n) => {
        for (let i = 0; i < Math.max(0, Math.min(8, n)); i++) {
            await addSpeakerSlot()
        }
    }

    // Confirming the roster is what actually starts everything: apply the
    // media plan stored at the REST join, which triggers handleStream (get
    // devices, open sockets); once the sockets open, the queued fingerprints
    // replay and validation completes. Until this click, nothing was
    // capturing or connected.
    const confirmSpeakers = () => {
        // The processing services get the group size in the start message;
        // slots are created on this page now, so count the actual roster.
        numSpeakers.current = (speakers.current || []).length
        if (!speakers.current.every((s) => s.fingerprinted)) {
            setDisplayText(
                "Not all added speakers have a fingerprint. Please record one for each speaker",
            )
            setCurrentForm("FingerprintingError")
            return
        }
        const pm = pendingMedia.current
        if (!pm) {
            setDisplayText("The join expired — please rejoin the session.")
            setCurrentForm("JoinError")
            return
        }
        ending.current = false
        replayDone.current = false
        everOpened.current = false
        cancelReconnect()
        setCurrentForm("Connecting")
        setMimeExtension(pm.mediaExt)
        setMimeType(pm.mediaType)
        setConstraintObj(pm.constraint)
    }

    // Replays the queued fingerprints over the freshly opened sockets (in
    // order, right behind the start messages — the services process each
    // connection sequentially) and then validates the roster. Also runs on
    // reconnects, so a dropped connection re-enrolls automatically instead
    // of bouncing the group back to the speaker page.
    const replayFingerprintsAndValidate = async () => {
        if (replaying.current || replayDone.current || audiows.current === null) return
        replaying.current = true
        replayDone.current = true
        try {
            const isVideo =
                joinwith.current === "Video" ||
                joinwith.current === "Videocartoonify"
            for (const item of pendingFingerprints.current) {
                if (item.type === "saved") {
                    const msg = JSON.stringify({
                        type: "add-saved-fingerprint",
                        id: item.id,
                        alias: item.alias,
                    })
                    audiows.current.send(msg)
                    if (isVideo && videows.current) videows.current.send(msg)
                } else {
                    const data = await item.blob.arrayBuffer()
                    const audiodata =
                        await audioContext.current.decodeAudioData(data)
                    audiows.current.send(
                        JSON.stringify({
                            type: "speaker",
                            id: item.id,
                            alias: item.alias,
                            size: item.blob.size,
                            blob_type: item.blob.type,
                        }),
                    )
                    audiows.current.send(audiodata.getChannelData(0))
                }
            }
            setSpeakers(
                (speakers.current || []).map((s) => ({
                    ...s,
                    fingerprinted: true,
                })),
            )
            const done = JSON.stringify({
                type: "speaker",
                id: "done",
                speakers: speakers.current,
            })
            audiows.current.send(done)
            if (isVideo && videows.current) videows.current.send(done)
            next("speakers_validated")
        } catch (ex) {
            console.error("fingerprint replay failed", ex)
        } finally {
            replaying.current = false
        }
    }

    const saveAudioFingerprint = (audioblob) => {
        //store blob for confirmation
        currBlob.current = audioblob
    }

    // Confirm from the record dialog. Queued locally; sent when the
    // connection starts after the roster is confirmed (no sockets exist on
    // the speaker page anymore). Mirrors the enrollment coach's floor: a
    // usable print needs sustained natural speech, so a take shorter than
    // the coach's 10-second minimum is rejected with a retry hint instead
    // of silently producing a fingerprint too thin to match against.
    const MIN_FINGERPRINT_SECONDS = 10
    const addSpeakerFingerprint = async () => {
        const blob = currBlob.current
        if (!blob) {
            setFingerprintRecordError("Record a take first, then confirm.")
            return
        }
        let duration = null
        try {
            const Ctx = window.AudioContext || window.webkitAudioContext
            const probe = new Ctx()
            try {
                const buf = await probe.decodeAudioData(await blob.arrayBuffer())
                duration = buf.duration
            } finally {
                probe.close()
            }
        } catch (ex) {
            // A probe failure must not block joining — length just goes
            // unchecked for this take.
            console.error("fingerprint duration probe failed", ex)
        }
        if (duration !== null && duration < MIN_FINGERPRINT_SECONDS) {
            setFingerprintRecordError(
                `That take was only ${Math.max(1, Math.round(duration))} second${Math.round(duration) === 1 ? "" : "s"} — keep talking for at least ${MIN_FINGERPRINT_SECONDS}.`,
            )
            return
        }
        setFingerprintRecordError("")
        // Re-recording replaces any fingerprint already queued for this slot.
        pendingFingerprints.current = pendingFingerprints.current
            .filter((f) => f.id !== selectedSpeaker.id)
            .concat({
                type: "blob",
                id: selectedSpeaker.id,
                alias: selectedSpeaker.alias,
                blob,
            })
        setSpeakers(
            speakers.current.map((s) =>
                s.id === selectedSpeaker.id ? { ...s, fingerprinted: true } : s,
            ),
        )
        currBlob.current = null
        closeDialog()
    }

    // A processing service reported it couldn't load a saved fingerprint at
    // replay time (after the roster was confirmed — there is no enrollment
    // dialog to reopen anymore). The speaker still exists; their utterances
    // are matched posthoc instead.
    const onSavedFingerprintFailed = (message) => {
        console.warn("saved fingerprint failed:", message)
    }

    // Delivered shape of the camera track feeding the recorder ({w, h},
    // zeros when unknown). Read fresh each time: modern iOS rotates a LIVE
    // track when the phone turns (dimensions swap in place).
    const recordingTrackShape = () => {
        const t = rawStreamReference.current?.getVideoTracks?.()[0]
        const s = t && t.getSettings ? t.getSettings() : {}
        return { w: s.width || 0, h: s.height || 0 }
    }

    // Fallback for pipelines that never rotate an open track: re-open the
    // camera (which adopts the now-landscape window orientation), swap the
    // video track into the live stream, and rebuild the not-yet-started
    // recorder over it. Audio tracks and the on-screen preview are
    // untouched — they ride the same MediaStream object.
    const swapInLandscapeTrack = async () => {
        const videoConstraint = constraintObj && constraintObj.video
        if (!videoConstraint || !recorderOptions.current) return false
        let fresh
        try {
            fresh = await navigator.mediaDevices.getUserMedia({
                video: videoConstraint,
                audio: false,
            })
        } catch (e) {
            console.warn("landscape re-acquire failed:", e)
            return false
        }
        const track = fresh.getVideoTracks()[0]
        const s = track && track.getSettings ? track.getSettings() : {}
        const stream = rawStreamReference.current
        if (!track || !stream || !s.width || !s.height || s.width < s.height) {
            fresh.getTracks().forEach((t) => t.stop())
            return false
        }
        stream.getVideoTracks().forEach((t) => {
            t.stop()
            stream.removeTrack(t)
        })
        stream.addTrack(track)
        try {
            if (mediaRecorder.current.state !== "inactive") {
                mediaRecorder.current.stop()
            }
        } catch (e) {
            console.warn("recorder stop before track swap failed", e)
        }
        mediaRecorder.current = new MediaRecorder(
            stream,
            recorderOptions.current,
        )
        return true
    }

    // The Start-recording tap. Arms immediately except when "wide" mode is
    // about to record a portrait-shaped track — then hold behind the rotate
    // prompt until the track turns landscape: first by waiting (iOS rotates
    // live tracks once the rotation lock is off), then by re-opening the
    // camera when the window says landscape but the track stays tall.
    const startRecordingGate = async () => {
        if (rotateGateActive.current) return
        const isVideo =
            joinwith.current === "Video" ||
            joinwith.current === "Videocartoonify"
        const shape = isVideo ? recordingTrackShape() : { w: 0, h: 0 }
        if (
            !isVideo ||
            orientationMode.current !== "wide" ||
            !shape.h ||
            shape.w >= shape.h
        ) {
            setArmed(true)
            return
        }
        rotateGateActive.current = true
        portraitOverride.current = false
        setRotatePrompt(true)
        let landscapeWindowPolls = 0
        try {
            for (;;) {
                await new Promise((resolve) => setTimeout(resolve, 1000))
                if (portraitOverride.current) break
                const t = rawStreamReference.current?.getVideoTracks?.()[0]
                if (!t || t.readyState === "ended") return // torn down mid-wait
                const { w, h } = recordingTrackShape()
                if (h && w >= h) break
                const windowLandscape =
                    typeof window.matchMedia === "function" &&
                    window.matchMedia("(orientation: landscape)").matches
                landscapeWindowPolls = windowLandscape
                    ? landscapeWindowPolls + 1
                    : 0
                if (landscapeWindowPolls >= 4) {
                    landscapeWindowPolls = 0
                    await swapInLandscapeTrack()
                }
            }
        } finally {
            rotateGateActive.current = false
            setRotatePrompt(false)
        }
        setArmed(true)
    }

    // Whether the media kept across a reconnect is still usable: a dead
    // track (iOS ends the mic on some audio-route changes) or a closed
    // AudioContext means a real re-acquire instead.
    const mediaReusable = () => {
        const s = rawStreamReference.current || streamReference.current
        const ctx = audioContext.current
        if (!s || !ctx || ctx.state === "closed") return false
        const tracks = s.getTracks()
        return tracks.length > 0 && tracks.every((t) => t.readyState === "live")
    }

    const handleStreamRef = useRef(null);
    const handleStream = async () => {
        try {
            // Reconnects reuse the microphone/camera and AudioContext from
            // the first connection (disconnect kept them): no new
            // permission prompt, no restarted capture.
            const reuse = mediaReusable()
            if (!reuse && (streamReference.current !== null || audioContext.current !== null)) {
                // Kept media that died: release it before re-acquiring. The
                // queued audio is no longer contiguous with what follows.
                disconnect(false)
                audioBacklog.current = { chunks: [], bytes: 0, drainTimer: null }
            }
            //Await wake lock for screen first
            await acquireWakeLock()
            //handle older browsers that might implement getUserMedia in some way

            ensureGetUserMedia()

            if (navigator.mediaDevices != null) {
                let stream = reuse ? streamReference.current : await navigator.mediaDevices.getUserMedia(constraintObj)
                if (!reuse) rawStreamReference.current = stream

                if (
                    !reuse &&
                    (joinwith.current === "Video" ||
                    joinwith.current === "Videocartoonify")
                ) {
                    // Apply the orientation choice from the device-check
                    // preview; "auto" falls back to the gravity heuristic
                    // (a rotation-locked phone mounted sideways delivers a
                    // buffer with the scene on its side, and only gravity —
                    // no screen API — reveals it).
                    try {
                        const mode = orientationMode.current
                        const rot =
                            mode === "auto"
                                ? await neededRotation()
                                : rotationForMode(mode)
                        if (rot !== 0) {
                            const fix = await correctedStream(stream, rot)
                            orientationFix.current = fix
                            stream = fix.stream
                        }
                    } catch (e) {
                        console.warn("orientation correction unavailable:", e)
                    }
                }

                // media.then(function (stream) {
                streamReference.current = stream
                if (!reuse) {
                    //keep this here for now to enable to capturing of audio finger printing
                    const context = new AudioContext({ sampleRate: 16000 })
                    // iOS creates contexts suspended when constructed this far
                    // after the tap gesture; resume so audio actually flows.
                    if (context.state === "suspended") {
                        context.resume().catch(() => {})
                    }
                    source.current = context.createMediaStreamSource(stream)
                    audioContext.current = context
                }
                if (joinwith.current === "Audio") {
                    audiows.current = new WebSocket(apiService.getAudioWebsocketEndpoint(),)
                    connect_audio_processor_service();

                } else if (
                    joinwith.current === "Video" ||
                    joinwith.current === "Videocartoonify"
                ) {

                    if (mimetype !== "") {
                        // Bitrate caps: browser defaults vary wildly (laptop
                        // Chrome ~2.5 Mbps, phone Safari 7-10 Mbps for the
                        // same 640x480) — uncapped, 20 pods fill ~60 GB/hour
                        // of disk and saturate classroom Wi-Fi uplinks.
                        // Hard ceiling 5 Mbps (good Full HD), scaled with
                        // the pixels actually captured against a 720p
                        // reference at 2.5 Mbps. Keep the ceiling honest
                        // against measured venue capacity: the throughput
                        // gauge's saturated readings are the budget N pods
                        // must fit inside (2026-08-08: an 8 Mbps cap plus a
                        // server-side ingest stall queued minutes of video
                        // on the phones, lost at teardown).
                        if (!reuse || !recorderOptions.current) {
                            const vSettings = (() => {
                                const t = stream.getVideoTracks()[0]
                                return t && t.getSettings ? t.getSettings() : {}
                            })()
                            const capturedPixels =
                                (vSettings.width || 640) * (vSettings.height || 480)
                            const videoRate = Math.min(
                                5_000_000,
                                Math.max(
                                    1_250_000,
                                    Math.round(
                                        (2_500_000 * capturedPixels) / (1280 * 720),
                                    ),
                                ),
                            )
                            recorderOptions.current = {
                                mimeType: mimetype,
                                videoBitsPerSecond: videoRate,
                                audioBitsPerSecond: 128_000,
                            }
                        }
                        // A fresh recorder per connection (see disconnect).
                        const mediaRec = new MediaRecorder(
                            stream,
                            recorderOptions.current,
                        )
                        mediaRecorder.current = mediaRec

                        //Since we are implementing distributed  processing for audio and video,
                        //The audio and  video socket needs to be enabled to receive the  video data
                        // The server listening to the audio_socket will extract audio stream from the
                        // video data for processing, while the server for video_socket will extract the video
                        // for processing.

                        audiows.current = new WebSocket(apiService.getAudioWebsocketEndpoint(),)
                        connect_audio_processor_service();

                        //activate video websocket 
                        videows.current = new WebSocket(apiService.getVideoWebsocketEndpoint(),)
                        connect_video_processor_service()

                    }


                }
            } else {
                setDisplayText("No media devices detected.")
                setCurrentForm("JoinError")
                disconnect(true)
            }
        } catch (ex) {
            console.error(ex)
            // Say why, not just that it failed — on phones this is almost
            // always a permission or in-app-browser problem the user can fix.
            let msg = "Failed to get user audio source."
            if (ex && (ex.name === "NotAllowedError" || ex.name === "PermissionDeniedError" || ex.name === "SecurityError")) {
                msg = "Microphone access was blocked. Allow microphone access for this site in your browser settings, then try again."
            } else if (ex && (ex.name === "NotFoundError" || ex.name === "DevicesNotFoundError")) {
                msg = "No microphone was found on this device."
            } else if (ex && (ex.name === "NotReadableError" || ex.name === "TrackStartError")) {
                msg = "The microphone is in use by another app. Close it and try again."
            } else if (ex && /not implemented|mediaDevices/i.test(ex.message || "")) {
                msg = "This browser can't record audio. Open the link in Safari or Chrome instead of the in-app browser."
            }
            setDisplayText(msg)
            setCurrentForm("JoinError")
            disconnect(true)
        }
    }

    handleStreamRef.current = handleStream;

    // Verifies the users connection input, then routes through the device
    // check page (camera preview, mic levels, channel choice) before the
    // actual join.
    const verifyInputAndAudio = (names, passcode, joinswith, collaborators) => {
        if (names === null) {
            names = "User Device"
        }
        // The join form's inline device check owns the current selection;
        // release its preview devices before the real capture opens them.
        const inline = inlineSelection.current || {}
        if (inline.stopPreview) inline.stopPreview()
        deviceSelection.current = {
            audioDeviceId: inline.audioDeviceId || null,
            videoDeviceId: inline.videoDeviceId || null,
            videoResolution: inline.videoResolution || null,
            videoPanorama: !!inline.videoPanorama,
            channelIndex:
                inline.channelIndex === undefined ? null : inline.channelIndex,
        }
        requestAccessKey(names, passcode, collaborators, joinswith)
    }

    // Stop the recorder so its buffered media flushes while the sockets
    // are still open (the video recorder only ships a chunk every
    // `interval` ms — without this, a recording shorter than that saved
    // nothing at all), then DRAIN the video socket. On a congested uplink
    // the websocket can be holding minutes of queued video; tearing down
    // after a fixed 1.5s abandoned all of it — sessions ended with full
    // audio but a fraction of their video, unrecoverably (the backlog
    // exists only in this tab's memory). Bounded by a no-progress cutoff
    // so a dead link doesn't trap the user on a spinner; while bytes are
    // moving it waits as long as it takes.
    const flushAndDrainVideo = async () => {
        try {
            if (mediaRecorder.current && mediaRecorder.current.state === "recording") {
                // stop(), not requestData(): the recorder must not keep
                // producing chunks while the backlog below drains.
                mediaRecorder.current.stop()
                await new Promise((resolve) => setTimeout(resolve, 1500))
            }
        } catch (ex) {
            console.error("final flush failed", ex)
        }
        // bufferedAmount is what this tab has queued but not yet
        // transmitted; it reaches 0 when the last chunk is on the wire.
        try {
            const backlog = () =>
                videows.current && videows.current.readyState === WebSocket.OPEN
                    ? videows.current.bufferedAmount
                    : 0
            if (backlog() > 65536) {
                setCurrentForm("FinishingUpload")
                // No hard deadline: on a starved uplink a long session's
                // backlog can legitimately need many minutes, and cutting
                // it off discards footage. The no-progress cutoff below is
                // the only exit — a transfer that is moving keeps going; a
                // dead link still releases the user within 30s.
                let lastBytes = backlog()
                let lastProgress = Date.now()
                while (backlog() > 0) {
                    await new Promise((resolve) => setTimeout(resolve, 500))
                    const now = backlog()
                    if (now < lastBytes) {
                        lastBytes = now
                        lastProgress = Date.now()
                    } else if (Date.now() - lastProgress > 30000) {
                        console.error(
                            "upload stalled with",
                            now,
                            "bytes queued — giving up",
                        )
                        break
                    }
                }
            }
        } catch (ex) {
            console.error("drain before close failed", ex)
        }
    }

    // End this pod's recording cleanly, waiting for queued video to reach
    // the server before the sockets close.
    const endRecording = async () => {
        setArmed(false)
        await flushAndDrainVideo()
        // Go straight to this pod's overview page instead of the dead-end
        // "recording ended" screen (which rendered as a mostly blank page).
        // Capture the ids before disconnect clears the session state; if
        // they're somehow gone, fall back to the old closed screen.
        const sid = session && session.id
        const did = sessionDevice && sessionDevice.id
        disconnect(true)
        if (sid && did) {
            return navigate(`/sessions/${sid}/pods/${did}`)
        }
        setDisplayText(
            "This pod's recording has ended. You can close this page, or join again to record more.",
        )
        setCurrentForm("ClosedSession")
    }


    // Requests session access from the server.
    const requestAccessKey = async (names, passcode, collaborators, l_joinwith) => {
        ending.current = false
        setCurrentForm("Connecting")
        const sel = deviceSelection.current || {}
        const constraint = {}
        // A specific channel implies a multi-channel rig: request all
        // channels raw (echo cancellation forces a mono downmix on several
        // platforms, which would destroy the separation being selected).
        constraint.audio = sel.channelIndex !== null && sel.channelIndex !== undefined
            ? {
                  channelCount: { ideal: 8 },
                  echoCancellation: false,
                  noiseSuppression: false,
                  autoGainControl: false,
              }
            : {}
        if (sel.audioDeviceId) {
            constraint.audio.deviceId = { exact: sel.audioDeviceId }
        }
        if (Object.keys(constraint.audio).length === 0) {
            constraint.audio = true
        }
        if (l_joinwith === "Video" || l_joinwith === "Videocartoonify") {
            // Resolution: an explicit device-check choice wins; otherwise
            // Full HD — the same default the device-check panel shows, so a
            // pod that skipped the panel records identically. Panorama cams
            // (360° conference cameras) compose their whole ring view inside
            // the requested frame, so they keep the full 1080p.
            // Phone paths go through dimsForMode: the device-check
            // orientation choice decides the requested frame shape ("wide"
            // asks for landscape outright; the rest request screen-oriented
            // dims and fix rotation after capture). Panorama cams compose
            // their ring view landscape regardless of the host device, so
            // they keep a fixed landscape frame.
            orientationMode.current = sel.orientationMode || "wide"
            constraint.video = sel.videoResolution
                ? {
                      facingMode: SESSION_FACING,
                      ...dimsForMode(orientationMode.current, sel.videoResolution),
                  }
                : sel.videoPanorama
                  ? {
                        width: { ideal: 1920 },
                        height: { ideal: 1080 },
                    }
                  : {
                        facingMode: SESSION_FACING,
                        ...dimsForMode(orientationMode.current, {
                            width: 1920,
                            height: 1080,
                        }),
                    }
            if (sel.videoDeviceId) {
                constraint.video.deviceId = { exact: sel.videoDeviceId }
                delete constraint.video.facingMode
            }
        } else {
            constraint.video = false
        }
        const mediaType = pickMimeType(constraint)
        const mediaExt = (mediaType !== "" && mediaType.indexOf("webm") !== -1) ? "webm" : (mediaType !== "" && mediaType.indexOf("mp4") !== -1) ? "mp4" : ""
        sessionService.joinByodSession(names, passcode, collaborators).then(
            (response) => {
                const joinFailed = (message) => {
                    setDisplayText(message)
                    setCurrentForm("JoinError")
                    disconnect(true)
                }
                if (response.status === 200) {
                    response.json().catch(() => {
                        joinFailed("The server sent an unexpected reply. Please try again.")
                        return null
                    }).then((jsonObj) => {
                        if (jsonObj === null) return
                        setSession(SessionModel.fromJson(jsonObj["session"]))
                        setSessionDevice(
                            SessionDeviceModel.fromJson(
                                jsonObj["session_device"],
                            ),
                        )
                        const freshSpeakers = SpeakerModel.fromJsonList(jsonObj["speakers"])
                        setSpeakers(freshSpeakers)
                        prunePolarToRoster(freshSpeakers)
                        // The server assigns "Group A/B/C..." when the name
                        // was left blank — keep the assigned name so backing
                        // out prefills it and rejoining resumes this pod.
                        name.current = jsonObj["session_device"].name || names
                        key.current = jsonObj.key;
                        // From here every API call and media URL carries the
                        // pod key — the guarded device endpoints need it.
                        setClientProcessingKey(jsonObj.key)
                        numSpeakers.current = collaborators
                        // Nothing captures or connects yet: the media plan
                        // waits until the roster is confirmed on the speaker
                        // page (confirmSpeakers applies it, which triggers
                        // handleStream via the constraintObj effect).
                        pendingMedia.current = {
                            constraint,
                            mediaType,
                            mediaExt,
                        }
                        pendingFingerprints.current = []
                        setPcode(passcode)
                        joinwith.current = l_joinwith
                        setCurrentForm("")
                    })
                } else {
                    // Any non-200 must end the spinner (it used to hang on
                    // a 5xx until the 30 s watchdog). Proxy and limiter
                    // errors are not JSON, so the message is best-effort.
                    let message =
                        "Couldn't join the session (server returned " +
                        response.status +
                        "). Please try again in a moment."
                    if (response.status === 429) {
                        message = "Too many join attempts from this network. Wait a minute and try again."
                    }
                    response
                        .json()
                        .then(
                            (jsonObj) => {
                                if (jsonObj && jsonObj["message"]) message = jsonObj["message"]
                            },
                            () => {},
                        )
                        .finally(() => joinFailed(message))
                }
            },
            (apierror) => {
                setDisplayText("Contact Administrator")
                setCurrentForm("JoinError")
                disconnect(true)
                console.error(
                    "byod-join-component error func : requestAccessKey 1",
                    apierror,
                )
            },
        )
    }

    // No getUserMedia probe here: it double-prompted for the microphone,
    // leaked the probe stream's tracks, and — worst — a permission denial
    // rejected with no catch, leaving the "Connecting..." dialog up forever.
    // Whether audio is wanted is already known from the constraints.
    const pickMimeType = (constraintObj) => {
        const hasAudio = !!constraintObj.audio;

        // Try best-to-widest support order.
        const candidates = [
            // WebM (Android/desktop Chrome)
            hasAudio ? "video/webm;codecs=vp9,opus" : "video/webm;codecs=vp9",
            hasAudio ? "video/webm;codecs=vp8,opus" : "video/webm;codecs=vp8",
            "video/webm",

            // MP4 (iOS/iPadOS Safari/WebKit, incl. Chrome on iPad)
            // H.264 (avc1) + AAC (mp4a) are the usual fourccs
            hasAudio ? "video/mp4;codecs=h264,aac" : "video/mp4;codecs=h264",
            hasAudio ? "video/mp4;codecs=avc1.42E01E,mp4a.40.2" : "video/mp4;codecs=avc1.42E01E",
        ];

        for (const mt of candidates) {
            try {
                if (typeof MediaRecorder.isTypeSupported === "function" &&
                    MediaRecorder.isTypeSupported(mt)) {
                    return mt;
                }
            } catch { /* some engines throw on probe; ignore and continue */ }
        }
        return ""; // no explicit mimeType — let the browser pick or we’ll handle failure
    }



    // Connects to audio processor websocket server.
    const connect_audio_processor_service = () => {
        audiows.current.binaryType = "arraybuffer"

        audiows.current.onopen = (e) => {
            // A ref (not a per-socket local): React state in the onclose
            // closure is a stale snapshot, and a socket-local flag made a
            // failed RETRY take the "couldn't reach the server" dead end.
            everOpened.current = true
            next("audio_socket_open")
            setPageTitle(name.current)

        };

        audiows.current.onmessage = (e) => {
            const message = JSON.parse(e.data)

            if (message["type"] === "start") {
                next("audio_ready")
                closeDialog()
            } else if (message['type'] === 'registeredfingerprintadded') {
                // Ack for a replayed saved-fingerprint attach — the card was
                // already marked ready when the attach was queued.
            } else if (message['type'] === 'registeredfingerprintfailed') {
                console.error("saved fingerprint failed (audio): " + message["message"])
                onSavedFingerprintFailed(message["message"])
            } else if (message["type"] === "asr_status") {
                // Server-side transcription health. Degraded = the live
                // transcript will lag; the recording itself is unaffected.
                setAsrNotice(
                    message["state"] === "degraded"
                        ? message["message"] ||
                              "Live transcription is running behind on the server; the recording is unaffected."
                        : null,
                )
            } else if (message["type"] === "error") {
                disconnect(true)
                setDisplayText(
                    "The connection to the session has been closed by the audio server.",
                )
                console.error("message from the audio server is " + message["message"])
                setCurrentForm("ClosedSession")
            } else if (message["type"] === "end") {
                // Ship any queued video before tearing down — the session
                // ending must not discard footage already recorded.
                flushAndDrainVideo().finally(() => {
                    disconnect(true)
                    setDisplayText("The session has been closed by the owner.")
                    setCurrentForm("ClosedSession")
                })
            }
        }

        audiows.current.onclose = (e) => {
            if (ending.current) return
            if (!everOpened.current) {
                // Never came up at all this join — not a mid-session drop.
                setDisplayText(
                    "Couldn't reach the session server. Please try again, or ask your instructor to check that recording is running.",
                )
                setCurrentForm("ClosedSession")
                disconnect(true)
                return
            }
            scheduleReconnect("audio")
        }
    }

    // Connects to video processor websocket server.
    const connect_video_processor_service = () => {
        videows.current.binaryType = "blob"

        videows.current.onopen = (e) => {
            next("video_socket_open")
        }

        videows.current.onmessage = (e) => {
            if (typeof e.data === 'string') {
                const message = JSON.parse(e.data);
                if (message['type'] === 'start') {
                    next("video_ready")
                    closeDialog();
                } else if (message['type'] === 'attention_data') {

                } else if (message['type'] === 'registeredfingerprintadded') {
                    // Ack for a replayed saved-fingerprint attach; nothing to
                    // update — the card was marked ready at queue time.
                } else if (message['type'] === 'registeredfingerprintfailed') {
                    console.error("saved fingerprint failed (video): " + message["message"])
                    onSavedFingerprintFailed(message["message"])
                } else if (message['type'] === 'error') {
                    disconnect(true);
                    setDisplayText(message["message"]);
                    console.error("message from the video server is " + message["message"])
                    setCurrentForm('ClosedSession');
                } else if (message['type'] === 'end') {
                    flushAndDrainVideo().finally(() => {
                        disconnect(true);
                        setDisplayText('The session has been closed by the owner.');
                        setCurrentForm('ClosedSession');
                    });
                } else if (message['type'] === 'no_video_data' ||
                           message['type'] === 'video_lagging') {
                    // Server-side watchdogs: the recording looks fine from
                    // the pod, so the pod itself must say otherwise.
                    setStreamWarning(message['message']);
                } else if (message['type'] === 'video_lag_cleared') {
                    setStreamWarning(null);
                } else if (message['type'] === 'heartbeat') {
                }
            } else if (e.data instanceof Blob) {
                const url = URL.createObjectURL(e.data);
                // Add the processed frame to the buffer
                frameBuffer.current.push(url);
                if (frameBuffer.current.length % 40 === 0) {
                    setFrameBufferLength(frameBuffer.current.length)
                }
            }
        };

        // Used to be empty: a video drop was silent and lost the video for
        // the rest of the session (audit E.2).
        videows.current.onclose = (e) => {
            if (!ending.current) scheduleReconnect("video")
        };
    }

    // Begin capturing and sending client audio.
    const requestStartAudioProcessing = () => {
        let message = null
        if (audiows.current === null) {
            return
        }
        message = {
            type: "start",
            key: key.current,
            // 0 on a first connection; on a reconnect the seconds of audio
            // queued while offline (see audioBacklogSeconds).
            start_time: audioBacklogSeconds(),
            sample_rate: audioContext.current.sampleRate,
            encoding: "pcm_f32le",
            channels: 1,
            streamdata: "audio",
            tag: true,
            embeddings_file: sessionDevice.embeddings,
            deviceid: sessionDevice.id,
            sessionid: session.id,
            numSpeakers: numSpeakers.current,
        }
        audiows.current.send(JSON.stringify(message))
    }

    // Begin capturing and sending client video.
    const requestStartVideoProcessing = () => {
        let message = null
        if (videows.current === null) {
            return
        }
        if (joinwith.current === "Video") {
            message = {
                type: "start",
                key: key.current,
                start_time: 0.0,
                sample_rate: 16000,
                encoding: "pcm_f16le",
                video_encoding: "video/webm",
                channels: 2,
                streamdata: "video",
                mimeextension: mimeExtension,
                embeddings_file: sessionDevice.embeddings,
                deviceid: sessionDevice.id,
                Video: true,
                sessionid: session.id,
                numSpeakers: numSpeakers.current,
                liveAnalytics: liveAnalyticsRef.current,
            }
        } else if (joinwith.current === "Videocartoonify") {
            message = {
                type: "start",
                key: key.current,
                start_time: 0.0,
                sample_rate: 16000,
                encoding: "pcm_f16le",
                video_encoding: "video/webm",
                channels: 2,
                streamdata: "video",
                mimeextension: mimeExtension,
                embeddings_file: sessionDevice.embeddings,
                deviceid: sessionDevice.id,
                sessionid: session.id,
                Video_cartoonify: true,
                numSpeakers: numSpeakers.current,
                liveAnalytics: liveAnalyticsRef.current,
            }
        }
        videows.current.send(JSON.stringify(message))
    }

    const requestHelp = () => {
        sessionDevice.button_pressed = !sessionDevice.button_pressed
        const requested = sessionDevice.button_pressed
        sessionService
            .setDeviceButton(sessionDevice.id, requested, key.current)
            .then((r) => {
                if (!r || r.status !== 200) return Promise.reject()
            })
            .catch(() => {
                // Offline, the tap used to silently do nothing — the group
                // kept waiting for an instructor who was never called.
                sessionDevice.button_pressed = !requested
                setStreamWarning(
                    "Couldn't send the help request — check the connection and tap again.",
                )
            })
    }

    // The header back arrow. In-app navigation only (never browser
    // history): from a joined pod it returns to the join form with the
    // passcode and name kept; from the plain form it leaves to the landing
    // page. A live connection still gets the NavGuard confirmation.
    const navigateToLogin = (confirmed = false) => {
        if (!confirmed && (state.audioSocketOpen || state.videoSocketOpen)) {
            setCurrentForm("NavGuard")
            return
        }
        if (session !== null) {
            const keepName = name.current
            const keepCode = pcode
            const sockets = [audiows.current, videows.current]
            // Detach the socket handlers BEFORE closing: their onclose
            // logic would otherwise read the cleared `ending` flag and
            // mistake this intentional leave for a mid-session drop,
            // launching the reconnect loop over the join form (the
            // "Something went wrong" crash).
            for (const w of sockets) {
                if (w) {
                    w.onclose = null
                    w.onmessage = null
                    w.onerror = null
                }
            }
            disconnect(true)
            ending.current = false
            constraintObjRefReset()
            name.current = keepName
            setPcode(keepCode)
            setCurrentForm("")
            return
        }
        disconnect(true)
        setCurrentForm("")
        return navigate("/")
    }

    // Returning to the form must also clear the connect request, or the
    // phase machine would still derive "connecting" after a back.
    const constraintObjRefReset = () => {
        setConstraintObj(null)
        setMimeType(null)
        setMimeExtension(null)
        pendingMedia.current = null
        pendingFingerprints.current = []
        replayDone.current = false
    }

    const getSpeakerAliasFromID = (selectedSpkrId) => {
        if (selectedSpkrId !== -1) {
            const speaker = speakers.current.filter((s) => s.id === selectedSpkrId)
            if (speaker.length !== 0) {
                return speaker[0].alias
            }
        } else {
            return -1
        }
    }

    // Both fetchers: `full` re-fetches everything (replace), otherwise only
    // rows after the last-seen id (merge by id). Resolve false on any
    // failure so the poll loop backs off.
    const fetchTranscript = async (deviceid, signal = undefined, full = true) => {
        try {
            const response =
                await sessionService.getSessionDeviceTranscriptSpeakerMetricsForClient(
                    deviceid,
                    0,
                    key.current,
                    { signal, afterId: full ? 0 : lastTranscriptId.current },
                )

            if (response.status !== 200) {
                console.error(response, "no transcript obj")
                return false
            }
            const jsonObj = await response.json()
            const fetched_trancript_metrics = jsonObj.map((item, index) => {return { ...item['transcript'], speaker_metrics: item['speaker_metrics'] }});

            transcripts.current = full
                ? fetched_trancript_metrics
                : mergeById(transcripts.current, fetched_trancript_metrics)
            lastTranscriptId.current = maxId(
                fetched_trancript_metrics,
                full ? 0 : lastTranscriptId.current,
            )

            const sessionLen =
                Object.keys(session).length > 0 ? session.length : 0
            const sTime = Math.round(sessionLen * timeRange.current[0] * 100) / 100
            const eTime = Math.round(sessionLen * timeRange.current[1] * 100) / 100
            setStartTime(sTime)
            setEndTime(eTime)
            // The display effect keys on start/end time, which rarely
            // change between polls: refresh the rows directly when new
            // ones arrived.
            if (full || fetched_trancript_metrics.length) generateDisplayTranscripts(sTime, eTime)
            return true
        } catch (error) {
            console.error(
                "byod-join-component error func : fetch transcript",
                error,
            )
            return false
        }
    }

    const fetchVideoMetric = async (deviceid, signal = undefined, full = true) => {
        try {
            const response =
                await sessionService.getSessionDeviceVideoMetricsForClient(
                    deviceid,
                    0,
                    key.current,
                    { signal, afterId: full ? 0 : lastVideoMetricId.current },
                )

            if (response.status !== 200) {
                console.error(response, "no videometrics obj")
                return false
            }
            const jsonObj = await response.json()

            videoMetrics.current = full ? jsonObj : mergeById(videoMetrics.current, jsonObj)
            lastVideoMetricId.current = maxId(jsonObj, full ? 0 : lastVideoMetricId.current)
            if (full || jsonObj.length) {
                const sessionLen =
                    Object.keys(session).length > 0 ? session.length : 0
                generateDisplayVideoMetrics(
                    Math.round(sessionLen * timeRange.current[0] * 100) / 100,
                    Math.round(sessionLen * timeRange.current[1] * 100) / 100,
                )
            }
            return true
        } catch (error) {
            console.error(
                "byod-join-component error func : fetch video metrics",
                error,
            )
            return false
        }
    }


    const renderFrameFromBuffer = useCallback(() => {
        if (isPlayingBatchRef.current) return;
        if (frameBufferLength - (40 * (cartoonImgBatch - 1)) < 40) return

        const startIndex = (cartoonImgBatch - 1) * 40;
        const endIndex = cartoonImgBatch * 40;

        if (frameBufferLength < endIndex) return;

        isPlayingBatchRef.current = true;

        let currentIndex = startIndex;

        playbackIntervalRef.current = setInterval(() => {
            const frame = frameBuffer.current[currentIndex];
            if (frame) {
                setCartoonImgUrl(frame);
            }

            currentIndex += 1;

            if (currentIndex >= endIndex) {
                if (playbackIntervalRef.current) {
                    clearInterval(playbackIntervalRef.current);
                    playbackIntervalRef.current = null;
                }

                // Free the PREVIOUS batch's object URLs — batches never
                // replay (cartoonImgBatch only grows), and unrevoked frame
                // blobs otherwise accumulate for the whole session. This
                // batch's last frame stays displayed, so it is kept.
                const prevStart = (cartoonImgBatch - 2) * 40;
                if (prevStart >= 0) {
                    for (let i = prevStart; i < startIndex; i++) {
                        const url = frameBuffer.current[i];
                        if (url) {
                            URL.revokeObjectURL(url);
                            frameBuffer.current[i] = null;
                        }
                    }
                }

                isPlayingBatchRef.current = false;
                setCartoonImgBatch((prev) => prev + 1);
            }
        }, 33);
    }, [cartoonImgBatch, frameBufferLength]);

    // The effect that consumes this callback must live below it: listing
    // `renderFrameFromBuffer` in an effect's dependency array above the
    // `const` declaration reads it in the temporal dead zone on the first
    // render (ReferenceError), which took down the whole join page.
    // SEVENTH LEVEL: THIS EFFECT IS TRIGGERED ONCE THE LENGTH OF THE CARTOONIFIED FRAME BUFFER IS UPDATED, THIS THEN RENDERS THE FRAMES IN 
    // THE BUFFER TO THE VIDEO ELEMENT ONE BY ONE WITH A SMALL DELAY TO CREATE A SMOOTH VIDEO STREAMING EXPERIENCE
    useEffect(() => {
        if (frameBufferLength > 0) {
            renderFrameFromBuffer()
        }
    }, [frameBufferLength, renderFrameFromBuffer])

    const ResetTimeRange = (values) => {
        if (session !== null) {
            const sessionLen =
                Object.keys(session).length > 0 ? session.length : 0
            timeRange.current = values
            const start = Math.round(sessionLen * values[0] * 100) / 100
            const end = Math.round(sessionLen * values[1] * 100) / 100
            setStartTime(start)
            setEndTime(end)
            generateDisplayTranscripts(start, end)
        }
    }

    const generateDisplayTranscripts = (s, e) => {
        setDisplayTranscripts(
            transcripts.current.filter((t) => t.start_time >= s && t.start_time <= e),
        )
    }

    const generateDisplayVideoMetrics = (s, e) => {
        setDisplayVideoMetrics(
            videoMetrics.current.filter((v) => v.time_stamp >= s && v.time_stamp <= e),
        )
    }


    const setSpeakerTranscripts = () => {
        if (displayTranscripts.length) {
            setSpkr1Transcripts(
                displayTranscripts.reduce((values, transcript) => {

                    if (transcript.speaker_id === selectedSpkrId1
                    ) {
                        values.push(transcript);
                    }
                    return values;
                }, [])
            );
            setSpkr2Transcripts(
                displayTranscripts.reduce((values, transcript) => {
                    if (transcript.speaker_id === selectedSpkrId2
                    ) {
                        values.push(transcript);
                    }
                    return values;
                }, [])
            );
        } else {
            setSpkr1Transcripts([]);
            setSpkr2Transcripts([]);
        }
    };

    const setSpeakerVideoMetrics = () => {
        if (displayVideoMetrics.length) {
            let speakerAlias1 = getSpeakerAliasFromID(selectedSpkrId1)
            let speakerAlias2 = getSpeakerAliasFromID(selectedSpkrId2)
            setSpkr1VideoMetrics(
                displayVideoMetrics.reduce((values, videometrics) => {
                    if (videometrics.student_username === speakerAlias1
                    ) {
                        values.push(videometrics)
                    }
                    return values
                }, []),
            )
            setSpkr2VideoMetrics(
                displayVideoMetrics.reduce((values, videometrics) => {
                    if (videometrics.student_username === speakerAlias2
                    ) {
                        values.push(videometrics)
                    }
                    return values
                }, []),
            )
        } else {
            setSpkr1VideoMetrics([])
            setSpkr2VideoMetrics([])
        }
    }

    const seeAllTranscripts = () => {
        if (Object.keys(currentTranscript) > 0 && sessionDevice !== null) {
            setCurrentForm("gottoselectedtranscript")
        } else if (sessionDevice !== null) {
            setCurrentForm("gototranscript")
        }
    }

    const loading = () => {
        return session === null || transcripts.current.length === 0
    }

    const onClickedTimeline = (transcript) => {
        setCurrentForm("Transcript")
        setCurrentTranscript(transcript)
    }

    const openDialog = (form) => {
        setCurrentForm(form)
    }

    const closeDialog = () => {
        if (currentForm === "ClosedSession") {
            // Dismissing "Session ended" returns to the join form (identity
            // kept, per disconnect's contract) — ending.current previously
            // stayed latched, deriving the terminal "ended" phase forever,
            // and the pod was stranded on a header-only blank page.
            setPrevSessionId(-1)
            ending.current = false
            constraintObjRefReset()
        }
        setCurrentForm("")
    }

    const changeTouppercase = (e) => {
        // Passcodes are single memorable words now (formerly 4 random chars).
        let val = e.target.value.toUpperCase()
        setWrongInput(val.length > 16)
        setPcode(val)
    }

    const togglePreview = () => {
        setCurrentForm("")
        setPreview(!preview)
    }

    const acquireWakeLock = async () => {
        if (!("wakeLock" in navigator)) {
            console.error(
                "Screen Wake Lock API is not supported by the browser",
            )
            return 
        }

        // Reconnects keep the lock from the first connection.
        if (wakeLock.current !== null && !wakeLock.current.released) return

        try {
            wakeLock.current = await navigator.wakeLock.request("screen")
            if (wakeLockVisListener.current === null) {
                // One listener for the component's lifetime; it re-acquires
                // only while a lock is meant to be held (ref non-null).
                wakeLockVisListener.current = async () => {
                    if (
                        wakeLock.current !== null &&
                        document.visibilityState === "visible"
                    ) {
                        try {
                            wakeLock.current = await navigator.wakeLock.request("screen")
                        } catch {
                            /* backgrounded tabs can refuse; retried on next visible */
                        }
                    }
                }
                document.addEventListener("visibilitychange", wakeLockVisListener.current)
            }
        } catch (err) {
            console.error(err)
        }
    }

    const releaseWakeLock = () => {
        try {
            if (wakeLockVisListener.current !== null) {
                document.removeEventListener("visibilitychange", wakeLockVisListener.current)
                wakeLockVisListener.current = null
            }
            const lock = wakeLock.current
            wakeLock.current = null
            if (lock) lock.release().catch(() => {})
        } catch (err) {
            console.error(`WakeLock release error: ${err}`)
        }
    }

    const viewComparison = () => {
        setDetails("Comparison")
    }

    const viewGroup = () => {
        setDetails("Group")
    }

    const sessionDevBtnPressed =
        sessionDevice !== null ? sessionDevice.button_pressed : null

    const loadSpeakerMetrics = (speakerId, speakrAlias) => {
        setSelectedSpkrId1(speakerId)
        setPageTitle(speakrAlias)
        setDetails("Individual");
    }

    // Strangler-fig step 1: the explicit join phase, derived from the
    // existing flags (see join-machine.ts). Observational for now —
    // exposed as data-join-phase for tests/debugging; rendering and effects
    // migrate onto it incrementally in later steps.
    const joinPhase = deriveJoinPhase(state, {
        joined: session !== null,
        // Set when the roster is confirmed: the media plan is applied and
        // handleStream opens devices + sockets.
        connectRequested: constraintObj !== null,
        armed,
        currentForm,
        ending: ending.current,
        joinwith: joinwith.current || "Audio",
    })

    return (
        <>
        <ByodJoinPage
            joinPhase={joinPhase}
            state={state}
            POD_COLOR={POD_COLOR}
            button_pressed={sessionDevBtnPressed}
            verifyInputAndAudio={verifyInputAndAudio}
            closeDialog={closeDialog}
            currentForm={currentForm}
            displayText={displayText}
            navigateToLogin={navigateToLogin}
            pageTitle={pageTitle}
            requestHelp={requestHelp}
            pcode={pcode}
            savedName={name.current}
            wrongInput={wrongInput}
            changeTouppercase={changeTouppercase}
            joinwith={joinwith.current}
            preview={preview}
            previewLabel={previewLabel}
            togglePreview={togglePreview}
            disconnect={disconnect}
            sessionDevice={sessionDevice}
            setRange={ResetTimeRange}
            onClickedTimeline={onClickedTimeline}
            session={session}
            displayTranscripts={displayTranscripts}
            displayVideoMetrics={displayVideoMetrics}
            startTime={startTime}
            endTime={endTime}
            loading={loading}
            currentTranscript={currentTranscript}
            seeAllTranscripts={seeAllTranscripts}
            openDialog={openDialog}
            setCurrentForm={setCurrentForm}
            showBoxes={showBoxes}
            showFeatures={showFeatures}
            videoApiEndpoint={apiService.getVideoServerEndpoint()}
            speakers={roster}
            addSpeakerSlot={addSpeakerSlot}
            addSpeakerSlots={addSpeakerSlots}
            inlineRenameSpeaker={inlineRenameSpeaker}
            bluetoothSupported={isBluetoothSupported()}
            polarEnabled={polarEnabled}
            liveAnalytics={liveAnalytics}
            setLiveAnalytics={setLiveAnalytics}
            setPolarEnabled={setPolarEnabled}
            polarInfo={polarInfo}
            assignPolarSensor={assignPolarSensor}
            unassignPolarSensor={unassignPolarSensor}
            openForms={openForms}
            selectedSpkrId1={selectedSpkrId1}
            setSelectedSpkrId1={setSelectedSpkrId1}
            selectedSpkrId2={selectedSpkrId2}
            setSelectedSpkrId2={setSelectedSpkrId2}
            getSpeakerAliasFromID={getSpeakerAliasFromID}
            spkr1Transcripts={spkr1Transcripts}
            spkr2Transcripts={spkr2Transcripts}
            selectedSpeaker={selectedSpeaker}
            fingerprintRecordError={fingerprintRecordError}
            spkr1VideoMetrics={spkr1VideoMetrics}
            spkr2VideoMetrics={spkr2VideoMetrics}
            saveAudioFingerprint={saveAudioFingerprint}
            addSpeakerFingerprint={addSpeakerFingerprint}
            confirmSpeakers={confirmSpeakers}
            removeSpeakerSlot={removeSpeakerSlot}
            details={details}
            viewComparison={viewComparison}
            viewGroup={viewGroup}
            cartoonImgUrl={cartoonImgUrl}
            deviceSelectionRef={inlineSelection}
            armed={armed}
            micSilent={micSilent}
            recSeconds={recSeconds}
            startRecording={() => {
                // Resume inside the tap itself — this is the user gesture
                // iOS requires before an AudioContext may run.
                audioContext.current?.resume?.().catch(() => {})
                startRecordingGate()
            }}
            requestEndRecording={() => setCurrentForm("ConfirmEndRec")}
            confirmEndRecording={endRecording}
            loadSpeakerMetrics={loadSpeakerMetrics}
            prevSessionId={prevSessionId}
        />
        {streamWarning && (
            <div
                style={{
                    position: "fixed",
                    top: 0,
                    left: 0,
                    right: 0,
                    zIndex: 9999,
                    background: "#b91c1c",
                    color: "#fff",
                    padding: "10px 14px",
                    textAlign: "center",
                    fontSize: "14px",
                    fontWeight: 600,
                }}
            >
                {streamWarning}
            </div>
        )}
        {(netNotice || asrNotice) && (
            <div
                role="status"
                style={{
                    position: "fixed",
                    bottom: 12,
                    left: "50%",
                    transform: "translateX(-50%)",
                    maxWidth: "92vw",
                    zIndex: 9998,
                    background: "rgba(120, 53, 15, 0.94)",
                    color: "#fff",
                    padding: "6px 14px",
                    borderRadius: 999,
                    fontSize: "13px",
                    textAlign: "center",
                    pointerEvents: "none",
                }}
            >
                {netNotice || asrNotice}
            </div>
        )}
        {rotatePrompt && (
            <div
                style={{
                    position: "fixed",
                    inset: 0,
                    zIndex: 10000,
                    background: "rgba(0, 0, 0, 0.88)",
                    color: "#fff",
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    justifyContent: "center",
                    textAlign: "center",
                    padding: "24px",
                    gap: "16px",
                }}
            >
                <div style={{ fontSize: "48px" }}>📱⟳</div>
                <div style={{ fontSize: "20px", fontWeight: 700 }}>
                    Turn your phone sideways
                </div>
                <div style={{ fontSize: "15px", maxWidth: "420px", lineHeight: 1.5 }}>
                    The camera is capturing a tall (portrait) picture. Rotate
                    the phone to landscape — if the screen doesn&apos;t
                    rotate with it, turn off the orientation lock in Control
                    Center, then rotate again. Recording starts automatically
                    once the picture is wide.
                </div>
                <button
                    type="button"
                    onClick={() => {
                        portraitOverride.current = true
                    }}
                    style={{
                        marginTop: "8px",
                        background: "none",
                        border: "1px solid rgba(255,255,255,0.5)",
                        borderRadius: "8px",
                        color: "rgba(255,255,255,0.8)",
                        padding: "8px 16px",
                        fontSize: "13px",
                    }}
                >
                    Record in portrait anyway
                </button>
            </div>
        )}
        </>
    )
}

export { JoinPage }
