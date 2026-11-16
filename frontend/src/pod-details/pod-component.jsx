import { SessionService } from "../services/session-service";
import { SessionDeviceModel } from "../models/session-device";
import { SpeakerModel } from "../models/speaker";
import { useEffect, useMemo, useState, useRef } from "react";
import { useNavigate, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { ApiService } from "../services/api-service";
import { PodComponentPages } from "./html-pages";
import { filterSortByDevice, indexSpeakerInfo } from "../globals";
import { decorateLines } from "./group-time";

// The dashboard views live in the URL (?view=...&speaker=...) so they are
// linkable, bookmarkable, and reload/back-button safe. "Group" is the clean
// URL. Slugs <-> the legacy `details` state values.
// "Comparison" (the two-dropdown Individual Comparison view) is retired for
// pods: speaker comparison lives in the Group view's multi-speaker panels, and
// per-speaker detail is the Individual view, opened from the Speakers cards.
const VIEW_SLUGS = {
  Group: "group",
  "Reflection Dashboard": "reflection",
  Individual: "individual",
};
const SLUG_VIEWS = Object.fromEntries(
  Object.entries(VIEW_SLUGS).map(([view, slug]) => [slug, view]),
);

function PodComponent() {
  const [activeSessionService] = useOutletContext();
  const { sessionId, sessionDeviceId } = useParams();
  // Start from what the session store already holds (the overview loaded
  // it): session and pod first, so the first paint has the title and the
  // time window, then the pod's rows below.
  const [sessionDevice, setSessionDevice] = useState(() => activeSessionService.getSessionDevice(sessionDeviceId) || {});
  const [session, setSession] = useState(() => activeSessionService.getSession() || {});
  // A breakout group's laptops record the same conversation from their own
  // sides: the pod page of any of them is the group's page. The transcript
  // is the merge the coder reads (scope "group"; "device" = this laptop's
  // own rows), and the per-laptop analyses follow the laptop chosen as the
  // view, which also drives the player.
  const [group, setGroup] = useState(null);
  const [groupVersion, setGroupVersion] = useState(0);   // bumps on every merge load: panels refetch
  const [viewDeviceId, setViewDeviceId] = useState(() => parseInt(sessionDeviceId, 10));
  const [scope, setScope] = useState("group");
  const groupName = (sessionDevice && sessionDevice.negotiation_group) || null;
  const viewDeviceRef = useRef(parseInt(sessionDeviceId, 10));
  // Rows too, so the first paint shows them instead of an empty panel that
  // fills in a moment later. Live updates arrive through the subscriptions
  // in the mount effect.
  const [transcripts, setTranscripts] = useState(() =>
    filterSortByDevice(activeSessionService.getTranscripts().getValue(), sessionDeviceId, "start_time"));
  const [videoMetrics, setVideoMetrics] = useState(() =>
    filterSortByDevice(activeSessionService.getVideoMetrics().getValue(), sessionDeviceId, "time_stamp"));
  // The view follows the URL's laptop whenever the URL changes; the video
  // metrics follow the view.
  useEffect(() => {
    setViewDeviceId(parseInt(sessionDeviceId, 10));
    setScope("group");
  }, [sessionDeviceId]);
  useEffect(() => {
    viewDeviceRef.current = viewDeviceId;
    setVideoMetrics(filterSortByDevice(activeSessionService.getVideoMetrics().getValue() || [], viewDeviceId, "time_stamp"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewDeviceId]);
  // The group's merged transcript, refetched after a correction so the merge
  // reflects the edited row.
  const groupFetchSeq = useRef(0);
  const loadGroup = () => {
    if (!groupName || !session.id) return;
    const seq = ++groupFetchSeq.current;
    new SessionService()
      .getGroupTranscript(session.id, groupName)
      .then((r) => (r.status === 200 ? r.json() : null))
      .then((d) => {
        if (d && seq === groupFetchSeq.current) {
          setGroup(d);
          setGroupVersion((v) => v + 1);
        }
      })
      .catch(() => {});
  };
  useEffect(() => {
    setGroup(null);
    loadGroup();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupName, session.id]);
  // A finished post-hoc run reaches the page as a device update with a new
  // analysed date (the store reloads that pod's rows on it). This pod's
  // record refreshes, and the group's merge is rebuilt when any of the
  // group's laptops was re-analysed.
  const loadGroupRef = useRef(null);
  loadGroupRef.current = loadGroup;
  const groupNameRef = useRef(groupName);
  groupNameRef.current = groupName;
  const analysedKeyRef = useRef("");
  useEffect(() => {
    const urlId = parseInt(sessionDeviceId, 10);
    const sub = activeSessionService.sessionDeviceSource.subscribe((devices) => {
      const list = devices || [];
      const mine = list.find((d) => d.id === urlId);
      if (mine) setSessionDevice((prev) => (prev && prev.posthoc_analyzed_date === mine.posthoc_analyzed_date && prev.name === mine.name ? prev : mine));
      const name = groupNameRef.current || (mine && mine.negotiation_group);
      const key = list
        .filter((d) => d.id === urlId || (name && d.negotiation_group === name))
        .map((d) => d.id + ":" + (d.posthoc_analyzed_date || ""))
        .join("|");
      if (analysedKeyRef.current && key !== analysedKeyRef.current && loadGroupRef.current) loadGroupRef.current();
      analysedKeyRef.current = key;
    });
    return () => sub.unsubscribe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionDeviceId]);
  // Take the session again when it ends (session_update), so the live
  // wearable sections (live-rows.js) stop polling. Only then: any other
  // update keeps the same object and re-renders nothing.
  useEffect(() => {
    const ended = (s) => (s && s.end_date ? s.end_date.getTime() : null);
    const sub = activeSessionService.sessionSource.subscribe((s) => {
      if (s) setSession((prev) => (prev && prev.id === s.id && ended(prev) === ended(s) ? prev : s));
    });
    return () => sub.unsubscribe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // False until the store has received the session's transcript history;
  // the page shows a spinner rather than "no transcript" meanwhile.
  const [transcriptsReady, setTranscriptsReady] = useState(() => activeSessionService.transcriptsReady.getValue());
  const [currentTranscript, setCurrentTranscript] = useState({});
  const [currentForm, setCurrentForm] = useState("");
  // Human-visible reason when the AI reflection can't load (LLM/api errors
  // used to fail silently — the toolbar button just did nothing).
  const [llmError, setLlmError] = useState("");
  const [, setSessionClosing] = useState(false);
  const [subscriptions] = useState([]);
  const [deleteDeviceToggle, setDeleteDeviceToggle] = useState(false);
  const [timeRange, setTimeRange] = useState([0, 1]);
  const [details, setDetails] = useState("Group");
  const [speakers, setSpeakers] = useState([]);
  const [selectedSpkrId1, setSelectedSpkrId1] = useState(-1);
  const [open, setOpen] = useState(true);
  const [selectedSpkralias, setSelectedSpkralias] = useState("");
  const [participantIDReflectionDashboard, setParticipantIDRefectionDashboard] = useState("")
  // Participant whose AI narrative is still generating (inline banner, not a
  // blocking dialog); guards against stale responses landing after a switch.
  const [llmPendingFor, setLlmPendingFor] = useState("")
  const latestParticipantRequest = useRef("")
  const prefetchStarted = useRef(false)
  const [searchParams, setSearchParams] = useSearchParams();
  const synthesizedFeedbackMetrics = useRef(null);
  const participants = useRef([])
  const selectedParticipantSynthesizedData = useRef({})
  const selectedParticipantLLMAnalysis = useRef(null)
  const llmSessionAnalysis = useRef({})
  const promptHistory = useRef({})
  const [currentParticipant, setCurrentParticipant] = useState("")
  const promptResponses = useRef({})
  const [isThinking, setIsThinking] = useState(false)
  const [selectedMomentIdAndIndex, setSelectedMomentIdAndIndex] = useState(null);
  const navigate = useNavigate();

  useEffect(() => {
    // if (endTime === undefined) {
    if (Object.keys(session).length <= 0) {
      const sessionSub = activeSessionService.getSession();
      if (sessionSub !== undefined) {
        setSession(sessionSub);
      }
    }

    if (Object.keys(sessionDevice).length <= 0) {
      const deviceSub = activeSessionService.getSessionDevice(sessionDeviceId);
      if (deviceSub !== undefined) {
        setSessionDevice(deviceSub);
      } else {
        // Direct URL / stale store: fetch the pod so per-run provenance
        // (posthoc_models) and metadata are available regardless of how the
        // page was reached.
        new SessionService()
          .getSessionDeviceForClient(sessionDeviceId)
          .then((r) => (r.status === 200 ? r.json() : null))
          .then((d) => d && setSessionDevice(SessionDeviceModel.fromJson(d)))
          .catch(() => {});
      }
    }
    // Keep the SUBSCRIPTION (what .subscribe returns), not the observable
    // — the old code pushed the observable, so cleanup never unsubscribed
    // and every pod visit leaked a live subscription firing setState on an
    // unmounted component for the rest of the session. Always subscribe:
    // the initial state came from the store, but live rows still arrive
    // through here.
    subscriptions.push(
      activeSessionService.getTranscripts().subscribe((e) => {
        setTranscripts(filterSortByDevice(e || [], sessionDeviceId, "start_time"));
      }),
    );
    subscriptions.push(
      activeSessionService.transcriptsReady.subscribe((ready) => setTranscriptsReady(!!ready)),
    );
    if (synthesizedFeedbackMetrics.current === null) {
      const fetchData = new SessionService().getSynthesizedFeedbackMetrics(sessionId, sessionDeviceId);
      fetchData.then(
        (response) => {
          if (response.status === 200)
            response.json().then((jsonObj) => {
              synthesizedFeedbackMetrics.current = jsonObj;
              participants.current = Object.keys(synthesizedFeedbackMetrics.current["participants_level"])
            });
        },
        (apierror) => {
          console.error("podcomponent useEffect getSynthesizedFeedbackMetrics", apierror);
        }
      );
    }



    subscriptions.push(
      activeSessionService.getVideoMetrics().subscribe((e) => {
        setVideoMetrics(filterSortByDevice(e || [], viewDeviceRef.current, "time_stamp"));
      }),
    );


    // Refresh based on timeslider. Local id, not the intervalId state: the
    // tick and the cleanup both read the state value captured BEFORE
    // setIntervalId committed (undefined), so the 2s interval never stopped.
    const refreshIntervalId = setInterval(() => {
      if (session === undefined || !session.recording) {
        clearInterval(refreshIntervalId);
      }
    }, 2000);

    return () => {
      // unsubscribe() is idempotent; the old `if (sub.closed)` guard was
      // inverted and only ever "cleaned" already-closed subscriptions.
      subscriptions.forEach((sub) => sub.unsubscribe());
      subscriptions.length = 0;
      clearInterval(refreshIntervalId);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The visible window and everything filtered by it are derived during
  // render, not set from effects: with effects the first paint showed an
  // empty panel and empty charts, then filled in one or two frames later.
  // The window is re-read when the data or the slider changes (a live
  // session's length grows with the clock, so the end moves with new rows).
  const { startTime, endTime } = useMemo(() => {
    const sessionLen = Object.keys(session).length > 0 ? session.length : 0;
    return {
      startTime: Math.round(sessionLen * timeRange[0] * 100) / 100,
      endTime: Math.round(sessionLen * timeRange[1] * 100) / 100,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, timeRange, transcripts, videoMetrics]);
  const groupLines = useMemo(() => (group ? decorateLines(group.lines, group.pods) : []), [group]);
  // The group's display names and team colours ("Pat 1", "Sandy 2"): the
  // server labels every roster of the group, so both scopes read the same.
  const speakerInfo = useMemo(() => indexSpeakerInfo(group ? group.speakers : null), [group]);
  const groupScope = !!group && scope === "group";
  const sourceTranscripts = groupScope ? groupLines : transcripts;
  // The elapsed clock's zero: the first line of what the page shows (a
  // stored stamp counts from the session's creation, hours before class).
  const clockOrigin = useMemo(() => {
    let first = null;
    for (const t of sourceTranscripts) if (t.start_time != null && (first == null || t.start_time < first)) first = t.start_time;
    return first == null ? 0 : first;
  }, [sourceTranscripts]);
  const displayTranscripts = useMemo(
    () => sourceTranscripts.filter((t) => t.start_time >= startTime && t.start_time <= endTime),
    [sourceTranscripts, startTime, endTime],
  );
  const displayVideoMetrics = useMemo(
    () => videoMetrics.filter((v) => v.time_stamp >= startTime && v.time_stamp <= endTime),
    [videoMetrics, startTime, endTime],
  );
  const spkr1Transcripts = useMemo(
    () => displayTranscripts.filter((t) => t.speaker_id === selectedSpkrId1),
    [displayTranscripts, selectedSpkrId1],
  );
  const spkr1VideoMetrics = useMemo(() => {
    const speaker = speakers.find((sp) => sp.id === selectedSpkrId1);
    if (!speaker) return [];
    return displayVideoMetrics.filter((v) => v.student_username === speaker.alias);
  }, [displayVideoMetrics, selectedSpkrId1, speakers]);

  const groupPodKey = groupScope && group && group.pods ? group.pods.map((p) => p.id).join(",") : "";
  useEffect(() => {
    if (viewDeviceId && session.id) getSpeakersRef.current();
    else setSpeakers([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewDeviceId, session, groupPodKey]);

  useEffect(() => {
    if (participantIDReflectionDashboard !== "") {
      // Switch the view immediately — metrics are already local; the AI
      // narrative streams in via the pending banner when not yet cached.
      latestParticipantRequest.current = participantIDReflectionDashboard
      setCurrentParticipant(participantIDReflectionDashboard)
      extractParticipantDataRef.current(participantIDReflectionDashboard)
    }
  }, [participantIDReflectionDashboard])

  const ResetTimeRange = (values) => {
    setTimeRange(values);
  };


  const getSpeakerAliasFromID = (selectedSpkrId) => {
    if (selectedSpkrId !== -1) {
      const speaker = speakers.filter((s) => s.id === selectedSpkrId)
      if (speaker.length !== 0) {
        return speaker[0].alias
      }
    } else {
      return -1
    }
  }

  const navigateToSession = () => {
    navigate("/sessions/" + session.id);
  };

  const onSessionClosing = (isClosing) => {
    setSessionClosing(isClosing);
  };

  const seeAllTranscripts = () => {
    if (currentTranscript != null && currentTranscript.id != null) {
      navigate(
        "/sessions/" +
        session.id +
        "/pods/" +
        sessionDeviceId +
        "/transcripts?index=" +
        currentTranscript.id
      );
    } else {
      navigate(
        "/sessions/" + session.id + "/pods/" + sessionDeviceId + "/transcripts"
      );
    }
  };

  const loading = (details) => {
    if(details === "Reflection Dashboard"){
      return Object.keys(llmSessionAnalysis.current).length <= 0
    }else{
    // The store has not delivered this session's transcript history yet.
    return !transcriptsReady;
    }
  };

  const getSpeakersRef = useRef(null);
  // The speakers the page works with: in group scope every laptop's roster
  // (both teams, one card per student), else the view laptop's.
  const getSpeakers = () => {
    const ids = groupScope && group && group.pods && group.pods.length
      ? group.pods.map((p) => p.id)
      : [viewDeviceId];
    Promise.all(ids.map((id) =>
      new SessionService().getSessionDeviceSpeakers(session.id, id)
        .then((r) => (r.status === 200 ? r.json() : []))
        .catch((apierror) => { console.error("podcomponent func getspeakers 1", apierror); return []; })
    )).then((lists) => {
      const seen = new Set();
      const merged = [];
      lists.forEach((jsonObj) => {
        (SpeakerModel.fromJsonList(jsonObj || []) || []).forEach((sp) => {
          const key = sp.alias || sp.id;
          if (seen.has(key)) return;
          seen.add(key);
          merged.push(sp);
        });
      });
      setSpeakers(merged);
    });
  };
  getSpeakersRef.current = getSpeakers;

  const removeDeviceFromSession = (deleteDevice = false) => {
    const fetchData = new SessionService().removeDeviceFromSession(
      session.id,
      sessionDeviceId,
      deleteDevice
    );
    fetchData.then(
      (response) => {
        if (response.status === 200) {
          closeDialog();
          if (deleteDevice) {
            navigateToSession();
          }
        }
      },
      (apierror) => {
        console.error("podcomponent func removedevicesession 1", apierror);
      }
    );
  };

  const onClickedTimeline = (transcript) => {
    setCurrentForm("Transcript");
    setCurrentTranscript(transcript);
    setEditDraft(transcript && transcript.transcript ? transcript.transcript : "");
    // Editing is the default action for a clicked segment — the dialog
    // opens straight into the inline editor.
    setPodEditing(true);
  };

  // Human corrections from the transcript dialog: fix the text, or fix who
  // said it (same endpoints as the full transcripts page). Editing is
  // inline like a title rename: click the text, Enter/blur saves.
  const [editDraft, setEditDraft] = useState("");
  const [podEditing, setPodEditing] = useState(false);
  // In group scope the roster also holds every name the merge carries, so a
  // line heard by the other laptop can be given to one of its students.
  const roster = [...new Set(speakers.map((s) => s.alias).filter(Boolean)
    .concat(groupScope ? groupLines.map((l) => l.speaker_tag).filter(Boolean) : []))].sort();
  const tagCounts = sourceTranscripts.reduce((m, t) => {
    if (t.speaker_tag) m[t.speaker_tag] = (m[t.speaker_tag] || 0) + 1;
    return m;
  }, {});

  // Reassign any row by id (transcript panel); the dialog variant below
  // just targets the currently open transcript.
  const reassignTranscriptRow = async (transcriptId, alias, applyToTag, guest) => {
    const target = sourceTranscripts.find((t) => t.id === transcriptId) || transcripts.find((t) => t.id === transcriptId);
    const oldTag = target ? target.speaker_tag : null;
    const res = await new ApiService().httpRequestCall(
      `api/v1/transcripts/${transcriptId}/reassign`, "POST",
      { alias, apply_to_tag: !!applyToTag, allow_guest: !!guest });
    if (res.status !== 200) return;
    const hitRow = (row) => (applyToTag && oldTag ? row.speaker_tag === oldTag : row.id === transcriptId);
    setTranscripts(transcripts.map((row) => (hitRow(row) ? { ...row, speaker_tag: alias } : row)));
    setGroup((g) => (g ? { ...g, lines: g.lines.map((row) => (hitRow(row) ? { ...row, speaker_tag: alias } : row)) } : g));
    loadGroup();
    if (currentTranscript && currentTranscript.id === transcriptId) {
      setCurrentTranscript({ ...currentTranscript, speaker_tag: alias });
    }
    if (guest) getSpeakers();
  };

  const reassignTranscriptSpeaker = (alias, applyToTag, guest) => {
    if (!currentTranscript || currentTranscript.id == null) return;
    return reassignTranscriptRow(currentTranscript.id, alias, applyToTag, guest);
  };

  const saveTranscriptText = async () => {
    const t = currentTranscript;
    const clean = (editDraft || "").trim();
    if (!t || t.id == null || !clean || clean === t.transcript) {
      setPodEditing(false);
      setEditDraft(t && t.transcript ? t.transcript : "");
      return;
    }
    const res = await new ApiService().httpRequestCall(
      `api/v1/transcripts/${t.id}/edit_text`, "POST", { transcript: clean });
    if (res.status !== 200) return;
    setTranscripts(transcripts.map((row) =>
      row.id === t.id ? { ...row, transcript: clean } : row));
    setCurrentTranscript({ ...t, transcript: clean });
    setPodEditing(false);
  };

  // Inline correction from the transcript panel (id + text, no dialog).
  const editTranscriptText = async (transcriptId, text) => {
    const clean = (text || "").trim();
    const orig = sourceTranscripts.find((t) => t.id === transcriptId);
    if (!clean || !orig || orig.transcript === clean) return;
    const res = await new ApiService().httpRequestCall(
      `api/v1/transcripts/${transcriptId}/edit_text`, "POST", { transcript: clean });
    if (res.status !== 200) return;
    setTranscripts(transcripts.map((row) =>
      row.id === transcriptId ? { ...row, transcript: clean } : row));
    setGroup((g) => (g ? { ...g, lines: g.lines.map((row) => (row.id === transcriptId ? { ...row, transcript: clean } : row)) } : g));
    loadGroup();
    if (currentTranscript && currentTranscript.id === transcriptId) {
      setCurrentTranscript({ ...currentTranscript, transcript: clean });
    }
  };

  const cancelPodEdit = () => {
    setPodEditing(false);
    setEditDraft(currentTranscript && currentTranscript.transcript
      ? currentTranscript.transcript : "");
  };

  const openDialog = (form) => {
    setDeleteDeviceToggle(false);
    setCurrentForm(form);
  };

  const closeDialog = () => {
    setCurrentForm("");
  };

  /*
    const toggleDeleteVal = (val) => {
        setDeleteDeviceToggle(!val);
        setTrigger(trigger + 1);
    }

    const toggleDeleteValFalse = () => {toggleDeleteVal(false)}
    */

  // Reflect the open view into the URL. Group is the clean URL; Individual
  // carries the speaker id. No-op when the URL already matches, so the
  // param->state sync effect below can call the loaders without history spam.
  const writeViewParams = (view, speakerId) => {
    const next = new URLSearchParams();
    const slug = VIEW_SLUGS[view] || "group";
    if (slug !== "group") next.set("view", slug);
    if (slug === "individual" && speakerId != null)
      next.set("speaker", String(speakerId));
    if (next.toString() !== searchParams.toString()) setSearchParams(next);
  };

  const dashboardView = (view) => {
    setDetails(view);
    writeViewParams(view);
  };

  const buildData = (reporttype, participantId, defaultQuestionId, question) => {
    let retObj = {}
    if (reporttype === "Participant level sesssion analysis") {
      retObj["participant_name"] = participantId
      retObj["sessionid"] = sessionId
      retObj["sessiondeviceid"] = sessionDeviceId
      retObj["retrieve_existing_report"] = "true"
      retObj["participant_level_metric"] = synthesizedFeedbackMetrics.current["participants_level"][participantId]
      retObj["session_level_metric"] = synthesizedFeedbackMetrics.current["session_level"][participantId]
      retObj["group_level_metric"] = synthesizedFeedbackMetrics.current["group_level"]
    } else if (reporttype === "interactive prompting") {
      retObj["participant_name"] = participantId
      retObj["sessionid"] = sessionId
      retObj["sessiondeviceid"] = sessionDeviceId
      retObj["retrieve_existing_answer"] = "true"
      retObj["default_question_id"] = defaultQuestionId
      retObj["question"] = question
      retObj["window_level_metric"] = synthesizedFeedbackMetrics.current["window_level"]
      retObj["session_level_metric"] = synthesizedFeedbackMetrics.current["session_level"]
      retObj["group_level_metric"] = synthesizedFeedbackMetrics.current["group_level"]
    }

    return retObj
  }

  // Non-blocking participant load: the synthesized metrics for EVERY
  // participant are already in memory (one fetch per pod), so a switch shows
  // the new student's metrics instantly; only the AI narrative may need a
  // ~30s Gemini generation, which renders as an inline pending banner instead
  // of a page-blocking dialog. `background` prefetches without touching what
  // is on screen (used to warm the remaining participants after first load).
  const extractParticipantDataRef = useRef(null);
  const extractParticipantData = async (participantId, background = false) => {
    let respObj = buildData("Participant level sesssion analysis", participantId, null, null)

    if (!respObj || Object.keys(respObj).length === 0) {
      return false;
    }

    if (participantId in llmSessionAnalysis.current) {
      if (!background) {
        selectedParticipantLLMAnalysis.current = llmSessionAnalysis.current[participantId]
        selectedParticipantSynthesizedData.current = respObj
        setSelectedMomentIdAndIndex([0, selectedParticipantSynthesizedData.current.participant_level_metric[0].windowid])
        setLlmPendingFor("")
      }
      return true
    }

    if (!background) {
      // Show this participant's metrics right away; AI panels go pending.
      selectedParticipantLLMAnalysis.current = null
      selectedParticipantSynthesizedData.current = respObj
      setSelectedMomentIdAndIndex([0, respObj.participant_level_metric[0].windowid])
      setLlmPendingFor(participantId)
    }

    try {
      const response = await new SessionService().getLLMFeedbackBasedOnMetrics(respObj);

      if (response.status === 200) {
        const jsonObj = await response.json()
        llmSessionAnalysis.current[participantId] = jsonObj.answer;
        // Only update the screen if the user is still on this participant.
        if (!background && latestParticipantRequest.current === participantId) {
          selectedParticipantLLMAnalysis.current = llmSessionAnalysis.current[participantId]
          loadprompthistory(participantId)
          setLlmPendingFor("")
        }
        return true
      } else {
        const errObj = await response.json().catch(() => ({}))
        if (!background) {
          setLlmPendingFor("")
          setLlmError(
            "Couldn't generate the AI reflection: " +
            (errObj.message || ("server returned " + response.status)),
          )
        }
        return false
      }
    } catch (error) {
      if (!background) {
        setLlmPendingFor("")
        setLlmError("Couldn't generate the AI reflection: " + error)
      }
      return false
    }
  }
  extractParticipantDataRef.current = extractParticipantData;

  // After the first participant's reflection is up, quietly generate the
  // rest so switching students is instant (reports persist server-side too).
  // Cost control: prefetching every participant's AI reflection multiplied
  // each first dashboard open by group size (4-5x Gemini spend) and helped
  // exhaust the monthly cap. Reports generate on demand per participant now
  // (cached in the DB after the first time); flip to true only if budget
  // allows the instant-switching nicety.
  const PREFETCH_ALL_PARTICIPANTS = false
  const prefetchRemainingParticipants = async () => {
    if (!PREFETCH_ALL_PARTICIPANTS || prefetchStarted.current) return
    prefetchStarted.current = true
    for (const p of participants.current) {
      if (!(p in llmSessionAnalysis.current)) {
        try { await extractParticipantData(p, true) } catch { /* best effort */ }
      }
    }
  }

  const loadprompthistory = async (participantId) => {
    try {
      const response = await new SessionService().get_llm_question_answer_interactions(sessionId, sessionDeviceId, participantId);

      if (response.status === 200) {
        const jsonObj = await response.json()
        promptHistory.current[participantId] = jsonObj
      } else if (response.status === 400) {
        // console.log("LLM api response", response.message)
      }
    } catch (error) {
      console.error(
        "podcomponent loadprompthistory",
        error,
      )
    }
  }

  const loadReflectiondashboard = async (view) => {
    if (participants.current.length === 0) {
      setLlmError(
        "No synthesized participant data for this pod yet — run a full analysis first.",
      )
      return
    }
    if (participants.current.length > 0) {
      const first = participants.current[0]
      latestParticipantRequest.current = first
      setCurrentParticipant(first)
      setDetails(view);
      writeViewParams(view);
      const actionstatus = await extractParticipantData(first)
      if (actionstatus) {
        // Warm the other participants in the background so switching between
        // students is instant.
        prefetchRemainingParticipants()
      }
    }
  };

  const interactivePromptFnc = async (participantId, default_question_id, question) => {
    let respObj = buildData("interactive prompting", participantId, default_question_id, question)
    let existingPrompt = null
    if (participantId in promptHistory.current) {
      existingPrompt = promptHistory.current[participantId].find(p => (default_question_id !== -1 && p?.default_question_id === default_question_id)) ?? null;
    }

    if (existingPrompt !== null) {
      if (participantId in promptResponses.current) {
        promptResponses.current[participantId].push([question, existingPrompt.answer])
      } else {
        promptResponses.current[participantId] = [[question, existingPrompt.answer]]
      }

    } else {
      try {

        const response = await new SessionService().getLLMPromptResponse(respObj);

        if (response.status === 200) {
          const jsonObj = await response.json()
          if (participantId in promptResponses.current) {
            promptResponses.current[participantId].push([question, jsonObj.answer])
          } else {
            promptResponses.current[participantId] = [[question, jsonObj.answer]]
          }
          // console.log(jsonObj.answer)
        } else if (response.status === 400) {
          console.error("LLM api response", response.message)
        }
      } catch (error) {
        console.error(
          "podcomponent interactivePromptFnc",
          error,
        )
      }
    }
  }


  const loadSpeakerMetrics = (speakerId, speakrAlias) => {
    setSelectedSpkrId1(speakerId)
    setSelectedSpkralias(speakrAlias)
    setDetails("Individual");
    writeViewParams("Individual", speakerId);
  }

  // URL -> state: open the view named in the URL (deep link, reload, back/
  // forward). Group/Comparison are plain state; Individual waits for the
  // speakers list; Reflection waits for the synthesized-metrics fetch to fill
  // participants (a ref, hence the short poll rather than a dependency).
  useEffect(() => {
    const view = SLUG_VIEWS[searchParams.get("view")] || "Group";
    if (view === details) return;
    if (view === "Group") {
      setDetails(view);
      return;
    }
    if (view === "Individual") {
      const spId = parseInt(searchParams.get("speaker"), 10);
      const s = speakers.find((x) => x.id === spId);
      if (s) loadSpeakerMetrics(s.id, s.alias);
      else if (speakers.length > 0) setDetails("Individual");
      // speakers not loaded yet: effect re-runs when they arrive
      return;
    }
    // Reflection Dashboard
    if (participants.current.length > 0) {
      loadReflectiondashboard("Reflection Dashboard");
      return;
    }
    const t = setInterval(() => {
      if (participants.current.length > 0) {
        clearInterval(t);
        loadReflectiondashboard("Reflection Dashboard");
      }
    }, 400);
    const stop = setTimeout(() => clearInterval(t), 15000);
    return () => {
      clearInterval(t);
      clearTimeout(stop);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, speakers, details]);

  return (
    <PodComponentPages
      sessionDevice={sessionDevice}
      group={group}
      groupVersion={groupVersion}
      groupName={groupName}
      viewDeviceId={viewDeviceId}
      setViewDeviceId={setViewDeviceId}
      viewDevice={activeSessionService.getSessionDevice(viewDeviceId) || sessionDevice}
      scope={groupScope ? "group" : "device"}
      setScope={setScope}
      navigateToSession={navigateToSession}
      setRange={ResetTimeRange}
      onClickedTimeline={onClickedTimeline}
      session={session}
      displayTranscripts={displayTranscripts}
      displayVideoMetrics={displayVideoMetrics}
      startTime={startTime}
      endTime={endTime}
      transcripts={transcripts}
      loading={loading}
      onSessionClosing={onSessionClosing}
      currentForm={currentForm}
      llmError={llmError}
      clearLlmError={() => setLlmError("")}
      currentTranscript={currentTranscript}
      roster={roster}
      tagCounts={tagCounts}
      speakerInfo={speakerInfo}
      clockOrigin={clockOrigin}
      editDraft={editDraft}
      setEditDraft={setEditDraft}
      podEditing={podEditing}
      setPodEditing={setPodEditing}
      cancelPodEdit={cancelPodEdit}
      saveTranscriptText={saveTranscriptText}
      editTranscriptText={editTranscriptText}
      reassignTranscriptRow={reassignTranscriptRow}
      reassignTranscriptSpeaker={reassignTranscriptSpeaker}
      closeDialog={closeDialog}
      seeAllTranscripts={seeAllTranscripts}
      openDialog={openDialog}
      deleteDeviceToggle={deleteDeviceToggle}
      setDeleteDeviceToggle={setDeleteDeviceToggle}
      removeDeviceFromSession={removeDeviceFromSession}
      speakers={speakers}
      selectedSpkrId1={selectedSpkrId1}
      setSelectedSpkrId1={setSelectedSpkrId1}
      spkr1Transcripts={spkr1Transcripts}
      spkr1VideoMetrics={spkr1VideoMetrics}
      getSpeakerAliasFromID={getSpeakerAliasFromID}
      details={details}
      setDetails={setDetails}
      dashboardView={dashboardView}
      open={open}
      setOpen={setOpen}
      loadSpeakerMetrics={loadSpeakerMetrics}
      selectedSpkralias={selectedSpkralias}
      loadReflectiondashboard={loadReflectiondashboard}
      participants={participants.current}
      selectedParticipantLLMAnalysis={selectedParticipantLLMAnalysis.current}
      selectedParticipantSynthesizedData={selectedParticipantSynthesizedData.current}
      interactivePromptFnc={interactivePromptFnc}
      promptResponses={promptResponses.current}
      isThinking={isThinking}
      setIsThinking={setIsThinking}
      setParticipantIDRefectionDashboard={setParticipantIDRefectionDashboard}
      currentParticipant={currentParticipant}
      llmPendingFor={llmPendingFor}
      selectedMomentIdAndIndex={selectedMomentIdAndIndex}
      setSelectedMomentIdAndIndex={setSelectedMomentIdAndIndex}
    />
  );
}

export { PodComponent };
