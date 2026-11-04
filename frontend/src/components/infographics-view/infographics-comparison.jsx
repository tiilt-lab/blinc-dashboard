import { AppSectionBoxComponent } from "../section-box/section-box-component"
import { TranscriptPanel } from "../transcript-panel/transcript-panel"
import { VideoAnalyticsPanel } from "../video-analytics/video-analytics-panel"
import { ConversationDynamicsPanel } from "../conversation-dynamics/conversation-dynamics-panel"
import { VideoPlayer } from "../video-player/video-player"
import { AudioPlayer } from "../audio-player/audio-player"
import { lineAt, seekFor, toMergedClock } from "../../pod-details/group-time"
import { PosthocTrigger } from "../posthoc/posthoc-trigger"
import { NegotiationCodingPanel } from "../negotiation-coding/negotiation-coding-panel"
import { ModelNote } from "../model-note/model-note"
import { AppTimelineSlider } from "../timeline-slider/timeline-slider-component"
import { AppIndividualFeaturesComponent } from "../individualmetrics/features-component"
import { AppGroupFeaturesComponent } from "../individualmetrics/group-features-component"
import { SessionVitals } from "../session-vitals/session-vitals"
import { SpeakerCardsPanel } from "../speaker-cards/speaker-cards-panel"
import { JointAttentionPanel } from "../joint-attention/joint-attention-panel"
import { HeartRatePanel } from "../heart-rate/heart-rate-panel"
import { EegPanel } from "../eeg/eeg-panel"
import React, { useState, useEffect, useRef, lazy, Suspense } from "react"
import { ApiService } from "../../services/api-service"

// Heaviest chunk in the app (~288 KB). Only rendered when a pod has video
// metrics AND the video box is expanded, so defer it until then — text-only
// pods and unexpanded views never pay for it.
const AppIndividualVideoFeaturesComponent = lazy(() =>
    import("../individualVideometrics/video-features-component").then((m) => ({
        default: m.AppIndividualVideoFeaturesComponent,
    })),
)

// The laptops of a breakout group as views of one conversation: one card
// per laptop across the width, offering only the recordings it has (video,
// audio), then the transcript scope and the follow toggle.
function GroupViewBar({ group, viewId, viewKind, videoMissing, onSelectView, scope, onSelectScope, follow, onFollow }) {
    const pods = group.pods || []
    const kindButton = (p, kind, label, title) => {
        const active = viewId === p.id && viewKind === kind
        return (
            <button
                key={kind}
                type="button"
                onClick={() => onSelectView(p.id, kind)}
                title={title}
                className={
                    "flex-1 cursor-pointer rounded-md px-3 py-1.5 text-sm font-semibold transition " +
                    (active ? "bg-tiilt text-white shadow-sm" : "bg-tiilt-ground text-tiilt hover:bg-tiilt-soft")
                }
            >
                {label}
            </button>
        )
    }
    const cols = pods.length >= 3 ? "lg:grid-cols-3" : "sm:grid-cols-2"
    return (
        <div className="flex w-full flex-col gap-3">
            <div className={"grid w-full grid-cols-1 gap-3 " + cols}>
                {pods.map((p) => {
                    const video = p.has_video !== false && !videoMissing[p.id]
                    const audio = p.has_audio !== false
                    const current = viewId === p.id
                    return (
                        <div
                            key={p.id}
                            className={
                                "flex flex-col gap-2 rounded-xl border bg-white p-3 transition " +
                                (current ? "border-tiilt ring-2 ring-tiilt/20" : "border-tiilt-line")
                            }
                        >
                            <div className="flex items-center gap-2">
                                {p.side ? (
                                    <span className="rounded-full bg-tiilt-soft px-2 py-0.5 text-[11px] font-semibold text-tiilt">
                                        {p.side}
                                    </span>
                                ) : null}
                                <span className="min-w-0 truncate text-sm font-semibold text-tiilt-ink" title={p.name}>
                                    {p.name}
                                </span>
                                {current ? (
                                    <span className="ml-auto text-[11px] font-semibold text-tiilt">Viewing</span>
                                ) : null}
                            </div>
                            <div className="text-xs text-tiilt-muted">
                                {p.primary ? "Carries the merged transcript" : "A view only: its rows are not merged"}
                            </div>
                            <div className="flex gap-2">
                                {video ? kindButton(p, "video", "Video", "Play this laptop's video") : null}
                                {audio ? kindButton(p, "audio", "Audio", "Play this laptop's audio recording") : null}
                                {!video && !audio ? (
                                    <span className="text-xs text-tiilt-muted">No recording to play</span>
                                ) : null}
                            </div>
                        </div>
                    )
                })}
            </div>
            <div className="flex flex-wrap items-center gap-3 rounded-lg bg-tiilt-ground px-3 py-2">
                <span className="text-xs font-semibold text-tiilt-muted">Transcript</span>
                <span className="flex items-center gap-1 rounded-md border border-tiilt-line bg-white p-0.5" title="Whose transcript the panel shows">
                    {[["group", "Whole group"], ["device", "This laptop"]].map(([value, label]) => (
                        <button
                            key={value}
                            type="button"
                            onClick={() => onSelectScope && onSelectScope(value)}
                            className={
                                "cursor-pointer rounded px-3 py-1 text-xs font-semibold transition " +
                                (scope === value ? "bg-tiilt text-white" : "text-tiilt hover:bg-tiilt-soft")
                            }
                        >
                            {label}
                        </button>
                    ))}
                </span>
                <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-xs text-tiilt-muted" title="Play each line from the laptop that heard it">
                    <input type="checkbox" className="accent-tiilt" checked={follow} onChange={(e) => onFollow(e.target.checked)} />
                    Follow the speaker
                </label>
            </div>
        </div>
    )
}

