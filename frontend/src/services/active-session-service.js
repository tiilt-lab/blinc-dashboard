import { BehaviorSubject } from "rxjs"
import { SocketService } from "./socket-service"
import { SessionService } from "./session-service"
import { SessionModel } from "../models/session"
import { SessionDeviceModel } from "../models/session-device"
import { TranscriptModel } from "../models/transcript"
import { SpeakerMetricsModel } from "../models/speaker-metrics"
import { SpeakerVideoMetricsModel } from "../models/speaker-video-metrics"
import { maxId } from "../globals"

export class ActiveSessionService {
    socketService = new SocketService()
    sessionService = new SessionService()

    sessionSource = new BehaviorSubject(null)
    sessionDeviceSource = new BehaviorSubject([])
    transcriptSource = new BehaviorSubject([])
    videoMetricSource = new BehaviorSubject([])

    socket
    sessionId
    initialized = false
    // Bumped by every initialize()/close(): an initialize() still awaiting
    // its REST calls when close() runs must not open a socket afterwards
    // (that socket had no owner and lived until the page was reloaded).
    generation = 0

    // onResult is called EXACTLY once with an outcome:
    //   { status: "ready" }                        — session + devices loaded
    //   { status: "error", httpStatus: <code|null> } — any failure
    // The old version only advanced on the full 200/200/parse happy path and
    // did nothing (just console.error) on any failure — so a 401 (expired
    // login) or a non-200 left the session page's spinner up forever with no
    // error and no redirect. Always report, so the caller can recover.
    async initialize(sessionId, onResult) {
        if (this.sessionId === sessionId) {
            return
        }
        this.close()
        this.sessionId = sessionId
        const generation = ++this.generation
        // close() (unmount) or a newer initialize() ran while we awaited:
        // the caller is gone, so report nothing and open nothing.
        const cancelled = () => generation !== this.generation
        try {
            const sessionResp = await this.sessionService.getSession(sessionId)
            if (cancelled()) return
            if (sessionResp.status !== 200) {
                onResult({ status: "error", httpStatus: sessionResp.status })
                return
            }
            const session = await sessionResp.json()
            if (cancelled()) return
            this.sessionSource.next(SessionModel.fromJson(session))

            const devicesResp = await this.sessionService.getSessionDevices(sessionId)
            if (cancelled()) return
            if (devicesResp.status !== 200) {
                onResult({ status: "error", httpStatus: devicesResp.status })
                return
            }
            const devices = await devicesResp.json()
            if (cancelled()) return
            this.sessionDeviceSource.next(SessionDeviceModel.fromJsonList(devices))

            // The pod list comes from REST above, so the page is ready to show
            // it now. Do NOT gate the display on the socket's room_joined: the
            // socket is for LIVE updates, and if it can't connect (e.g. the
            // WebSocket transport is unavailable) the pods would otherwise load
            // forever. room_joined re-affirms this flag when the socket does
            // connect; live data still streams in through its handlers.
            this.initialized = true
            this.initializeSocket()
            onResult({ status: "ready" })
        } catch (error) {
            if (cancelled()) return
            console.error("active-session-service: initialize failed", error)
            onResult({ status: "error", httpStatus: null, error })
        }
    }

    // Highest ids currently held, sent with every join_room so a reconnect
    // replays only newer rows. Both 0 on the first join = full history.
    lastSeenIds() {
        const fields = {}
        const t = maxId(this.transcriptSource.getValue())
        const v = maxId(this.videoMetricSource.getValue())
        if (t > 0) fields.last_transcript_id = t
        if (v > 0) fields.last_video_metric_id = v
        return fields
    }

