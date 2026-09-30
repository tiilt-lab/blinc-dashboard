
import { useEffect, useRef, useState, useMemo } from "react";
import { useNavigate, useLocation } from "react-router-dom"
import { StudentSessionDashboardPages } from "./html-pages";
import { AuthService } from "../services/auth-service"
import { SessionService } from "../services/session-service";
import { StudentModel } from "../models/student"
import { SessionModel } from "../models/session";
import { SessionDeviceModel } from "../models/session-device";
import { FEATURE_LABELS, BOX_LABELS, buildChecklist } from "../utilities/checklist"
import { startPolling } from "../globals"

// Live-session poll cadence. This is a student's read-only view of their
// own lines (full history per response — the alias routes have no
// after_id), so 5 s is plenty and halves the load of the old 2 s.
const LIVE_POLL_MS = 5000

const surveyquestion = [
  ["Communication rate", "How would you rate the level of communication?"],
  ["Climate rate", "How would  you rate the climate (group dynamics) during the collaboration session?"],
  ["Conflict frequency", "How frequently did disagreements/conflict occur during the collaboration?"],
  ["Participation balance", "How balanced was participation among group members (verbal contribution, turn taking) of group members?"],
  ["Reflection usefulness", "How useful was the reflection feedback in helping you understand your collaboration?", "(If you did not receive feedback, please answer based on how useful you think it would have been.)"],
  ["Collaboration goal", "What aspect of your collaboration would you want to improve on in future collaboration session (Objective) ?"],
  ["Collaboration quality", "How would you rate the overall quality of your team's collaboration this session?"],
  ["Assessment accuracy", "How would you rate the accuracy of the assessment scores?", "(If you did not receive assessment, please select 'Did not receive')"]
]
const likertOptions = [["Very low", "Low", "Normal", "High", "Very high"],
["Very poor", "Poor", "Neutral", "Good", "Excellent"],
["Never", "Rarely", "Sometimes", "Often", "Very Often"],
["Very unbalanced", "Somewhat unbalanced", 'Moderately balanced', "Mostly balanced", "Very balanced"],
["Not useful", "Slightly useful", "Moderately useful", "Very useful", "Extremely useful"],
["Communication", "Participation", "Focused attention", "Idea contribution", "Momemtum"],
["Very low", "Low", "Normal", "High", "Very high"],
["Did not receive", "low", "Just right", "High", "Too high"]]
// const likertOptions = [1, 2, 3, 4, 5];

