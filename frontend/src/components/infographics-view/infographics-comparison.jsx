import { AppSectionBoxComponent } from "../section-box/section-box-component"
import { TranscriptPanel } from "../transcript-panel/transcript-panel"
import { VideoAnalyticsPanel } from "../video-analytics/video-analytics-panel"
import { ConversationDynamicsPanel } from "../conversation-dynamics/conversation-dynamics-panel"
import { DiscussionSummaryPanel } from "../discussion-summary/discussion-summary-panel"
import { VideoPlayer } from "../video-player/video-player"
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
import React, { useState, useEffect, lazy, Suspense } from "react"
import { ApiService } from "../../services/api-service"

// Heaviest chunk in the app (~288 KB). Only rendered when a pod has video
// metrics AND the video box is expanded, so defer it until then — text-only
// pods and unexpanded views never pay for it.
const AppIndividualVideoFeaturesComponent = lazy(() =>
    import("../individualVideometrics/video-features-component").then((m) => ({
        default: m.AppIndividualVideoFeaturesComponent,
    })),
)

function AppInfographicsComparison(props) {
    // Shared selection linking the transcript with the Expression & Thinking
    // Style graphs. It is the start_time of the selected utterance, which maps
    // 1:1 to a point on each feature graph.
    const [selectedTime, setSelectedTime] = useState(null)
    // Video segment layout, lifted from the player so the transcript panel can
    // show each line on the video's clock (recording-relative) rather than
    // session time — the two differ by however long setup ran before recording.
    const [videoSegments, setVideoSegments] = useState(null)
    // Session-relative playhead of the discussion video, so the transcript can
    // follow along while it plays.
    const [playbackTime, setPlaybackTime] = useState(null)

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
                          heading={"Session video & transcript"}
                      >
                          <div className="flex w-full flex-col gap-3">
                              <VideoPlayer
                                  sessionId={props.session.id}
                                  sessionDeviceId={props.sessionDevice.id}
                                  selectedTime={selectedTime}
                                  transcripts={props.displayTranscripts}
                                  onPlaybackTime={setPlaybackTime}
                                  onSegments={setVideoSegments}
                              />
                              <TranscriptPanel
                                  transcripts={props.displayTranscripts}
                                  start={props.startTime}
                                  end={props.endTime}
                                  onOpenFull={props.seeAllTranscripts}
                                  selectedTime={selectedTime}
                                  onSelectTime={setSelectedTime}
                                  playbackTime={playbackTime}
                                  videoSegments={videoSegments}
                                  compact
                                  transcriptionLabel={transcriptionLabel}
                                  onEditText={props.editTranscriptText}
                                  onReassignSpeaker={props.reassignTranscriptRow}
                                  roster={props.roster}
                                  tagCounts={props.tagCounts}
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
                                    sessionDeviceId={props.sessionDevice.id}
                                    tagCounts={props.tagCounts}
                                />
                            ),
                        },
                    ]
                  : []),
              {
                  key: "summary",
                  modality: "Record & logs",
                  node: (
                      <AppSectionBoxComponent
                          type={"w-full"}
                          heading={"AI discussion summary"}
                      >
                          <DiscussionSummaryPanel
                              sessionId={props.session.id}
                              sessionDeviceId={props.sessionDevice.id}
                          />
                      </AppSectionBoxComponent>
                  ),
              },
              {
                  key: "dynamics",
                  modality: "Verbal",
                  node: (
                      <AppSectionBoxComponent
                          type={"w-full"}
                          heading={"Conversation dynamics"}
                          badge={"verbal · evidence-backed"}
                          badgeTone={"teal"}
                      >
                          <ConversationDynamicsPanel
                              sessionId={props.session.id}
                              sessionDeviceId={props.sessionDevice.id}
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
              {
                  key: "speakers",
                  modality: "Cross-modal",
                  node: (
                      <AppSectionBoxComponent
                          type={"w-full"}
                          heading={"Speakers"}
                      >
                          <SpeakerCardsPanel
                              sessionId={props.session.id}
                              sessionDeviceId={props.sessionDevice.id}
                              speakers={props.speakers}
                              transcripts={props.displayTranscripts}
                              onOpenSpeaker={props.loadSpeakerMetrics}
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