    initializeSocket() {
        // Create Socket.
        this.socket = this.socketService.createSocket(
            "session",
            this.sessionId,
            () => this.lastSeenIds(),
        )
        // Update device.
        this.socket.on("device_update", (e) => {
            const updatedDevice = SessionDeviceModel.fromJson(JSON.parse(e))
            const currentDevices = this.sessionDeviceSource.getValue()
            const index = currentDevices.findIndex(
                (d) => d.id === updatedDevice.id,
            )
            if (index !== -1) {
                currentDevices[index] = updatedDevice
                this.sessionDeviceSource.next(currentDevices)
            } else {
                currentDevices.push(updatedDevice)
                this.sessionDeviceSource.next(currentDevices)
            }
        })

        //  Remove device
        this.socket.on("device_removed", (e) => {
            const removedDeviceId = JSON.parse(e)["id"]
            const currentDevices = this.sessionDeviceSource
                .getValue()
                .filter((d) => d.id !== removedDeviceId)
            const currentTranscripts = this.transcriptSource
                .getValue()
                .filter((d) => d.session_device_id !== removedDeviceId)
            const currentVideoMetrics = this.videoMetricSource
                .getValue()
                .filter((d) => d.session_device_id !== removedDeviceId)    

            this.sessionDeviceSource.next(currentDevices)
            this.transcriptSource.next(currentTranscripts)
            this.videoMetricSource.next(currentVideoMetrics)
        })

        // Update session.
        this.socket.on("session_update", (e) => {
            this.sessionSource.next(SessionModel.fromJson(JSON.parse(e)))
        })

        // Handle room join. socket.io fires 'connect' (and therefore
        // join_room) again after every reconnect. The join carries the
        // last-seen ids, so the replay that follows is only the rows we
        // missed — local state is KEPT (clearing it here would blank the
        // page until the partial replay landed). Both digest handlers
        // de-duplicate by id, so a server that still replays the full
        // history is harmless too.
        this.socket.on("room_joined", (e) => {
            this.initialized = true
        })

        // Update transcripts and speaker metrics. Replace-by-id when the row
        // is already known (a live update racing the digest replay must not
        // duplicate it).
        this.socket.on("transcript_metrics_update", (e) => {
            const data = JSON.parse(e)
            const speaker_metrics = SpeakerMetricsModel.fromJsonList(
                data["speaker_metrics"],
            )
            const transcript_model = TranscriptModel.fromJson(
                data["transcript"],
                speaker_metrics,
            )
            const currentTranscripts = this.transcriptSource.getValue()
            const index = currentTranscripts.findIndex(
                (t) => t.id === transcript_model.id,
            )
            if (index !== -1) {
                currentTranscripts[index] = transcript_model
            } else {
                currentTranscripts.push(transcript_model)
            }
            this.transcriptSource.next(currentTranscripts)
        })

        // Live transcripts from pods with NO enrolled speakers: that path
        // posts to /callback/transcript, which emits the bare transcript row
        // on 'transcript_update' (no speaker metrics). Without this listener
        // those rows reached the DB but never the open panel — the pod looked
        // dead until a reload.
        this.socket.on("transcript_update", (e) => {
            const transcript_model = TranscriptModel.fromJson(JSON.parse(e), [])
            const currentTranscripts = this.transcriptSource.getValue()
            const index = currentTranscripts.findIndex(
                (t) => t.id === transcript_model.id,
            )
            if (index !== -1) {
                currentTranscripts[index] = transcript_model
            } else {
                currentTranscripts.push(transcript_model)
            }
            this.transcriptSource.next(currentTranscripts)
        })

        // Initial digest of transcripts and speaker metrics (paged; several
        // events per join). Skip ids already present so replays and races
        // can never duplicate rows.
        this.socket.on("transcript_metrics_digest", (e) => {
            const data = JSON.parse(e)
            const transcripts = this.transcriptSource.getValue()
            const known = new Set(transcripts.map((t) => t.id))
            for (const transcript_metrics of data) {
                const speaker_metrics = SpeakerMetricsModel.fromJsonList(
                    transcript_metrics["speaker_metrics"],
                )
                const transcript_model = TranscriptModel.fromJson(
                    transcript_metrics["transcript"],
                    speaker_metrics,
                )
                if (!known.has(transcript_model.id)) {
                    known.add(transcript_model.id)
                    transcripts.push(transcript_model)
                }
            }
            this.transcriptSource.next(transcripts)
        })

        // Initial digest of speaker video metrics. Skip ids already present
        // (same reason as the transcript digest).
        this.socket.on("video_metrics_digest", (e) => {
            const data = JSON.parse(e)
            const videoMetrics = this.videoMetricSource.getValue()
            const known = new Set(videoMetrics.map((m) => m.id))
            for (const metrics of data) {
                const speaker_video_metrics = SpeakerVideoMetricsModel.fromJson(
                    metrics["speaker_video_metrics"]
                )
                if (speaker_video_metrics.id == null || !known.has(speaker_video_metrics.id)) {
                    known.add(speaker_video_metrics.id)
                    videoMetrics.push(speaker_video_metrics)
                }
            }
            this.videoMetricSource.next(videoMetrics)
        })

        // Update speaker video metrics.
        this.socket.on("video_metrics_update", (e) => {
            const data = JSON.parse(e)
            const currentVideoMetrics = this.videoMetricSource.getValue()
            for(const metric of data["speaker_video_metrics"]){
                SpeakerVideoMetricsModel.fromJson(metric)
                currentVideoMetrics.push(metric)
            }
            
            this.videoMetricSource.next(currentVideoMetrics)
        })
    }

    close() {
        // Invalidate any initialize() still in flight (see `generation`).
        this.generation++
        if (this.socket != null) {
            this.socket.removeAllListeners()
            this.socket.disconnect()
            this.socket = null
        }
        this.initialized = false
        this.sessionId = null
        this.sessionSource.next(null)
        this.sessionDeviceSource.next([])
        this.transcriptSource.next([])
        this.videoMetricSource.next([])
    }

    getSession() {
        return this.sessionSource.getValue()
    }

    getSessionDevice(sessionDeviceId) {
        return this.sessionDeviceSource
            .getValue()
            .find((d) => d.id === parseInt(sessionDeviceId, 10))
    }

    getSessionDevices() {
        return this.sessionDeviceSource.getValue()
    }

    getTranscripts() {
        return this.transcriptSource
    }


    getVideoMetrics() {
        return this.videoMetricSource
    }
}
