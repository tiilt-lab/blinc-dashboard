import { SessionService } from "../services/session-service";
import { DeviceService } from "../services/device-service";
import { startPolling } from "../globals";
import { DeviceModel } from "../models/device";
import { SpeakerModel } from "../models/speaker";
import { useState, useEffect, useMemo, useRef } from "react";
import { useNavigate, useOutletContext } from "react-router-dom";
import { PodsOverviewPages } from "./html-pages";

// Teacher-overview poll cadences. All three loops are chained (never
// overlapping), pause while the tab is hidden, back off on errors and
// abort on unmount (audit G.2). Triage went 8 s -> 10 s: its alerts are
// 30 s-scale signals, so the audit's 5-10 s band is safe there.
const TRIAGE_POLL_MS = 10000;
const QUEUE_POLL_MS = 15000;
const DEVICES_POLL_MS = 15000;

function PodsOverviewComponent(props) {
  const [, setSessionClosing] = useState(false);
  const [sessionDevices, setSessionDevices] = useState(null);
  const [session, setSession] = useState(null);
  const [sessionSpeaker, setSessionSpeakers] = useState([])
  //const [subscriptions, setSubscriptions] = useState([]);
  const [devices, setDevices] = useState([]);
  // Enriched per-pod info (duration, participants, has_data, analysis_running)
  // straight from the devices endpoint — the shared in-memory source strips
  // these fields. Polled so the Analyzing badge updates live.
  const [enriched, setEnriched] = useState({});
  const [selected, setSelected] = useState({});
  const [queueState, setQueueState] = useState({});
  const toggleSelect = (id) =>
    setSelected((cur) => ({ ...cur, [id]: !cur[id] }));
  const toggleSelectAll = (ids) =>
    setSelected((cur) => {
      const allOn = ids.length > 0 && ids.every((id) => cur[id]);
      const next = {};
      for (const id of ids) next[id] = !allOn;
      return next;
    });
  // Resolves false on any failure so the poll loop can back off.
  const loadQueue = (sid, signal) =>
    new SessionService().getPosthocQueue(sid, { signal }).then(
      async (r) => {
        if (r.status !== 200) return false;
        const list = await r.json();
        const m = {};
        for (const j of list) m[j.device_id] = j.state;
        setQueueState(m);
        return true;
      },
      () => false,
    );
  useEffect(() => {
    if (session === null || !session.id) return;
    return startPolling((signal) => loadQueue(session.id, signal), QUEUE_POLL_MS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session]);

  // Live triage: per-pod alert flags (silent / dominated / hanging question)
  // while a class is running. Only polled when the session is still live.
  const [triage, setTriage] = useState({});
  useEffect(() => {
    if (session === null || !session.id || session.ended) return;
    return startPolling(async (signal) => {
      const r = await new SessionService().getSessionTriage(session.id, { signal });
      if (r.status !== 200) return false;
      const list = await r.json();
      const m = {};
      for (const p of list) m[p.device_id] = p.alerts;
      setTriage(m);
      return true;
    }, TRIAGE_POLL_MS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session]);
  const stopRuns = () => {
    if (session === null) return;
    new SessionService().stopPosthocQueue(session.id).then(
      () => loadQueue(session.id),
      () => {},
    );
  };
  const runSelected = () => {
    const ids = Object.keys(selected).filter((k) => selected[k]);
    if (!ids.length || session === null) return;
    new SessionService().enqueuePosthoc(session.id, ids).then(
      () => {
        setSelected({});
        loadQueue(session.id);
      },
      () => {},
    );
  };
  useEffect(() => {
    if (session === null || !session.id) return;
    let alive = true;
    const stop = startPolling(async (signal) => {
      const r = await new SessionService().getSessionDevices(session.id, { signal });
      if (r.status !== 200) return false;
      const list = await r.json();
      if (!alive) return true;
      const map = {};
      for (const d of list) map[d.id] = d;
      setEnriched(map);
      return true;
    }, DEVICES_POLL_MS);
    return () => {
      alive = false;
      stop();
    };
  }, [session]);
  const [currentForm, setCurrentForm] = useState("");
  // Pod deletion: admins/supers may delete any pod; everyone else only pods
  // that never recorded anything (the server enforces the same rule).
  const [podToDelete, setPodToDelete] = useState(null);
  const canDeletePod = (device) => {
    const role = (props.userdata || {}).role;
    if (role === "admin" || role === "super") return true;
    const e = enriched[device.id] || {};
    return e.has_data === false;
  };
  const confirmDeletePod = () => {
    const device = podToDelete;
    setPodToDelete(null);
    if (!device || !session) return;
    const label =
      (device.name && String(device.name).trim()) || `Pod ${device.id}`;
    new SessionService()
      .removeDeviceFromSession(session.id, device.id, true)
      .then(
        (resp) => {
          if (resp.status === 200) {
            setSessionDevices((prev) =>
              (prev || []).filter((d) => d.id !== device.id),
            );
            setToasts((t) => [
              ...t,
              { id: `poddel-${device.id}-${Date.now()}`, text: `${label} deleted` },
            ]);
          } else {
            resp
              .json()
              .then((j) =>
                setToasts((t) => [
                  ...t,
                  {
                    id: `poddelerr-${Date.now()}`,
                    text: (j && j.message) || `Could not delete ${label}.`,
                  },
                ]),
              )
              .catch(() => {});
          }
        },
        () =>
          setToasts((t) => [
            ...t,
            { id: `poddelerr-${Date.now()}`, text: `Could not delete ${label}.` },
          ]),
      );
  };
  // Toasts for analysis completion: diff the 15s enrichment polls and
  // announce pods whose analysis_running flipped off (or hit an error).
  const [toasts, setToasts] = useState([]);
  const prevEnriched = useRef({});
  useEffect(() => {
    const prev = prevEnriched.current;
    const notes = [];
    for (const id of Object.keys(enriched)) {
      const was = prev[id];
      const now = enriched[id];
      if (was && was.analysis_running && now && !now.analysis_running) {
        const name = (now.name && String(now.name).trim()) || `Pod ${id}`;
        notes.push(`${name}: analysis finished`);
      }
    }
    if (notes.length) {
      setToasts((t) => [
        ...t,
        ...notes.map((text, i) => ({ id: `${text}-${t.length + i}`, text })),
      ]);
    }
    prevEnriched.current = enriched;
  }, [enriched]);
  useEffect(() => {
    if (!toasts.length) return;
    const t = setTimeout(() => setToasts((list) => list.slice(1)), 6000);
    return () => clearTimeout(t);
  }, [toasts]);

  const dismissToast = (id) =>
    setToasts((list) => list.filter((x) => x.id !== id));
  const [activeSessionService] = useOutletContext();
  // Join/leave toasts from the fast (2s) socket-backed device feed.
  const prevConnected = useRef(null); // null until the first real feed snapshot
  useEffect(() => {
    if (sessionDevices == null) return; // feed hasn't delivered yet
    const firstSnapshot = prevConnected.current === null;
    const prev = prevConnected.current || {};
    const notes = [];
    const nextMap = {};
    for (const d of sessionDevices) {
      nextMap[d.id] = !!d.connected;
      if (prev[d.id] === undefined) {
        // Toast arrivals after the baseline snapshot; devices already present
        // when the page loaded stay silent.
        if (d.connected && !firstSnapshot) {
          notes.push(`${d.name || "A group"} joined`);
        }
      } else if (!prev[d.id] && d.connected) {
        notes.push(`${d.name || "A group"} joined`);
      } else if (prev[d.id] && !d.connected) {
        notes.push(`${d.name || "A group"} disconnected`);
      }
    }
    if (notes.length) {
      setToasts((t) => [
        ...t,
        ...notes.map((text, i) => ({ id: `${text}-${Date.now()}-${i}`, text })),
      ]);
    }
    prevConnected.current = nextMap;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionDevices]);

  // "Speaking recently" per pod: watch the live transcript stream and stamp
  // each device's latest utterance; cards pulse while < 30s old.
  const [lastActivity, setLastActivity] = useState({});
  useEffect(() => {
    if (activeSessionService === null) return;
    const sub = activeSessionService.transcriptSource.subscribe((list) => {
      if (!list || !list.length) return;
      const latest = list[list.length - 1];
      if (latest && latest.session_device_id) {
        setLastActivity((m) => ({
          ...m,
          [latest.session_device_id]: Date.now(),
        }));
      }
    });
    return () => sub.unsubscribe && sub.unsubscribe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionService]);
  // tick so the pulse expires without new data
  const [, setActivityTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setActivityTick((x) => x + 1), 5000);
    return () => clearInterval(t);
  }, []);

  // Banner End flow (confirm dialog + end + refresh).
  const endThisSession = () => {
    if (session === null) return;
    new SessionService().endSession(session.id).then(
      () => window.location.reload(),
      () => window.location.reload(),
    );
  };

  const navigate = useNavigate();
  const [trigger, setTrigger] = useState(0);
  const interval = 2000;

  useEffect(() => {
    if (activeSessionService !== null) {
      const nextSession = activeSessionService.getSession();
      setSession(nextSession);
      const nextSessionDevices = activeSessionService.getSessionDevices();
      setSessionDevices(nextSessionDevices);

      getSessionSpeakers(nextSession.id)

      let intervalLoad = setInterval(() => {
        const nextSession = activeSessionService.getSession();
        setSession(nextSession);
        const nextSessionDevices = [
          ...activeSessionService.getSessionDevices(),
        ];
        setSessionDevices(nextSessionDevices);
      }, interval);

      // subscriptions.push(sessionSub, deviceSub);

      // return () => {
      //     subscriptions.map(sub => sub.unsubscribe());
      // }
      return () => {
        clearInterval(intervalLoad);
      };
    }
  }, [activeSessionService]);

  // so that the passcode renders in time
  useEffect(() => {
  }, [trigger]);

  const goToDevice = (sessionDevice) => {
    navigate("/sessions/" + session.id + "/pods/" + sessionDevice.id);
  };

  // Inline group rename, same UX as the Manage Sessions inline rename.
  // Mutate the store's device object too: the 2s poll copies the same model
  // instances, so a purely-local state update would be overwritten until the
  // device_update socket event lands.
  const renamePodInline = (device, newName) => {
    const trimmed = (newName || "").trim();
    if (!trimmed || trimmed === device.name) return;
    new SessionService().renamePod(session.id, device.id, trimmed).then(
      (response) => {
        if (response.status === 200) {
          device.name = trimmed;
          setSessionDevices((prev) => [...(prev || [])]);
        }
      },
      (apierror) => {
        console.error("pods-overview func: renamePodInline ", apierror);
      },
    );
  };

  // A participant's metrics live in their pod's Individual view (URL-
  // addressable), replacing the old pods_session dead-end clone page.
  const goToSpeakerMetrics = (speaker) => {
    navigate(
      "/sessions/" + session.id + "/pods/" + speaker.session_device_id +
        "?view=individual&speaker=" + speaker.id,
    );
  };

  const onSessionClosing = (isClosing) => {
    setSessionClosing(isClosing);
  };

  const navigateToSessions = () => {
    if (session.folder) {
      navigate("/sessions?folder=" + session.folder);
    } else {
      navigate("/sessions", { replace: true });
    }
  };

  const openDialog = (form) => {
    if (form === "AddDevice") {
      const fetchData = new DeviceService().getDevices(
        false,
        true,
        false,
        true
      );
      fetchData.then(
        (response) => {
          if (response.status === 200) {
            const resp2 = response.json();
            resp2.then((respdevices) => {
              const deviceresult = DeviceModel.fromJsonList(respdevices);
              setDevices(deviceresult);
              setCurrentForm(form);
            });
          }
        },
        (apierror) => {
          console.error("pods-overview-components func: openDialog 1 ", apierror);
        }
      );
    } else if (form === "Passcode" && session.end_date == null) {
      setCurrentForm(form);
    } else if (form === "ConfirmEnd") {
      setCurrentForm(form);
    }
  };

  // Status renders as a header pill: "Closed" is static, "Locked" opens the
  // share dialog (so a locked session can be unlocked); a live passcode is a
  // clickable header action (opens the same dialog).
  const getRightPill = () => {
    if (session !== null && session.end_date) return "Closed";
    if (session !== null && session.passcode == null) return "Locked";
    return null;
  };

  const getPasscode = () => {
    if (session !== null && !session.end_date && session.passcode != null) {
      return session.passcode;
    }
    return "";
  };

  const setPasscodeState = (state) => {
    const fetchData = new SessionService().setPasscodeStatus(session.id, state);
    fetchData.then(
      (response) => {
        if (response.status === 200) {
          closeDialog();
        }
        setSession(activeSessionService.getSession());
        setTrigger(trigger + 1);
      },
      (apierror) => {
        console.error(
          "pods-overview-components func: setPasscodeState 1 ",
          apierror
        );
      }
    );
  };

  // to make it easier on the user
  const copyPasscode = () => {
    let pwd = getPasscode();
    navigator.clipboard.writeText(pwd);
  };

  // Shareable yellkey-style join link for the current passcode.
  const getJoinLink = () =>
    getPasscode() ? window.location.origin + "/join/" + getPasscode() : "";

  const copyJoinLink = () => {
    navigator.clipboard.writeText(getJoinLink());
  };

  const exportSessionMetricsData = (type,windowsize,format) => {
    let fetchData = null;
    if (type === "audiometrics") {
      fetchData = new SessionService().downloadSessionTranscriptMetrics(session.id, session.title,windowsize,format);
    } else if (type === "videometrics") {
      fetchData = new SessionService().downloadSessionVideoMetrics(session.id, session.title,windowsize,format);
    } else if (type === "transcriptvideometrics") {
      fetchData = new SessionService().downloadSessionTranscriptVideoMetrics(session.id, session.title,windowsize,format);
    }
    if (fetchData != null) {
      fetchData.then(
        (response) => {
          if (response.status === 200) {
            response.text().then((Data) => {
              const anchor = document.createElement("a");
              anchor.href =
                "data:attachment/csv;charset=utf-8," + encodeURI(Data);
              let extension = format === "csv" ? ".csv" : ".json"  
              anchor.download = session.title + extension;
              anchor.click();
             
              //return true;
              /* {const resp = response.json()
                    resp.then() }*/
            });
          } else {
            alert("Failed to download transcript metrics.");
          }
        },
        (apierror) => {
          console.error(
            "pods-overview-components func: exportSessionTranscriptMetrics 1 ",
            apierror
          );
          alert("Failed to download transcript metrics.");
        }
      );
    }
  };

 

  const downloadData = (windowsize, datatype,format) => {
    exportSessionMetricsData(datatype,windowsize,format);
  }

  const closeDialog = () => {
    setCurrentForm("");
  };

  const openDownloadOptionDialog = (dialog) => {
    setCurrentForm(dialog);
  };


  const getSessionSpeakers = (sessionID) => {
    const fetchData = new SessionService().getSessionSpeakers(sessionID);
    fetchData.then(
      (response) => {
        if (response.status === 200)
          response.json().then((jsonObj) => {
            const input = SpeakerModel.fromJsonList(jsonObj)
            const identified_speakers = []
            const uniqueSpeakers = []
            if (input && input.length) {
              input.map((spkr, index) => {
                if (identified_speakers.indexOf(spkr.alias) === -1) {
                  uniqueSpeakers.push(spkr)
                  identified_speakers.push(spkr.alias)
                }
              })
              setSessionSpeakers(uniqueSpeakers);
            }
          });
      },
      (apierror) => {
        console.error("pod-overview-component func getspeakers 1", apierror);
      }
    );
  };


  // Alphabetical pod order (numeric-aware, so "Group 2" < "Group 10"). The
  // store's list is join-order and the 2s poll would reshuffle any sort done
  // downstream, so sort the copy handed to the page each render.
  const sortedDevices = useMemo(() => {
    if (sessionDevices === null) return null;
    return [...sessionDevices].sort((a, b) =>
      String(a.name || "").localeCompare(String(b.name || ""), undefined, {
        numeric: true,
        sensitivity: "base",
      }),
    );
  }, [sessionDevices]);

  return (
    <PodsOverviewPages
      enriched={enriched}
      triage={triage}
      selected={selected}
      toggleSelect={toggleSelect}
      toggleSelectAll={toggleSelectAll}
      runSelected={runSelected}
      stopRuns={stopRuns}
      queueState={queueState}
      toasts={toasts}
      dismissToast={dismissToast}
      lastActivity={lastActivity}
      endThisSession={endThisSession}
      righttext={getPasscode()}
      rightpill={getRightPill()}
      session={session}
      openDialog={openDialog}
      navigateToSessions={navigateToSessions}
      sessionDevices={sortedDevices}
      renamePodInline={renamePodInline}
      canDeletePod={canDeletePod}
      podToDelete={podToDelete}
      requestDeletePod={setPodToDelete}
      cancelDeletePod={() => setPodToDelete(null)}
      confirmDeletePod={confirmDeletePod}
      goToDevice={goToDevice}
      currentForm={currentForm}
      closeDialog={closeDialog}
      setPasscodeState={setPasscodeState}
      copyPasscode={copyPasscode}
      passcode={getPasscode()}
      joinLink={getJoinLink()}
      copyJoinLink={copyJoinLink}
      onSessionClosing={onSessionClosing}
      initialized={activeSessionService.initialized}
      sessionSpeaker={sessionSpeaker}
      devices={devices}
      goToSpeakerMetrics={goToSpeakerMetrics}
      openDownloadOptionDialog={openDownloadOptionDialog}
      downloadData={downloadData}
    />
  );
}

export { PodsOverviewComponent };