function groupSummary(group, lines) {
    const primaries = (group.pods || []).filter((p) => p.primary).map((p) => p.name)
    const a = group.alignment
    return (
        `${(lines || []).length} lines merged from ${primaries.join(" and ") || "no laptop"}` +
        (a ? ` · clocks aligned by ${a.method} (${a.offset_min} to ${a.offset_max} s)` : "")
    )
}

function AppInfographicsComparison(props) {
    // Shared selection linking the transcript with the Expression & Thinking
    // Style graphs. It is the start_time of the selected utterance, which maps
    // 1:1 to a point on each feature graph.
    const [selectedTime, setSelectedTime] = useState(null)
    // Video segment layout, lifted from the player so the transcript panel can
    // show each line on the video's clock (recording-relative) rather than
    // session time — the two differ by however long setup ran before recording.
    const [videoSegments, setVideoSegments] = useState(null)
    const [videoMissing, setVideoMissing] = useState(false)
    // Session-relative playhead of the discussion video, so the transcript can
    // follow along while it plays.
    const [playbackTime, setPlaybackTime] = useState(null)
    // A breakout group: props.group holds the merged transcript's pods and
    // clock alignment, props.viewDeviceId the laptop whose recording plays
    // and whose per-laptop analyses show. In group scope the transcript is
    // on the merged clock, the player on its laptop's clock; seeks and the
    // playhead cross that mapping. "Follow the speaker" moves the view to
    // the laptop that heard the line now playing.
    const groupScope = !!props.group && props.scope === "group"
    const alignment = props.group ? props.group.alignment : null
    const [viewKind, setViewKind] = useState("video") // video | audio
    const [viewVideoMissing, setViewVideoMissing] = useState({})
    const [follow, setFollow] = useState(false)
    const lastSwitch = useRef(0)
    const viewId = props.viewDeviceId != null ? props.viewDeviceId : props.sessionDevice && props.sessionDevice.id
    const playerSeek = (() => {
        if (selectedTime == null || !groupScope) return selectedTime
        const line = (props.displayTranscripts || []).find((l) => l.start_time === selectedTime)
        return line ? seekFor(line, viewId, alignment) : selectedTime
    })()
    const podOf = (deviceId) => ((props.group && props.group.pods) || []).find((p) => p.id === deviceId)
    const hasVideo = (deviceId) => {
        const p = podOf(deviceId)
        return !!p && p.has_video !== false && !viewVideoMissing[deviceId]
    }
    const switchView = (deviceId, kind) => {
        // Land the new view on the moment playing now: select that line, the
        // new player seeks to it. A laptop without video plays its audio.
        const at = playbackTime
        const line = lineAt(props.displayTranscripts || [], at)
        if (line) setSelectedTime(line.start_time)
        setViewKind(kind === "video" && !hasVideo(deviceId) ? "audio" : kind)
        if (props.onSelectView && deviceId !== viewId) props.onSelectView(deviceId)
    }
    // The first laptop's kind: video when it has one, else audio.
    useEffect(() => {
        if (props.group && viewId != null && viewKind === "video" && !hasVideo(viewId)) setViewKind("audio")
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [props.group, viewId])
    const onGroupPlaybackTime = (deviceSeconds) => {
        const merged = groupScope ? toMergedClock(deviceSeconds, viewId, alignment) : deviceSeconds
        setPlaybackTime(merged)
        if (!follow || !groupScope) return
        const line = lineAt(props.displayTranscripts || [], merged)
        if (!line || line.session_device_id === viewId) return
        if (!(props.group.pods || []).some((p) => p.id === line.session_device_id)) return
        if (Date.now() - lastSwitch.current < 4000) return
        lastSwitch.current = Date.now()
        switchView(line.session_device_id, viewKind)
    }

    // Deployment-wide model registry from /api/v1/models (the audio/video
    // services' live config). Used only as the FALLBACK: every module note in
    // this view prefers the pod's own per-run provenance (posthoc_models,
    // stamped when its analysis completed), so labels describe the models
    // that actually produced the STORED rows, not whatever the deployment
    // would use today.
    const [globalModels, setGlobalModels] = useState(null)
    // Label registry now comes from the API (GET /api/v1/provenance_labels),
    // the single source of truth in routes/callback.py — the frontend used
    // to hand-mirror it, and the drift was the "caption names the wrong
    // model" bug. Empty until fetched; labels fall back to the id meanwhile.
    const [provenanceLabels, setProvenanceLabels] = useState({})
    useEffect(() => {
        new ApiService()
            .httpRequestCall("api/v1/provenance_labels", "GET", {})
            .then((r) => (r.status === 200 ? r.json() : null))
            .then((d) => d && setProvenanceLabels(d))
            .catch(() => {})
    }, [])
    // /api/v1/models module key -> posthoc_models provenance field.
    const MODULE_TO_PROVENANCE = {
        transcription: "asr",
        scoring: "scorer",
        participation: "embedder",
        keywords: "keywords",
        emotion: "emotion",
        attention: "attention",
        objects: "objects",
    }
    const podModels =
        props.sessionDevice && props.sessionDevice.posthoc_models
    // Same shape as /api/v1/models, resolved provenance-first, so children
    // (VideoAnalyticsPanel etc.) inherit correct labels unchanged.
    const models = (() => {
        const out = { ...(globalModels || {}) }
        for (const [module, field] of Object.entries(MODULE_TO_PROVENANCE)) {
            const id = podModels && podModels[field]
            if (id) {
                const label = (provenanceLabels[field] &&
                    provenanceLabels[field][id]) || id
                out[module] = { id, label }
            }
        }
        return out
    })()
    const transcriptionLabel =
        models.transcription && models.transcription.label
    // Section toggles by label, not index — index gating broke every time an
    // entry was added or retired (see the E&T removal realignment). A missing
    // list or label means "show it".
    const boxOn = (label) => {
        const box = (props.showBoxes || []).find((b) => b.label === label)
        return !box || box.clicked
    }
    useEffect(() => {
        new ApiService()
            .httpRequestCall("api/v1/models", "GET", {})
            .then((r) => (r.status === 200 ? r.json() : null))
            .then((d) => d && setGlobalModels(d))
            .catch(() => {})
    }, [])

    // Two arrangements of the same Group sections: the teacher's mental model
    // (what happened -> how the group worked -> who did what -> tools) and the
    // researcher's taxonomy (grouped by sensor modality, after Schneider et
    // al. 2025). The choice persists per browser.
    const isGroup = props.details === "Group" && !!props.sessionDevice
    const [sectionMode, setSectionMode] = useState(() => {
        try {
            return window.localStorage.getItem("podSectionMode") || "question"
        } catch (e) {
            return "question"
        }
    })
    const pickMode = (m) => {
        setSectionMode(m)
        try {
            window.localStorage.setItem("podSectionMode", m)
        } catch (e) {
            // private-mode storage failures just lose the preference
        }
    }
    const MODALITY_ORDER = [
        "Verbal",
        "Gaze",
        "Head & facial",
        "Physiological",
        "Record & logs",
        "Cross-modal",
        "Tools",
    ]

    // Heart-rate/HRV rows (Polar straps assigned on the join page). Fetched
    // here rather than in the panel so the whole section — and its modality
    // header in by-sensor mode — only exists for pods that actually have
    // strap data.
    const [hrRows, setHrRows] = useState([])
    useEffect(() => {
        if (!isGroup) return
        new ApiService()
            .httpRequestCall(
                `api/v1/sessions/${props.session.id}/device/${props.sessionDevice.id}/heartrate`,
                "GET",
                {},
            )
            .then((r) => (r.status === 200 ? r.json() : []))
            .then((d) => setHrRows(Array.isArray(d) ? d : []))
            .catch(() => setHrRows([]))
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [isGroup, props.session && props.session.id, props.sessionDevice && props.sessionDevice.id])

    // EEG rows (Muse headbands) and the pod's sync taps, which say how far
    // each headband's timing sat from the audio. Same reasoning as hrRows:
    // the section exists only for pods with headband data.
    const [eegRows, setEegRows] = useState([])
    const [syncs, setSyncs] = useState([])
    // The raw native-rate streams the pod has (raw_signals.py), offered as
    // CSV downloads; signed-in readers only, so a join page gets none.
    const [rawStreams, setRawStreams] = useState([])
    useEffect(() => {
        if (!isGroup) return
        const base = `api/v1/sessions/${props.session.id}/device/${props.sessionDevice.id}`
        const api = new ApiService()
        const get = (path) =>
            api
                .httpRequestCall(`${base}/${path}`, "GET", {})
                .then((r) => (r.status === 200 ? r.json() : []))
                .then((d) => (Array.isArray(d) ? d : []))
                .catch(() => [])
        get("eeg").then((rows) => {
            setEegRows(rows)
            // The sync report reads the audio; only worth it with EEG to align.
            if (rows.length) get("sync").then(setSyncs)
        })
        get("raw").then((streams) =>
            setRawStreams(
                streams.map((s) => ({ ...s, href: `${api.getEndpoint()}${base}/raw/${s.sensor_kind}/${s.stream}.csv` })),
            ),
        )
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [isGroup, props.session && props.session.id, props.sessionDevice && props.sessionDevice.id])
    // Array order == the "by question" order.
    const groupSections = !isGroup
        ? []
        : [
              {
                  key: "record",
                  modality: "Record & logs",
                  node: (
                      <AppSectionBoxComponent
                          type={"w-full"}
                          heading={"Transcript"}
                      >
                          <div className="flex w-full flex-col gap-3">
                              {props.group ? (
                                  <GroupViewBar
                                      group={props.group}
                                      viewId={viewId}
                                      viewKind={viewKind}
                                      videoMissing={viewVideoMissing}
                                      onSelectView={switchView}
                                      scope={props.scope}
                                      onSelectScope={props.onSelectScope}
                                      follow={follow}
                                      onFollow={setFollow}
                                  />
                              ) : null}
                              {/* Audio-only pods (and pods whose video is
                                  missing) play their audio recording in the
                                  same slot, synced to the transcript. In a
                                  group the player is the chosen view's. */}
                              {props.sessionDevice.has_video === false || videoMissing ||
                              (props.group && (viewKind === "audio" || !hasVideo(viewId))) ? (
                                  <AudioPlayer
                                      key={"a" + props.sessionDevice.id}
                                      sessionId={props.session.id}
                                      sessionDeviceId={props.sessionDevice.id}
                                      selectedTime={playerSeek}
                                      onPlaybackTime={onGroupPlaybackTime}
                                      onSegments={setVideoSegments}
                                  />
                              ) : (
                                  <VideoPlayer
                                      key={"v" + props.sessionDevice.id}
                                      sessionId={props.session.id}
                                      sessionDeviceId={props.sessionDevice.id}
                                      selectedTime={playerSeek}
                                      transcripts={
                                          groupScope
                                              ? (props.displayTranscripts || []).map((l) => ({ ...l, start_time: seekFor(l, viewId, alignment) }))
                                              : props.displayTranscripts
                                      }
                                      onPlaybackTime={onGroupPlaybackTime}
                                      onSegments={setVideoSegments}
                                      onMissing={() => {
                                          if (props.group) {
                                              setViewVideoMissing((m) => ({ ...m, [props.sessionDevice.id]: true }))
                                              setViewKind("audio")
                                          } else setVideoMissing(true)
                                      }}
                                  />
                              )}
                              <TranscriptPanel
                                  transcripts={props.displayTranscripts}
                                  start={props.startTime}
                                  end={props.endTime}
                                  onOpenFull={props.seeAllTranscripts}
                                  selectedTime={selectedTime}
                                  onSelectTime={setSelectedTime}
                                  playbackTime={playbackTime}
                                  videoSegments={groupScope ? null : videoSegments}
                                  compact
                                  transcriptionLabel={
                                      groupScope
                                          ? groupSummary(props.group, props.displayTranscripts)
                                          : transcriptionLabel
                                  }
                                  onEditText={props.editTranscriptText}
                                  onReassignSpeaker={props.reassignTranscriptRow}
                                  roster={props.roster}
                                  tagCounts={props.tagCounts}
                                  clockOrigin={props.clockOrigin}
                                  sessionClockBase={props.session && props.session.creation_date}
                                  speakerInfo={props.speakerInfo}
                              />
                          </div>
                      </AppSectionBoxComponent>
                  ),
              },
              // Instructor view only (not the student BYOD page). Renders
              // nothing unless the session opted in or a run exists — the
              // panel owns its section box for that reason.
              ...(!props.fromclient
                  ? [
                        {
                            key: "negotiation",
                            modality: "Verbal",
                            node: (
                                <NegotiationCodingPanel
                                    session={props.session}
                                    sessionDevice={props.sessionDevice}
                                    sessionDeviceId={props.sessionDevice.id}
                                    tagCounts={props.tagCounts}
                                    speakerInfo={props.speakerInfo}
                                />
                            ),
                        },
                    ]
                  : []),
              {
                  key: "dynamics",
                  modality: "Verbal",
                  node: (
                      <AppSectionBoxComponent
                          type={"w-full"}
                          heading={groupScope ? "Conversation dynamics (whole group)" : "Conversation dynamics"}
                          badge={"verbal · evidence-backed"}
                          badgeTone={"teal"}
                      >
                          <ConversationDynamicsPanel
                              sessionId={props.session.id}
                              sessionDeviceId={props.sessionDevice.id}
                              group={groupScope ? props.groupName : null}
                              refreshKey={groupScope ? props.groupVersion : props.sessionDevice.posthoc_analyzed_date}
                              speakerInfo={props.speakerInfo}
                          />
                      </AppSectionBoxComponent>
                  ),
              },
              {
                  key: "pi",
                  modality: "Verbal",
                  node: (
                      <AppSectionBoxComponent
                          type={"w-full"}
                          heading={"Participation & impact styles"}
                          badge={"verbal · exploratory"}
                          badgeTone={"amber"}
                      >
                          {(props.displayTranscripts || []).length > 0 && (
                              <div className="mb-2">
                                  <ModelNote label={models && models.participation && models.participation.label} fallback="sentence-transformer semantic cohesion (all-mpnet-base-v2)" />
                              </div>
                          )}
                          <AppGroupFeaturesComponent
                              transcripts={props.displayTranscripts}
                              speakers={props.speakers}
                          />
                      </AppSectionBoxComponent>
                  ),
              },
              ...((props.displayVideoMetrics || []).length > 0
                  ? [
                        {
                            key: "joint-attention",
                            modality: "Gaze",
                            node: (
                                <AppSectionBoxComponent
                                    type={"w-full"}
                                    heading={"Joint visual attention"}
                                    badge={"gaze · evidence-backed"}
                                    badgeTone={"teal"}
                                >
                                    <div className="mb-2">
                                        <ModelNote label={models && models.attention && models.attention.label} fallback="Gaze-LLE (DINOv2, open SOTA)" />
                                    </div>
                                    <JointAttentionPanel
                                        sessionId={props.session.id}
                                        sessionDeviceId={props.sessionDevice.id}
                                    />
                                </AppSectionBoxComponent>
                            ),
                        },
                        {
                            key: "video-analytics",
                            modality: "Head & facial",
                            node: (
                                <AppSectionBoxComponent
                                    type={"w-full"}
                                    heading={"Video analytics"}
                                    badge={"gaze+facial · exploratory"}
                                    badgeTone={"amber"}
                                >
                                    <VideoAnalyticsPanel
                                        videometrics={props.displayVideoMetrics}
                                        start={props.startTime}
                                        end={props.endTime}
                                        models={models}
                                        playbackTime={playbackTime}
                                        onSeek={setSelectedTime}
                                        sessionId={props.session.id}
                                        sessionDeviceId={props.sessionDevice.id}
                                    />
                                </AppSectionBoxComponent>
                            ),
                        },
                    ]
                  : []),
              ...(hrRows.length > 0
                  ? [
                        {
                            key: "heart-rate",
                            modality: "Physiological",
                            node: (
                                <AppSectionBoxComponent
                                    type={"w-full"}
                                    heading={"Heart rate & HRV"}
                                    badge={"polar strap · sensor data"}
                                    badgeTone={"teal"}
                                >
                                    <HeartRatePanel
                                        rows={hrRows}
                                        speakers={props.speakers}
                                    />
                                </AppSectionBoxComponent>
                            ),
                        },
                    ]
                  : []),
              ...(eegRows.length > 0
                  ? [
                        {
                            key: "eeg",
                            modality: "Physiological",
                            node: (
                                <AppSectionBoxComponent
                                    type={"w-full"}
                                    heading={"EEG"}
                                    badge={"muse headband · exploratory"}
                                    badgeTone={"amber"}
                                >
                                    <EegPanel
                                        rows={eegRows}
                                        speakers={props.speakers}
                                        syncs={syncs}
                                        rawStreams={rawStreams}
                                    />
                                </AppSectionBoxComponent>
                            ),
                        },
                    ]
                  : []),
              {
                  key: "speakers",
                  modality: "Cross-modal",
                  node: (
                      <AppSectionBoxComponent
                          type={"w-full"}
                          heading={groupScope ? "Speakers (both teams)" : "Speakers"}
                      >
                          <SpeakerCardsPanel
                              sessionId={props.session.id}
                              sessionDeviceId={props.sessionDevice.id}
                              speakers={props.speakers}
                              transcripts={props.displayTranscripts}
                              onOpenSpeaker={props.loadSpeakerMetrics}
                              group={groupScope ? props.groupName : null}
                              refreshKey={groupScope ? props.groupVersion : props.sessionDevice.posthoc_analyzed_date}
                              speakerInfo={props.speakerInfo}
                          />
                      </AppSectionBoxComponent>
                  ),
              },
              {
                  key: "posthoc",
                  modality: "Tools",
                  node: (
                      <AppSectionBoxComponent
                          type={"w-full"}
                          heading={"Post-Hoc Analysis"}
                      >
                          <PosthocTrigger
                              models={models}
                              session={props.session}
                              sessionDevice={props.sessionDevice}
                              sessionDeviceId={props.sessionDevice.id}
                              lastAnalyzed={props.sessionDevice.posthoc_analyzed_date}
                              speakers={props.speakers}
                              transcripts={props.displayTranscripts}
                          />
                      </AppSectionBoxComponent>
                  ),
              },
          ]

    return (
        <>
            {props.speakers && (
                <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-6">
                    {props.details !== "Group" &&
                        props.details !== "Individual" && (
                            <div className="flex w-full flex-col gap-2 @sm:flex-row">
                                <select
                                    id="speaker1"
                                    className="dropdown small-section"
                                    value={props.selectedSpkrId1}
                                    onChange={(e) =>
                                        props.setSelectedSpkrId1(
                                            parseInt(e.target.value, 10),
                                        )
                                    }
                                >
                                    <option value="-1">
                                        Select Participant
                                    </option>
                                    {props.speakers.map((speaker) => (
                                        <option
                                            key={speaker["id"]}
                                            value={speaker["id"]}
                                        >
                                            {speaker["alias"]}
                                        </option>
                                    ))}
                                </select>

                                {props.details === "Comparison" && (
                                    <select
                                        id="speaker2"
                                        className="dropdown small-section"
                                        value={props.selectedSpkrId2}
                                        onChange={(e) =>
                                            props.setSelectedSpkrId2(
                                                parseInt(e.target.value, 10),
                                            )
                                        }
                                    >
                                        <option value="-1">
                                            Select Participant
                                        </option>
                                        {props.speakers.map((speaker) => (
                                            <option
                                                key={speaker["id"]}
                                                value={speaker["id"]}
                                            >
                                                {speaker["alias"]}
                                            </option>
                                        ))}
                                    </select>
                                )}
                            </div>
                        )}
                    {isGroup && (
                        <div className="flex flex-col gap-2 @sm:flex-row @sm:items-center">
                            <div className="min-w-0 grow">
                                <SessionVitals
                                    sessionId={props.session.id}
                                    sessionDeviceId={props.sessionDevice.id}
                                />
                            </div>
                            <div className="flex flex-none overflow-hidden rounded-lg border border-tiilt-line text-xs">
                                <button
                                    type="button"
                                    onClick={() => pickMode("question")}
                                    className={
                                        "px-2.5 py-1.5 transition " +
                                        (sectionMode === "question"
                                            ? "bg-tiilt-soft font-semibold text-tiilt-ink"
                                            : "bg-white text-tiilt-muted hover:bg-tiilt-ground")
                                    }
                                >
                                    By question
                                </button>
                                <button
                                    type="button"
                                    onClick={() => pickMode("sensor")}
                                    className={
                                        "px-2.5 py-1.5 transition " +
                                        (sectionMode === "sensor"
                                            ? "bg-tiilt-soft font-semibold text-tiilt-ink"
                                            : "bg-white text-tiilt-muted hover:bg-tiilt-ground")
                                    }
                                >
                                    By sensor
                                </button>
                            </div>
                        </div>
                    )}

                    <AppSectionBoxComponent
                        type={"w-full"}
                        heading={"Adjust time range"}
                    >
                        <AppTimelineSlider
                            id="timeSlider"
                            inputChanged={props.setRange}
                        />
                    </AppSectionBoxComponent>

                    {isGroup &&
                        sectionMode === "question" &&
                        groupSections.map((s) => (
                            <React.Fragment key={s.key}>
                                {s.node}
                            </React.Fragment>
                        ))}

                    {isGroup &&
                        sectionMode === "sensor" &&
                        MODALITY_ORDER.filter((m) =>
                            groupSections.some((s) => s.modality === m),
                        ).map((m) => (
                            <React.Fragment key={m}>
                                <div className="font-ahamono mt-1 text-[11px] tracking-wider text-tiilt-muted uppercase">
                                    {m}
                                </div>
                                {groupSections
                                    .filter((s) => s.modality === m)
                                    .map((s) => (
                                        <React.Fragment key={s.key}>
                                            {s.node}
                                        </React.Fragment>
                                    ))}
                            </React.Fragment>
                        ))}

                    {props.details !== "Comparison" &&
                        props.details !== "Group" &&
                        (props.spkr1VideoMetrics || []).length > 0 && (
                            <AppSectionBoxComponent
                                type={"w-full"}
                                heading={"Video analytics"}
                                badge={"gaze+facial · exploratory"}
                                badgeTone={"amber"}
                            >
                                <VideoAnalyticsPanel
                                    videometrics={props.spkr1VideoMetrics}
                                    start={props.startTime}
                                    end={props.endTime}
                                    models={models}
                                    playbackTime={playbackTime}
                                    onSeek={setSelectedTime}
                                    sessionId={props.session.id}
                                    sessionDeviceId={props.sessionDevice.id}
                                />
                            </AppSectionBoxComponent>
                        )}

                    {props.details !== "Comparison" &&
                        !(props.details === "Group" && props.sessionDevice) && (
                        <AppSectionBoxComponent
                            type={"w-full"}
                            heading={"Transcript"}
                        >
                            <TranscriptPanel
                                transcripts={
                                    props.details === "Group"
                                        ? props.displayTranscripts
                                        : props.spkr1Transcripts
                                }
                                start={props.startTime}
                                end={props.endTime}
                                onOpenFull={props.seeAllTranscripts}
                                selectedTime={selectedTime}
                                onSelectTime={setSelectedTime}
                                playbackTime={playbackTime}
                                transcriptionLabel={transcriptionLabel}
                                onEditText={props.editTranscriptText}
                                onReassignSpeaker={props.reassignTranscriptRow}
                                roster={props.roster}
                                tagCounts={props.tagCounts}
                                clockOrigin={props.clockOrigin}
                                sessionClockBase={props.session && props.session.creation_date}
                                speakerInfo={props.speakerInfo}
                            />
                        </AppSectionBoxComponent>
                    )}

                    {/* "Participation across sessions" moved to the student
                        profile page (/students/:id) — cross-session data was
                        out of place inside a single discussion's dashboard. */}

                    {props.details !== "Group" &&
                        boxOn("Video Metrics") && (
                            <AppSectionBoxComponent
                                type={"w-full"}
                                heading={`Visual Analytics`}
                                badge={"gaze+facial · exploratory"}
                                badgeTone={"amber"}
                            >
                                {(props.spkr1VideoMetrics || []).length > 0 && (
<div className="mb-2 flex flex-col gap-0.5">
                                    <ModelNote label={models && models.attention && models.attention.label} fallback="Gaze-LLE (DINOv2, open SOTA)" />
                                    <ModelNote label={models && models.emotion && models.emotion.label} fallback="HSEmotion EfficientNet-B2 (AffectNet-8)" />
                                    <ModelNote label={models && models.objects && models.objects.label} fallback="YOLO11m object detector (COCO)" />
                                </div>
)}
                                <Suspense fallback={null}>
                                <AppIndividualVideoFeaturesComponent
                                    session={props.session}
                                    videometrics={props.spkr1VideoMetrics}
                                    spkrId={props.getSpeakerAliasFromID(
                                        props.selectedSpkrId1,
                                    )}
                                    showFeatures={props.showFeatures}
                                />
                                </Suspense>
                            </AppSectionBoxComponent>
                        )}

                    {props.details === "Comparison" &&
                        boxOn("Video Metrics") && (
                            <AppSectionBoxComponent
                                type={"w-full"}
                                heading={`Visual Analytics`}
                                badge={"gaze+facial · exploratory"}
                                badgeTone={"amber"}
                            >
                                {(props.spkr2VideoMetrics || []).length > 0 && (
<div className="mb-2 flex flex-col gap-0.5">
                                    <ModelNote label={models && models.attention && models.attention.label} fallback="Gaze-LLE (DINOv2, open SOTA)" />
                                    <ModelNote label={models && models.emotion && models.emotion.label} fallback="HSEmotion EfficientNet-B2 (AffectNet-8)" />
                                    <ModelNote label={models && models.objects && models.objects.label} fallback="YOLO11m object detector (COCO)" />
                                </div>
)}
                                <Suspense fallback={null}>
                                <AppIndividualVideoFeaturesComponent
                                    session={props.session}
                                    videometrics={props.spkr2VideoMetrics}
                                    spkrId={props.getSpeakerAliasFromID(
                                        props.selectedSpkrId2,
                                    )}
                                    showFeatures={props.showFeatures}
                                />
                                </Suspense>
                            </AppSectionBoxComponent>
                        )}

                    {props.details !== "Group" &&
                        boxOn("Participation") && (
                            <AppSectionBoxComponent
                                type={"w-full"}
                                heading={`Participation and Impact Style`}
                                badge={"verbal · exploratory"}
                                badgeTone={"amber"}
                            >
                                {(props.spkr1Transcripts || []).length > 0 && (
<div className="mb-2">
                                    <ModelNote label={models && models.participation && models.participation.label} fallback="sentence-transformer semantic cohesion (all-mpnet-base-v2)" />
                                </div>
)}
                                <AppIndividualFeaturesComponent
                                    session={props.session}
                                    transcripts={props.spkr1Transcripts}
                                    spkrId={props.selectedSpkrId1}
                                    showFeatures={props.showFeatures}
                                />
                            </AppSectionBoxComponent>
                        )}

                    {props.details === "Comparison" &&
                        boxOn("Participation") && (
                            <AppSectionBoxComponent
                                type={"w-full"}
                                heading={`Participation and Impact Style`}
                                badge={"verbal · exploratory"}
                                badgeTone={"amber"}
                            >
                                {(props.spkr2Transcripts || []).length > 0 && (
<div className="mb-2">
                                    <ModelNote label={models && models.participation && models.participation.label} fallback="sentence-transformer semantic cohesion (all-mpnet-base-v2)" />
                                </div>
)}
                                <AppIndividualFeaturesComponent
                                    session={props.session}
                                    transcripts={props.spkr2Transcripts}
                                    spkrId={props.selectedSpkrId2}
                                    showFeatures={props.showFeatures}
                                />
                            </AppSectionBoxComponent>
                        )}

                </div>
            )}
        </>
    )
}

export { AppInfographicsComparison }