function StudentSessionDashboard() {
  const previousSessions = useRef([])
  // False until the student explicitly opens a session from the table —
  // keeps the landing on the sessions list.
  const sessionChosen = useRef(false)
  // Session id whose single group was already auto-opened, so the effect
  // doesn't re-trigger the reflection load on every re-render.
  const autoOpenedFor = useRef(null)
  const sessionsObjects = useRef({});
  const pageTitle = useRef("Dashboard")
  const session = useRef(null)


  const [sessionDevices, setSessionDevices] = useState([])
  const [selectedDeviceID, setSelectedDeviceID] = useState(-1)
  const [sessionId, setSessionId] = useState(-1)

  const [selectFilteredDevice1, setSelectFilteredDevice1] = useState([])
  const [selectFilteredDevice2, setSelectFilteredDevice2] = useState([])
  const [reload, setReload] = useState(0)

  const [nextPage, setNextPage] = useState("reportoptionpage")

  const [currentForm, setCurrentForm] = useState("")
  const [showFeatures, setShowFeatures] = useState([])
  const [showBoxes, setShowBoxes] = useState([])
  // The student view always lists every session the student appeared in
  // (getSessionsByAlias), so there is no current-vs-previous choice to make —
  // the flow is hardwired to the all-sessions path.
  const [sessiontype, setSessiontype] = useState("previoussessions")


  const [wrongInput, setWrongInput] = useState(false)
  const [pcode, setPcode] = useState("")
  const [userDetail, setUserDetail] = useState(null)

  const [transcripts, setTranscripts] = useState([])
  const [videoMetrics, setVideoMetrics] = useState([])
  const [startTime, setStartTime] = useState()
  const [endTime, setEndTime] = useState()
  const [startTime2, setStartTime2] = useState()
  const [endTime2, setEndTime2] = useState()
  const [displayTranscripts] = useState([])
  const [displayVideoMetrics] = useState([])
  const [currentTranscript, setCurrentTranscript] = useState(null)
  const [details, setDetails] = useState("Individual")


  const [timeRange, setTimeRange] = useState([0, 1])
  const [showAlert, setShowAlert] = useState(false)
  const [alertMessage, setAlertMessage] = useState("")


  const [selectedSessionId1, setSelectedSessionId1] = useState(-1);
  const [selectedSessionId2, setSelectedSessionId2] = useState(-1);
  const [selectedSessionDeviceId1, setSelectedSessionDeviceId1] = useState(-1);
  const [selectedSessionDeviceId2, setSelectedSessionDeviceId2] = useState(-1);
  const [session1Transcripts, setSession1Transcripts] = useState([]);
  const [session2Transcripts, setSession2Transcripts] = useState([]);
  const [session1VideoMetrics, setSession1VideoMetrics] = useState([])
  const [session2VideoMetrics, setSession2VideoMetrics] = useState([])

  const [displaySingleSession, setDisplaySingleSession] = useState(false);
  const [displayCompareSession, setDisplayCompareSession] = useState(false);

  const [firstLoadCompleted, setFirstLoadCompleted] = useState(false);
  const [transcriptDoneLoading, setTranscriptDoneLoading] = useState(false);
  const [videoMetricDoneLoading, setVideoMetricDoneLoading] = useState(false);
  const [currentSessionRunning, setCurrentSessionRunning] = useState(false);

  const [, setSessionPart] = useState("");
  const [loadedAfresh, setLoadedAfresh] = useState(true)

  const [reflectionDashboardDoneLoading, setReflectionDashboardDoneLoading] = useState(false);

  const sessionDataObjects = useRef({});

  const synthesizedFeedbackMetrics = useRef({});
  const llmSessionAnalysis = useRef({})
  const selectedSynthesizedData = useRef({})
  const selectedLLMAnalysis = useRef({})
  const promptHistory = useRef({})
  const promptResponses = useRef({})
  const [isThinking, setIsThinking] = useState(false)

  const [selectedMomentIdAndIndex, setSelectedMomentIdAndIndex] = useState(null);
  const [deviceIDReflectionDashboard, setdeviceIDRefectionDashboard] = useState(-1)
  const [sessionNameForReflecDashboard, setSessionNameForReflecDashboard] = useState("")
  const [groupNameForReflecDashboard, setGroupNameForReflecDashboard] = useState("")
  const [ratings, setRatings] = useState({});
  const [submitted, setSubmitted] = useState(false);
  const [notes, setNotes] = useState("");
  const [dialogHeading, setDialogHeading] = useState("Error");


  const navigate = useNavigate()

  //I want to use the same JSX to handle participants who joined the collaboration
  // but will not be exposed to either real-time or post-hoc feedback. However, the component is 
  // designed to display feedback, so in other to re-use it, i define a separate url in the route
  // but points it to this jSX, so this url has 'survey' at the end while the other url to the page
  //does 'dashboard'. So i use the useLocation hooks to get the path and detect the last path element
  // such that if is it 'survey', it will render only the survey page, else it will render the dashboard
  // including the survey page.
  const location = useLocation();
  const pathToSurveyOptions = location.pathname.split("/").at(-1)

  const sessionService = new SessionService()
  const authService = new AuthService()

  // USEEFFECTS AND INIT FUNCTIONS

  useEffect(() => {
    setShowFeatures(buildChecklist(FEATURE_LABELS))
    setShowBoxes(buildChecklist(BOX_LABELS))
  }, []);


  // ___________________________________________________________ 
  //  FIRST USEEFFECT:  CALL ONCE STEP 1,2, 2.1, 2.1.1, OR 2.2 HAS COMPLETED            
  // --------------------------------------------------------
  useEffect(() => {
    let stopPoll = null

    if (session.current !== null && userDetail !== null && sessiontype !== "") {

      if (sessiontype === "previoussessions") {
        //Load session groups the student joined
        setSelectFilteredDevice1(sessionDevices)
        if (!sessionChosen.current) {
          // Landing: the sessions table.
          setSelectedSessionDeviceId1(-1)
          setNextPage("sessionlistpage")
        } else if (
          sessionDevices.length === 1 &&
          autoOpenedFor.current !== session.current.id
        ) {
          // A student is almost always in a single group per session, so
          // skip the pod-picker. On the survey-only route (no-feedback
          // control condition) open the group's survey; otherwise open its
          // reflection dashboard. The groups table remains for the rare
          // multi-pod case below.
          autoOpenedFor.current = session.current.id
          if (pathToSurveyOptions === "survey") {
            loadSelectedSessionDeviceMetrics(sessionDevices[0].id)
          } else {
            loadReflectiondashboard("Reflection Dashboard")
          }
        } else if (sessionDevices.length !== 1) {
          setSelectedSessionDeviceId1(-1)
          setNextPage("displaygrouppage")
        }
      } else if (sessiontype === "currentsession" && !firstLoadCompleted) { //LOOK INTO THIS LOGIC, AND ENSURE SMOOTH OPERATION WITH LOADEDAFRESH
        setCurrentSessionRunning(true);
        // Live session: chained poll (never overlapping, paused while the
        // tab is hidden, backed off on errors, aborted on cleanup). The
        // first run fires immediately.
        stopPoll = startPolling(async (signal) => {
          const [a, b] = await Promise.all([
            fetchTranscript(session.current.id, setTranscripts, null, signal),
            fetchVideoMetric(session.current.id, setVideoMetrics, null, signal),
          ])
          return a !== false && b !== false
        }, LIVE_POLL_MS)
      }

    }

    return () => {
      if (stopPoll) stopPoll()
    }
  }, [session.current, userDetail, sessionDevices, sessiontype])

  // ___________________________________________________________ 
  //  SECOND USEEFFECT:  CALL TO LOAD TRANSCRIPT FOR SELECTED DEVICE            
  // --------------------------------------------------------
  useEffect(() => {
    // fetch the transcript based on group selected
    if (selectedDeviceID > -1) {
      //this is necessary if the first session and device were loaded from the entry page
      // and the user has not clicked on any session
      if (!firstLoadCompleted) {
        setSelectedSessionId1(sessionId)
      }
      setDetails("Individual");
      setDisplaySingleSession(true);
    }

  }, [selectedDeviceID, firstLoadCompleted, sessionId])


  // ___________________________________________________________ 
  //  THIRD USEEFFECT:  THIS IS TRIGGERED ONCE THE TRANSCRIPT AND VIDEO METRIC IS PULLED FROM THE DB            
  // --------------------------------------------------------
  useEffect(() => {
    // Before the first load completes, and again on every update while the
    // session is still running (the two branches used to be identical bodies).
    if (session.current !== null && (!firstLoadCompleted || currentSessionRunning)) {
      const sessionLen =
        Object.keys(session.current).length > 0 ? session.current.length : 0
      const sTime = Math.round(sessionLen * timeRange[0] * 100) / 100
      const eTime = Math.round(sessionLen * timeRange[1] * 100) / 100
      setStartTime(sTime)
      setEndTime(eTime)
      generateDisplayTranscripts(transcripts, sTime, eTime)
      generateDisplayVideoMetrics(videoMetrics, sTime, eTime)
    }

  }, [transcripts, videoMetrics, timeRange, currentSessionRunning, firstLoadCompleted])


  useEffect(() => {
    if (transcriptDoneLoading && videoMetricDoneLoading && !firstLoadCompleted) {
      setSelectedSessionId1(session.current.id);

      // ///we maintain a store that keeps already fetched transcript base session and device id
      // updateTranscriptVideoMetricStore(loadedAfresh, sessiontype,selectedSessionId1, selectedSessionDeviceId1,session1Transcripts,session1VideoMetrics)

      setNextPage((p) => (p === "Reflection Dashboard" ? p : "displayreportpage"))
      setFirstLoadCompleted(true);
    }

  }, [transcriptDoneLoading, videoMetricDoneLoading, loadedAfresh, firstLoadCompleted]);


  useEffect(() => {
    if (transcriptDoneLoading && videoMetricDoneLoading && firstLoadCompleted) {

      ///we maintain a store that keeps already fetched transcript base session and device id
      updateTranscriptVideoMetricStore(loadedAfresh, sessiontype, selectedSessionId1, selectedSessionDeviceId1, session1Transcripts, session1VideoMetrics)

      // if (loadedAfresh && (session2Transcripts.length > 0 || session2VideoMetrics.length > 0) && (selectedSessionId2 > -1)) {
      if (selectedSessionId2 > -1) {
        updateTranscriptVideoMetricStore(loadedAfresh, sessiontype, selectedSessionId2, selectedSessionDeviceId2, session2Transcripts, session2VideoMetrics)


        const sessionData = sessionsObjects.current[selectedSessionId2];
        const sessionLen =
          Object.keys(sessionData).length > 0 ? sessionData.length : 0
        setStartTime2(Math.round(sessionLen * timeRange[0] * 100) / 100)
        setEndTime2(Math.round(sessionLen * timeRange[1] * 100) / 100)
      }

      setNextPage((p) => (p === "Reflection Dashboard" ? p : "displayreportpage"))
    }

  }, [transcriptDoneLoading, videoMetricDoneLoading, loadedAfresh, firstLoadCompleted]);


  useEffect(() => {

    if (displayCompareSession || displaySingleSession) {
      setSessionTranscripts()
      setSessionVideoMetrics()

    }
    if (details === "Individual" && displaySingleSession) {
      setDisplaySingleSession(false);
    } else if (details === "Comparison" && displayCompareSession) {
      setDisplayCompareSession(false);
    }

  }, [details, displaySingleSession, displayCompareSession]);

  // THIS IS MEANT FOR REFLECTION DASHBOARD
  useEffect(() => {
    if (deviceIDReflectionDashboard !== -1) {
      setSelectedSessionDeviceId1(deviceIDReflectionDashboard)
      setRatings(Object.fromEntries(surveyquestion.map((item) => [item[0], ""])))
    }
  }, [deviceIDReflectionDashboard])


  const completedCount = useMemo(
    () => Object.values(ratings).filter((value) => value !== "").length,
    [ratings]
  );

  const updateTranscriptVideoMetricStore = (loaded, sessiontype, selectedSessionId, selectedSessionDeviceId, Transcripts, VideoMetrics) => {
    //if loaded afresh then update the store
    if (loaded && sessiontype === "previoussessions") {
      if (!sessionDataObjects.current.hasOwnProperty(selectedSessionId)) {
        sessionDataObjects.current[selectedSessionId] = {
          [selectedSessionDeviceId]: {
            transcripts: Transcripts,
            videoMetrics: VideoMetrics
          }
        }
      } else {
        if (!sessionDataObjects.current[selectedSessionId].hasOwnProperty(selectedSessionDeviceId)) {
          sessionDataObjects.current[selectedSessionId][selectedSessionDeviceId] = {
            transcripts: Transcripts,
            videoMetrics: VideoMetrics
          }
        }
      }
    }
  }


  // ___________________________________________________________ 
  //  STEP1: Clicking the  continue buttion   call this function to verify the selections and input              
  // --------------------------------------------------------

  const loadDashboard = (username) => {
    if (username === '') {
      setAlertMessage("Please enter your username.");
      setShowAlert(true);
      document.getElementById("username").focus();
    } else {
      verifyUsername(username)
    }
  }

  // ___________________________________________________________ 
  //   STEP2: This is called to authenticate the username, fetch user data and call load session           
  // --------------------------------------------------------

  const verifyUsername = async (username) => {
    const fetchData = authService.getStudentProfileByID(username)
    fetchData
      .then(
        (response) => {
          if (response.status === 200) {
            response.json().then((jsonObj) => {
              const student_data = StudentModel.fromJson(jsonObj)
              setUserDetail(student_data)
              loadSession(username)
            })
          } else {
            setAlertMessage("Invalid Username");
            setShowAlert(true);
          }
        },
        (apierror) => {
          console.error(
            "Student dashboard func: verifyUsername 1 ",
            apierror,
          )
        },
      )
  }

  // ___________________________________________________________ 
  //   STEP 2.1 : This is called inside step 2 to load the sessions the student have joined so far and then initialize the first session and first device/group          
  // --------------------------------------------------------

  const loadSession = (username) => {
    if (sessiontype === "previoussessions") {
      const fetchData = sessionService.getSessionsByAlias(username)
      fetchData
        .then(
          (response) => {
            if (response.status === 200) {
              response.json().then((jsonArrayObj) => {
                const session_data = SessionModel.fromJsonList(jsonArrayObj)

                if (session_data.length > 0) {
                  previousSessions.current = session_data
                  session.current = session_data[0]
                  pageTitle.current = session_data[0].name + ": " + new Date(session_data[0].creation_date).toDateString()
                  setSessionId(session_data[0].id);
                  session_data.forEach((s) => {
                    sessionsObjects.current[s.id] = s
                  })
                  loadSessionDevice(session_data[0].id, username)
                  setReload(reload + 1)
                }
              })
            } else {
              setAlertMessage("No Previous Sessions Found for this User");
              setShowAlert(true);
            }
          },
          (apierror) => {
            console.error(
              "Student dashboard func: loadSession 1 ",
              apierror,
            )
          },
        )
    }
  }

  // ___________________________________________________________ 
  //   STEP 2.1.1 : This is called inside step 2.1 to load the sessions device/group joined by the student         
  // --------------------------------------------------------

  const loadSessionDevice = (sessionid, username) => {
    if (sessiontype === "previoussessions") {
      getSessionDevicesBySessionAndAlias(sessionid, username, setSessionDevices)
    }
  }


  const getSessionDevicesBySessionAndAlias = (sessionid, username, setFnc) => {
    const fetchData = sessionService.getSessionsDeviceByAlias(sessionid, username)
    fetchData
      .then(
        (response) => {
          if (response.status === 200) {
            response.json().then((jsonArrayObj) => {
              const session_device_data = SessionDeviceModel.fromJsonList(jsonArrayObj)

              if (session_device_data.length > 0) {
                setFnc(session_device_data)
              }
            })
          } else {
            setFnc([])
            setAlertMessage("No  Sessions Group Found for this User");
            setShowAlert(true);
          }
        },
        (apierror) => {
          console.error(
            "Student dashboard func: loadSession 1 ",
            apierror,
          )
        },
      )
  }


  // Both fetchers resolve false on any failure so the live poll can back off.
  const fetchTranscript = async (sessionId, setMetric, deviceId = null, signal = undefined) => {
    try {
      let response = null
      if (deviceId === null) {
        response = await sessionService.getSessionTranscriptsForClient(sessionId, userDetail.username, 0, { signal })
      } else {
        response = await sessionService.getSessionDeviceTranscriptsByAlias(sessionId, deviceId, userDetail.username)
      }


      if (response !== null && response.status === 200) {
        const jsonObj = await response.json()
        const fetched_trancript_metrics = jsonObj.map((trancript_metrics) => {
          return { ...trancript_metrics['transcript'], speaker_metrics: trancript_metrics['speaker_metrics'] }
        })

        const sessionLen =
          Object.keys(session.current).length > 0 ? session.current.length : 0
        setStartTime(Math.round(sessionLen * timeRange[0] * 100) / 100)
        setEndTime(Math.round(sessionLen * timeRange[1] * 100) / 100)

        setMetric(fetched_trancript_metrics)
        setLoadedAfresh(true)
        setTranscriptDoneLoading(true);
        return true
      }
      console.error(response, "no transcript obj")
      return false
    } catch (error) {
      console.error(
        "student-dashboard error func : fetch transcript",
        error,
      )
      return false
    }
  }

  const fetchVideoMetric = async (sessionId, setMetric, deviceId = null, signal = undefined) => {
    try {
      let response = null
      if (deviceId === null) {
        response = await sessionService.getSessionVideoMetricsForClient(sessionId, userDetail.username, 0, { signal })
      } else {
        response = await sessionService.getSessionDeviceVideoMetricsByAlias(sessionId, deviceId, userDetail.username)
      }


      if (response !== null && response.status === 200) {
        const fetched_video_metrics = await response.json()

        setMetric(fetched_video_metrics)
        setLoadedAfresh(true)
        setVideoMetricDoneLoading(true);
        return true
      }
      console.error(response, "no videometrics obj")
      return false
    } catch (error) {
      console.error(
        "student-dashboard error func : fetch video metrics",
        error,
      )
      return false
    }
  }


  const loadSelectedSessionMetrics = (newSession) => {
    sessionChosen.current = true
    setTranscriptDoneLoading(false);
    setVideoMetricDoneLoading(false);
    setSession1Transcripts([]);
    setSession1VideoMetrics([]);
    setSession2Transcripts([]);
    setSession2VideoMetrics([]);
    setSelectedSessionId1(newSession.id)
    setSessionId(newSession.id);
    setSelectedSessionId2(-1)
    setSelectedDeviceID(-1)
    pageTitle.current = newSession.name + ": " + new Date(newSession.creation_date).toDateString()
    session.current = newSession
    loadSessionDevice(newSession.id, userDetail.username)
  }

  const loadSelectedSessionDeviceMetrics = (deviceId) => {
    setSelectedSessionDeviceId1(deviceId)
    setSelectedSessionDeviceId2(-1)
    setSelectedDeviceID(deviceId)
  }

  const getSessionDevices = (selectedSessionId, sessionPart = "sessionOne") => {
    if (sessionPart === "sessionOne") {
      getSessionDevicesBySessionAndAlias(selectedSessionId, userDetail.username, setSelectFilteredDevice1)
      setSelectedSessionId1(selectedSessionId)
    } else if (sessionPart === "sessionTwo") {
      getSessionDevicesBySessionAndAlias(selectedSessionId, userDetail.username, setSelectFilteredDevice2)
      setSelectedSessionId2(selectedSessionId)
    }
    setSessionId(selectedSessionId);
  }

  const loadComparedSessionDeviceMetrics = (selectedSessionDeviceId, sessionPart) => {
    setTranscriptDoneLoading(false);
    setVideoMetricDoneLoading(false);
    setSessionPart(sessionPart);
    if (sessionPart === "sessionOne") {
      setSession1Transcripts([]);
      setSession1VideoMetrics([]);
      setSelectedSessionDeviceId1(selectedSessionDeviceId)
    } else if (sessionPart === "sessionTwo") {
      setSession2Transcripts([]);
      setSession2VideoMetrics([]);
      setSelectedSessionDeviceId2(selectedSessionDeviceId)
    }
    pageTitle.current = "Comparing Sessions"
    setDisplayCompareSession(true);
  }


  const generateDisplayTranscripts = (transcripts, s, e) => {
    setSession1Transcripts(
      transcripts.filter((t) => t.start_time >= s && t.start_time <= e),
    )
  }

  const generateDisplayVideoMetrics = (videoMetrics, s, e) => {
    setSession1VideoMetrics(
      videoMetrics.filter((v) => v.time_stamp >= s && v.time_stamp <= e),
    )
  }

  const setSessionTranscripts = () => {
    if (selectedSessionId1 !== -1) {
      if (sessionDataObjects.current.hasOwnProperty(selectedSessionId1)) {
        if (sessionDataObjects.current[selectedSessionId1].hasOwnProperty(selectedSessionDeviceId1)) {
          loadSavedSessionTranscriptMetrics(selectedSessionId1, sessionDataObjects.current[selectedSessionId1][selectedSessionDeviceId1].transcripts, setSession1Transcripts)
        } else {
          fetchTranscript(selectedSessionId1, setSession1Transcripts, selectedSessionDeviceId1)
        }
      } else {
        fetchTranscript(selectedSessionId1, setSession1Transcripts, selectedSessionDeviceId1)
      }
    }

    if (selectedSessionId2 !== -1) {
      if (sessionDataObjects.current.hasOwnProperty(selectedSessionId2)) {
        if (sessionDataObjects.current[selectedSessionId2].hasOwnProperty(selectedSessionDeviceId2)) {
          loadSavedSessionTranscriptMetrics(selectedSessionId2, sessionDataObjects.current[selectedSessionId2][selectedSessionDeviceId2].transcripts, setSession1Transcripts)
        } else {
          fetchTranscript(selectedSessionId2, setSession2Transcripts, selectedSessionDeviceId2)
        }
      } else {
        fetchTranscript(selectedSessionId2, setSession2Transcripts, selectedSessionDeviceId2)
      }
    }
  }

  const setSessionVideoMetrics = () => {
    if (selectedSessionId1 !== -1) {
      if (sessionDataObjects.current.hasOwnProperty(selectedSessionId1)) {
        if (sessionDataObjects.current[selectedSessionId1].hasOwnProperty(selectedSessionDeviceId1)) {
          loadSavedSessionVideoMetrics(sessionDataObjects.current[selectedSessionId1][selectedSessionDeviceId1].videoMetrics, setSession1VideoMetrics)
        } else {
          fetchVideoMetric(selectedSessionId1, setSession1VideoMetrics, selectedSessionDeviceId1)
        }
      } else {
        fetchVideoMetric(selectedSessionId1, setSession1VideoMetrics, selectedSessionDeviceId1)
      }

    }

    if (selectedSessionId2 !== -1) {
      if (sessionDataObjects.current.hasOwnProperty(selectedSessionId2)) {
        if (sessionDataObjects.current[selectedSessionId2].hasOwnProperty(selectedSessionDeviceId2)) {
          loadSavedSessionVideoMetrics(sessionDataObjects.current[selectedSessionId2][selectedSessionDeviceId2].videoMetrics, setSession2VideoMetrics)
        } else {
          fetchVideoMetric(selectedSessionId2, setSession2VideoMetrics, selectedSessionDeviceId2)
        }
      } else {
        fetchVideoMetric(selectedSessionId2, setSession2VideoMetrics, selectedSessionDeviceId2)
      }
    }
  }


  const loadSavedSessionTranscriptMetrics = (sessionId, transcript, setSessionTranscript) => {
    const sessionData = sessionsObjects.current[sessionId];
    const sessionLen =
      Object.keys(sessionData).length > 0 ? sessionData.length : 0
    setStartTime(Math.round(sessionLen * timeRange[0] * 100) / 100)
    setEndTime(Math.round(sessionLen * timeRange[1] * 100) / 100)

    setSessionTranscript(transcript)
    setLoadedAfresh(false)
    setTranscriptDoneLoading(true);
  }

  const loadSavedSessionVideoMetrics = (videoMetric, setSessionVideoMetric) => {
    setSessionVideoMetric(videoMetric)
    setLoadedAfresh(false)
    setVideoMetricDoneLoading(true);
  }

  ///------------------------------------------------------------------------//
  // IMPLEMENTATION FOR THE REFLECTION DASHBOARD 
  //-----------------------------------------------------------------------//

  const loadReflectiondashboard = async (view) => {
    // Auto-open the group when there is only one; alert with directions
    // otherwise (previously this demanded a selection the UI never forced).
    const deviceId = selectedSessionId1 === -1 ? -1 : ensureDeviceSelected()
    if (selectedSessionId1 === -1 || deviceId === -1) {
      if (selectedSessionId1 === -1) {
        setAlertMessage("Open a session from the list first, then pick your group.");
        setShowAlert(true);
      }
    } else {
      const priorPage = nextPage
      setNextPage(view);
      setReflectionDashboardDoneLoading(false)
      // Every failure path must leave the loading state — a thrown error or
      // false status used to strand the page on "Processing…" forever.
      let actionstatus = false
      try {
        actionstatus = await extractParticipantData(deviceId)
      } catch (error) {
        console.error("loadReflectiondashboard", error)
      }
      if (actionstatus) {
        const sess = previousSessions.current.find((ses) => ses.id === selectedSessionId1);
        const group = sessionDevices.find((dev) => dev.id === deviceId);
        setSessionNameForReflecDashboard(sess?.name + ": " + new Date(sess?.creation_date).toDateString())
        setGroupNameForReflecDashboard(group?.name)
        setReflectionDashboardDoneLoading(true)
      } else {
        setNextPage(priorPage)
        setAlertMessage("The reflection dashboard couldn't be generated for this group — no analysis data was found for your username in it.");
        setShowAlert(true);
      }
    }

  };

  const loadReflectionDashboardForNewSelection = async (newSessionDeviceID) => {
    setdeviceIDRefectionDashboard(newSessionDeviceID)
    setReflectionDashboardDoneLoading(false)
    let actionstatus = false
    try {
      actionstatus = await extractParticipantData(newSessionDeviceID)
    } catch (error) {
      console.error("loadReflectionDashboardForNewSelection", error)
    }
    if (actionstatus) {
      const sess = previousSessions.current.find((ses) => ses.id === selectedSessionId1);
      const group = selectFilteredDevice1.find((dev) => dev.id === newSessionDeviceID);
      setSessionNameForReflecDashboard(sess.name + ": " + new Date(sess.creation_date).toDateString())
      setGroupNameForReflecDashboard(group.name)
    } else {
      setAlertMessage("The reflection dashboard couldn't be generated for this group — no analysis data was found for your username in it.");
      setShowAlert(true);
    }
    // Always leave the loading state; a failure shows the alert instead of
    // spinning forever.
    setReflectionDashboardDoneLoading(true)
  }


  const extractParticipantData = async (deviceId) => {
    const resp = await loadSynthesizedMetric(deviceId)

    let respObj = buildData("Participant level sesssion analysis", selectedSessionId1, deviceId, null, null)
    if (Object.keys(respObj).length === 0 || resp === false) {
      return false;
    }
    // The username may not appear in this pod's metrics at all (wrong group,
    // or a pod analyzed before this student joined) — bail cleanly instead of
    // sending an incomplete request the server 500s on.
    if (!respObj.participant_level_metric || !respObj.participant_level_metric.length) {
      return false;
    }

    const ret = await loadLLMAnalytics(respObj, deviceId)
    if (ret) {
      selectedLLMAnalysis.current = llmSessionAnalysis.current[selectedSessionId1][deviceId]
      selectedSynthesizedData.current = respObj
      await loadprompthistory(deviceId)
      setSelectedMomentIdAndIndex([0, selectedSynthesizedData.current.participant_level_metric[0].windowid])

      return ret
    } else {
      return ret
    }
  }

  const interactivePromptFnc = async (selSessionId, selSessionDeviceId, default_question_id, question) => {
    let respObj = buildData("interactive prompting", selSessionId, selSessionDeviceId, default_question_id, question)
    let existingPrompt = null
    if (promptHistory.current.hasOwnProperty(selSessionId) && promptHistory.current[selSessionId].hasOwnProperty(selSessionDeviceId)) {
      existingPrompt = promptHistory.current[selSessionId][selSessionDeviceId].find(p => (default_question_id !== -1 && p?.default_question_id === default_question_id)) ?? null;
    }

    if (existingPrompt !== null) {
      if (promptResponses.current.hasOwnProperty(selSessionId) && promptResponses.current[selSessionId].hasOwnProperty(selSessionDeviceId)) {
        promptResponses.current[selSessionId][selSessionDeviceId].push([question, existingPrompt.answer])
      } else if (promptResponses.current.hasOwnProperty(selSessionId)) {
        promptResponses.current[selSessionId][selSessionDeviceId] = [[question, existingPrompt.answer]]
      } else {
        promptResponses.current[selSessionId] = {
          [selSessionDeviceId]: [[question, existingPrompt.answer]]
        }
      }

    } else {
      try {

        const response = await new SessionService().getLLMPromptResponse(respObj);

        if (response.status === 200) {
          const jsonObj = await response.json()

          if (promptResponses.current.hasOwnProperty(selSessionId) && promptResponses.current[selSessionId].hasOwnProperty(selSessionDeviceId)) {
            promptResponses.current[selSessionId][selSessionDeviceId].push([question, jsonObj.answer])
          } else if (promptResponses.current.hasOwnProperty(selSessionId)) {
            promptResponses.current[selSessionId][selSessionDeviceId] = [[question, jsonObj.answer]]
          } else {
            promptResponses.current[selSessionId] = {
              [selSessionDeviceId]: [[question, jsonObj.answer]]
            }
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

  const buildData = (reporttype, sessionId, deviceId, defaultQuestionId, question) => {
    let retObj = {}
    if (synthesizedFeedbackMetrics.current.hasOwnProperty(sessionId) && synthesizedFeedbackMetrics.current[sessionId].hasOwnProperty(deviceId)) {
      if (reporttype === "Participant level sesssion analysis") {
        retObj["participant_name"] = userDetail.username
        retObj["sessionid"] = sessionId
        retObj["sessiondeviceid"] = deviceId
        retObj["retrieve_existing_report"] = "true"
        retObj["participant_level_metric"] = synthesizedFeedbackMetrics.current[sessionId][deviceId]["participants_level"][userDetail.username]
        retObj["session_level_metric"] = synthesizedFeedbackMetrics.current[sessionId][deviceId]["session_level"][userDetail.username]
        retObj["group_level_metric"] = synthesizedFeedbackMetrics.current[sessionId][deviceId]["group_level"]
      } else if (reporttype === "interactive prompting") {
        retObj["participant_name"] = userDetail.username
        retObj["sessionid"] = sessionId
        retObj["sessiondeviceid"] = deviceId
        retObj["retrieve_existing_answer"] = "true"
        retObj["default_question_id"] = defaultQuestionId
        retObj["question"] = question
        retObj["window_level_metric"] = synthesizedFeedbackMetrics.current[sessionId][deviceId]["window_level"]
        retObj["session_level_metric"] = synthesizedFeedbackMetrics.current[sessionId][deviceId]["session_level"]
        retObj["group_level_metric"] = synthesizedFeedbackMetrics.current[sessionId][deviceId]["group_level"]
      }
    }
    return retObj
  }

  const loadSynthesizedMetric = async (deviceId) => {
    // This is needed to load the synthesized feedback for the Reflection dashboard
    if (!synthesizedFeedbackMetrics.current.hasOwnProperty(selectedSessionId1)) {
      const data = await getSynthesizedMetric(deviceId)

      if (data === null) {
        return false
      }
      synthesizedFeedbackMetrics.current[selectedSessionId1] = {
        [deviceId]: data
      }
    } else {
      if (!synthesizedFeedbackMetrics.current[selectedSessionId1].hasOwnProperty(deviceId)) {
        const data = await getSynthesizedMetric(deviceId)

        if (data === null) {
          return false
        }
        synthesizedFeedbackMetrics.current[selectedSessionId1][deviceId] = data
      }

    }
    return true
  }

  const loadLLMAnalytics = async (respObj, deviceId) => {

    if (!llmSessionAnalysis.current.hasOwnProperty(selectedSessionId1)) {
      const analysis = await getLLMAnalytics(respObj)

      if (analysis === null) {
        return false
      }

      llmSessionAnalysis.current[selectedSessionId1] = {
        [deviceId]: analysis
      }
    } else {
      if (!llmSessionAnalysis.current[selectedSessionId1].hasOwnProperty(deviceId)) {

        const analysis = await getLLMAnalytics(respObj)
        if (analysis === null) {
          return false
        }

        llmSessionAnalysis.current[selectedSessionId1][deviceId] = analysis

      }

    }
    return true
  }

  const loadprompthistory = async (deviceId) => {
    if (!promptHistory.current.hasOwnProperty(selectedSessionId1)) {
      const prompt = await getPrompthistory(deviceId)

      if (prompt === null) {
        return false
      }

      promptHistory.current[selectedSessionId1] = {
        [deviceId]: prompt
      }
    } else {
      if (!promptHistory.current[selectedSessionId1].hasOwnProperty(deviceId)) {

        const prompt = await getPrompthistory(deviceId)
        if (prompt === null) {
          return false
        }

        promptHistory.current[selectedSessionId1][deviceId] = prompt

      }

    }
    return true
  }


  const getSynthesizedMetric = async (deviceId) => {
    try {
      const response = await new SessionService().getSynthesizedFeedbackMetrics(selectedSessionId1, deviceId);
      if (response.status === 200) {
        const jsonObj = response.json()
        return jsonObj;
      } else {
        return null
      }
    } catch (error) {
      console.error(
        "student dashboard getSynthesizedMetric",
        error,
      )
      return null
    }
  }

  const getLLMAnalytics = async (respObj) => {
    try {
      const response = await new SessionService().getLLMFeedbackBasedOnMetrics(respObj);

      if (response.status === 200) {
        const jsonObj = await response.json()
        return jsonObj.answer
      }
      // Any non-200 (including 500s) must resolve to null — an undefined
      // return here slipped past the callers' null checks and stranded the
      // page on the loading dialog.
      console.error("LLM api response status", response.status)
      return null
    } catch (error) {
      console.error(
        "student dashboard loadReflectiondashboard",
        error,
      )
      return null
    }
  }

  const getPrompthistory = async (deviceId) => {
    try {
      const response = await new SessionService().get_llm_question_answer_interactions(selectedSessionId1, deviceId, userDetail.username);

      if (response.status === 200) {
        const jsonObj = await response.json()
        return jsonObj
      } else if (response.status === 400) {
        return null
      }
    } catch (error) {
      console.error(
        "student dashboard loadprompthistory",
        error,
      )
      return null
    }
  }


  ///------------------------------------------------------------------------//
  // END OF IMPLEMENTATION FOR THE REFLECTION DASHBOARD 
  //-----------------------------------------------------------------------//

  // A group must be open for either view. If the session has exactly one
  // group, open it implicitly; otherwise ask the student to pick one.
  const ensureDeviceSelected = () => {
    if (selectedSessionDeviceId1 !== -1) return selectedSessionDeviceId1
    if ((sessionDevices || []).length === 1) {
      loadSelectedSessionDeviceMetrics(sessionDevices[0].id)
      return sessionDevices[0].id
    }
    setAlertMessage(
      sessionChosen.current
        ? "Pick one of your groups from the list first."
        : "Open a session from the list first, then pick your group.",
    )
    setShowAlert(true)
    return -1
  }

  const viewComparison = () => {
    // Only setting `details` did nothing when the student was still on the
    // sessions/groups list — the report view never opened.
    if (ensureDeviceSelected() === -1) return
    setDetails("Comparison")
    setDisplaySingleSession(false);
    setNextPage("displayreportpage")
  }

  const viewIndividual = () => {
    setDetails("Individual")
  }

  const ResetTimeRange = (values) => {
    if (session.current !== null) {
      const sessionLen =
        Object.keys(session.current).length > 0 ? session.current.length : 0
      setTimeRange(values)
      const start = Math.round(sessionLen * values[0] * 100) / 100
      const end = Math.round(sessionLen * values[1] * 100) / 100
      setStartTime(start)
      setEndTime(end)
      generateDisplayTranscripts(session1Transcripts, start, end)
      generateDisplayVideoMetrics(session1VideoMetrics, start, end)
    }
  }

  const seeAllTranscripts = () => {
    if (Object.keys(currentTranscript).length > 0) { //&& sessionDevice !== null
      setCurrentForm("gottoselectedtranscript")
    }
    // else if (sessionDevice !== null) {
    //     setCurrentForm("gototranscript")
    // }
  }

  const closeDialog = () => {
    setCurrentForm("")
  }
  const navigateToLogin = (confirmed = false) => {
    return navigate("/")
  }

  // Hierarchical back: from a pod/reflection/comparison view, return to the
  // student's sessions overview; from the sessions list or the username
  // entry, leave the dashboard.
  const handleBack = () => {
    const inPodView =
      nextPage === "displaygrouppage" ||
      nextPage === "displayreportpage" ||
      nextPage === "Reflection Dashboard"
    if (!inPodView) {
      navigate("/")
      return
    }
    // Reset the drill-down state so reopening a session (even the same one)
    // auto-opens its group again.
    sessionChosen.current = false
    autoOpenedFor.current = null
    setSelectedSessionDeviceId1(-1)
    setSelectedSessionId1(-1)
    setDetails("Individual")
    setReflectionDashboardDoneLoading(false)
    setNextPage("sessionlistpage")
  }

  const loading = () => {
    return session.current === null || transcripts.length === 0
  }

  const changeTouppercase = (e) => {
    let val = e.target.value.toUpperCase()
    if (val.length <= 4) {
      setWrongInput(false)
    } else {
      setWrongInput(true)
    }
    setPcode(val)
  }

  const closeAlert = () => {
    setShowAlert(false)
  }

  const handleRate = (criterion, value) => {
    setRatings((prev) => ({ ...prev, [criterion]: value }));
    setSubmitted(false);
  };

  const handleSubmit = async (allComplete) => {
    // e.preventDefault();
    if (!allComplete) return;

    ratings.notes = notes
    const payload = {
      sessionid: selectedSessionId1,
      sessionDeviceId: selectedSessionDeviceId1,
      username: userDetail.username,
      response: ratings,
    };

    // console.log("payload ",payload)
    try {
      const response = await new SessionService().postSurveyResponse(payload);

      if (response.status === 200) {
        // console.log("Submitted rating form:", payload);
        setDialogHeading("Success")
        setSubmitted(true);
        setAlertMessage("Survey Submission Successful");
        setShowAlert(true);
      } else if (response.status === 400) {
        setDialogHeading("Error")
        setAlertMessage("Survey Submission Unsuccessful, Please contact Admin");
        setShowAlert(true);
      }
    } catch (error) {
      console.error(
        "Student dashboard handle submit",
        error,
      )
      return null
    }

  };

  const onClickedTimeline = (transcript) => {
    setCurrentForm("Transcript")
    setCurrentTranscript(transcript)
  }

  const onClickedKeyword = (transcript) => {
    setCurrentTranscript(transcript)
    setCurrentForm("gottoselectedtranscript")
  }

  return (
    <StudentSessionDashboardPages
      currentForm={currentForm}
      closeDialog={closeDialog}
      nextPage={nextPage}
      pageTitle={pageTitle.current}
      navigateToLogin={navigateToLogin}
      handleBack={handleBack}
      sessiontype={sessiontype}
      setSessiontype={setSessiontype}
      changeTouppercase={changeTouppercase}
      setShowAlert={setShowAlert}
      showAlert={showAlert}
      closeAlert={closeAlert}
      alertMessage={alertMessage}
      pcode={pcode}
      loadDashboard={loadDashboard}
      wrongInput={wrongInput}
      setRange={ResetTimeRange}
      loading={loading}
      displayTranscripts={displayTranscripts}
      displayVideoMetrics={displayVideoMetrics}
      showFeatures={showFeatures}
      showBoxes={showBoxes}
      userDetail={userDetail}
      session={session.current}
      previousSessions={previousSessions.current}
      startTime={startTime}
      endTime={endTime}
      startTime2={startTime2}
      endTime2={endTime2}
      transcripts={transcripts}
      videoMetrics={videoMetrics}
      onClickedTimeline={onClickedTimeline}
      onClickedKeyword={onClickedKeyword}
      details={details}
      viewComparison={viewComparison}
      viewIndividual={viewIndividual}
      loadSelectedSessionMetrics={loadSelectedSessionMetrics}
      loadComparedSessionDeviceMetrics={loadComparedSessionDeviceMetrics}
      selectedSessionId1={selectedSessionId1}
      setSelectedSessionId1={setSelectedSessionId1}
      selectedSessionDeviceId1={selectedSessionDeviceId1}
      selectedSessionDeviceId2={selectedSessionDeviceId2}
      selectedSessionId2={selectedSessionId2}
      setSelectedSessionId2={setSelectedSessionId2}
      session1Transcripts={session1Transcripts}
      session2Transcripts={session2Transcripts}
      session1VideoMetrics={session1VideoMetrics}
      session2VideoMetrics={session2VideoMetrics}
      seeAllTranscripts={seeAllTranscripts}
      currentTranscript={currentTranscript}
      transcriptDoneLoading={transcriptDoneLoading}
      videoMetricDoneLoading={videoMetricDoneLoading}
      sessionDevices={sessionDevices}
      loadSelectedSessionDeviceMetrics={loadSelectedSessionDeviceMetrics}
      getSessionDevices={getSessionDevices}
      selectFilteredDevice1={selectFilteredDevice1}
      selectFilteredDevice2={selectFilteredDevice2}
      dialogHeading={dialogHeading}

      loadReflectiondashboard={loadReflectiondashboard}
      reflectionDashboardDoneLoading={reflectionDashboardDoneLoading}
      selectedLLMAnalysis={selectedLLMAnalysis.current}
      selectedSynthesizedData={selectedSynthesizedData.current}
      promptResponses={promptResponses.current}
      isThinking={isThinking}
      setIsThinking={setIsThinking}
      selectedMomentIdAndIndex={selectedMomentIdAndIndex}
      setSelectedMomentIdAndIndex={setSelectedMomentIdAndIndex}
      setdeviceIDRefectionDashboard={setdeviceIDRefectionDashboard}
      sessionNameForReflecDashboard={sessionNameForReflecDashboard}
      groupNameForReflecDashboard={groupNameForReflecDashboard}
      interactivePromptFnc={interactivePromptFnc}
      loadReflectionDashboardForNewSelection={loadReflectionDashboardForNewSelection}


      //Survey props
      surveyquestion={surveyquestion}
      likertOptions={likertOptions}
      completedCount={completedCount}
      ratings={ratings}
      handleRate={handleRate}
      handleSubmit={handleSubmit}
      submitted={submitted}
      setNotes={setNotes}
      notes={notes}
      pathToSurveyOptions={pathToSurveyOptions}

    />
  );
}

export { StudentSessionDashboard };
