import { useEffect, useState, useRef } from "react"
import { POD_ON_COLOR as POD_COLOR, GLOW_COLOR } from "../components/pod-colors"
import { ProfileCreationPage } from "./html-pages"
import { AuthService } from '../services/auth-service';
import { ApiService } from "../services/api-service"
import { useNavigate } from "react-router-dom"
import fixWebmDuration from "fix-webm-duration"
import { ensureGetUserMedia } from "../utilities/media"

// Reply from either processing socket when the addstudent enrollment token is
// missing, expired or minted for another alias (audio server.py / video
// server_posthoc.py). It arrives before the recording page exists, so it is
// shown as the page alert rather than the inline enrollStatus.
const ENROLLMENT_DENIED = "Enrollment not authorised. Start again from the sign-up page."


function SignupPage() {
    const [pageTitle] = useState("Create Account")
    const [nextPage, setNextPage] = useState("profile_creation")
    const [studentObject, setStudentObject] = useState(null);
    const [showAlert, setShowAlert] = useState(false)
    const [alertMessage, setAlertMessage] = useState("")
    const [constraintObj, setConstraintObj] = useState(null)
    const [streamReference, setStreamReference] = useState(null)
    const [mediaRecorder, setMediaRecorder] = useState(null)
    const [audioAuthenticated, setAudioAuthenticated] = useState(false)
    const [videoAuthenticated, setVideoAuthenticated] = useState(false)
    const [audioSocketProccesorConnected, setAudioSocketProccesorConnected] = useState(false)
    const [videoSocketProccesorConnected, setVideoSocketProcesorConnected] = useState(false)
    const [audioSaved, setAudioSaved] = useState(false)
    const [videoSaved, setVideoSaved] = useState(false)
    const [videoBlob, setVideoBlob] = useState(null)
    const [videoData, setVideoData] = useState(null)
    const [currentForm, setCurrentForm] = useState("")
    const [displayText] = useState("")
    const video_captured = useRef(null)
    const [isRecordingStopped, setIsRecordingStopped] = useState(false);
    const [mimetype, setMimeType] = useState(null)
    const [mimeExtension, setMimeExtension] = useState(null);
    const [studentUpdate, setStudentUpdated] = useState(false)
    const [audioDisconnected, setAudioDisconnected] = useState(false)
    const [videoDisconnected, setVideoDisconnected] = useState(false)
    // const [userProfileDetails, setUserProfileDetail] = useState(null)
    const audiows = useRef(null)
    const videows = useRef(null)
    // Consecutive voice-quality failures; after 3 the next save is accepted
    // as-is so an unusual voice or setup can never lock a student out.
    const qualityFails = useRef(0)
    // Inline enrollment verification status shown on the recording page
    // (replaces the success/quality popups): idle | saving | success |
    // quality_failed | error, with a message.
    const [enrollStatus, setEnrollStatus] = useState({ kind: "idle", message: "" })
    const navigate = useNavigate()

    const apiService = new ApiService()
    const interval = 10000

    // Refs, not per-render `let`s — same fix as the join page: the release
    // path ran in a later render whose binding was null, and each acquire
    // stacked another permanent visibilitychange re-acquirer.
    const wakeLock = useRef(null)
    const wakeLockVisListener = useRef(null)

    useEffect(() => {
        if (studentObject != null) {
            setCurrentForm("processing")
            const constraint = {}
            constraint.audio = true
            constraint.video = {
                facingMode: "user",
                width: 500, //{ min: 640, ideal: 1280, max: 1920 },
                height: 500, //{ min: 480, ideal: 720, max: 1080 }
            }
            setConstraintObj(constraint)
        }
    }, [studentObject])

    useEffect(() => {
        if (constraintObj !== null) {
            detectMediaSouceTypeRef.current()

        }
    }, [constraintObj])



    useEffect(() => {
        if (mimeExtension !== null && mimetype !== null) {
            if (mimeExtension !== "" && (mimetype.endsWith("opus") || mimetype.endsWith("aac") || mimetype.endsWith("mp4a.40.2"))) {
                // The fingerprint handlers live on the live AUDIO service but
                // the POSTHOC video service (/video_socket's live video server
                // has no save-audio-video-fingerprinting handler — connecting
                // there never acks and enrollment hangs on "Processing…").
                audiows.current = new WebSocket(apiService.getAudioWebsocketEndpoint())
                videows.current = new WebSocket(apiService.getVideoPosthocWebsocketEndpoint())
                connect_audio_websocket_processor_service();
                connect_video_websocket_processor_service();
            } else {
                setCurrentForm("")
                setAlertMessage("No Audio Source detected, please use another device");
                setShowAlert(true);
            }
        }
    }, [mimetype, mimeExtension])

    useEffect(() => {
        if (audioSocketProccesorConnected && videoSocketProccesorConnected) {
            requestStartAudioVideoProcessingRef.current()
        }
    }, [audioSocketProccesorConnected, videoSocketProccesorConnected])

    useEffect(() => {
        if (audioAuthenticated && videoAuthenticated) {
            setCurrentForm("")
            setNextPage("video_audio_capture_page")

        } else {
            // setCurrentForm("processing")
        }
    }, [audioAuthenticated, videoAuthenticated])

    useEffect(() => {
        if (streamReference && mediaRecorder) {
            setCurrentForm("")
            const videoPlay = () => {
                let video = document.getElementById('video_preview')
                video.srcObject = streamReference
                video.onloadedmetadata = function (ev) {
                    video.play()
                    mediaRecorder.start(interval)
                }

                mediaRecorder.ondataavailable = async function (ev) {

                    if (ev.data.size && video_captured.current === null) {
                        if (ev.data && ev.data.size !== 0) {
                            video_captured.current = ev.data
                            setVideoData(ev.data)
                            stopRecording()
                        }
                    }

                }

                // When stopped, you can combine the chunks into a single Blob:
                mediaRecorder.onstop = function () {
                    const blob = new Blob([video_captured.current], { type: mimetype });
                    setVideoBlob(blob)
                    video.pause();
                    video.srcObject = null;
                    setIsRecordingStopped(true)
                };
            }



            videoPlay()

        }
    }, [streamReference, mediaRecorder])

    useEffect(() => {
        if (document.getElementById('video_playback') !== null) {

            if (wakeLock) {
                releaseWakeLock()
            }
            if (streamReference != null) {
                streamReference.getAudioTracks().forEach((track) => track.stop())
                setStreamReference(null)
            }

            if (mediaRecorder != null) {
                mediaRecorder.stop()
                setMediaRecorder(null)
            }

            const url = URL.createObjectURL(videoBlob);
            document.getElementById('video_playback').src = url;
        }
    }, [isRecordingStopped])

    useEffect(() => {
        if (audioSaved && videoSaved) {
            new AuthService().updateStudentProfile(studentObject.id, studentObject.lastname, studentObject.firstname, "yes", setStudentUpdated, setAlertMessage, setShowAlert);
        }
    }, [audioSaved, videoSaved])

    useEffect(() => {
        if (studentUpdate) {
            // Inline success on the recording page instead of a popup.
            setCurrentForm("")
            setEnrollStatus({
                kind: "success",
                message: "Your voice and face were verified and saved. You're enrolled.",
            })
        }
    }, [studentUpdate])

    useEffect(() => {
        if (audioDisconnected || videoDisconnected) {
            setCurrentForm("")
            setAlertMessage("You are Disconnected from the Server, please reload the page");
            setShowAlert(true);
        }
    }, [audioDisconnected, videoDisconnected])

    // Watchdog: if the services haven't both acknowledged within 30s, stop
    // the spinner and say so instead of processing forever.
    useEffect(() => {
        if (currentForm !== "processing" || (audioAuthenticated && videoAuthenticated)) return
        const t = setTimeout(() => {
            setCurrentForm("")
            setAlertMessage("The enrollment services did not respond. Please try again in a moment.")
            setShowAlert(true)
        }, 30000)
        return () => clearTimeout(t)
    }, [currentForm, audioAuthenticated, videoAuthenticated])

    // Save-phase watchdog: quality check + face embedding normally finish
    // within a minute; past two minutes something server-side died without
    // replying, so surface it inline instead of spinning forever.
    useEffect(() => {
        if (enrollStatus.kind !== "saving") return
        if (audioSaved && videoSaved) return
        const t = setTimeout(() => {
            setEnrollStatus({
                kind: "error",
                message: "Saving is taking too long — the server may have hit an error. Please record again.",
            })
        }, 120000)
        return () => clearTimeout(t)
    }, [enrollStatus.kind, audioSaved, videoSaved])

    useEffect(() => {
        if (audioAuthenticated && videoAuthenticated) {
            if (audiows.current === null || audiows.current.readyState !== WebSocket.OPEN || videows.current === null || videows.current.readyState !== WebSocket.OPEN) return;

            const send = () => {
                if (audiows.current.readyState === WebSocket.OPEN && videows.current.readyState === WebSocket.OPEN) {
                    audiows.current.send(JSON.stringify({ type: "heartbeat", key:"no key" }));
                    videows.current.send(JSON.stringify({ type: "heartbeat",key:"no key" }));

                } else {
                    clearInterval(id);
                }
            };

            // fire once immediately, then on interval
            send();
            const id = setInterval(send, 20000);

            return () => {
                clearInterval(id);
            };
        }
    }, [audiows, videows, audioAuthenticated, videoAuthenticated]);


    const stopRecording = () => {
        if (mediaRecorder && mediaRecorder.state === "recording") {
            mediaRecorder.stop(); // <-- this will fire mediaRecorder.onstop
        }
    };

    // Token rejected: tell the student on the sign-up form and stop. Both
    // sockets are closed quietly (their onclose would otherwise replace this
    // with the generic "Disconnected" alert) so no media is ever sent.
    const stopEnrollment = (message) => {
        for (const ws of [audiows, videows]) {
            if (ws.current !== null) {
                ws.current.onclose = null
                ws.current.close()
                ws.current = null
            }
        }
        setAudioAuthenticated(false)
        setVideoAuthenticated(false)
        setCurrentForm("")
        setAlertMessage(message)
        setShowAlert(true)
    }

    const saveRecording = async (videoBlob, duration) => {
        if (videoBlob) {
            if (mimetype.startsWith('video/webm')) {
                const fixedBlob = await fixWebmDuration(videoBlob, duration*1000);
                if (qualityFails.current >= 3 && audiows.current?.readyState === WebSocket.OPEN) {
                    audiows.current.send(JSON.stringify({ type: "fingerprint-force" }))
                }
                // Fresh attempt: clear the previous save-state flags so the
                // "both saved" effect only fires for THIS submission.
                setAudioSaved(false)
                setVideoSaved(false)
                setStudentUpdated(false)
                setEnrollStatus({ kind: "saving", message: "" })
                videows.current.send(fixedBlob)
                audiows.current.send(fixedBlob)
            } else if (mimetype.startsWith('video/mp4')) {

            }

        }
    };

    // const saveRecording = () => {
    //     if (videoData) {
    //         if (mimetype.startsWith('video/webm')) {
    //             fixWebmDuration(
    //                 videoData,
    //                 interval,
    //                 (fixedblob) => {
    //                     videows.current.send(fixedblob)
    //                     audiows.current.send(fixedblob)
    //                     setCurrentForm("processing")
    //                 },
    //             )
    //         } else if (mimetype.startsWith('video/mp4')) {

    //         }

    //     }
    // };





    const resetForRecording = () => {
        if (videoData !== null) {
            setVideoData(null)
        }
        if (video_captured.current != null) {
            video_captured.current = null
        }
        if (isRecordingStopped) {
            setIsRecordingStopped(false)
        }
        if (streamReference != null) {
            streamReference.getAudioTracks().forEach((track) => track.stop())
            setStreamReference(null)
        }

        if (mediaRecorder != null) {
            mediaRecorder.stop()
            setMediaRecorder(null)
        }
    }

    const startRecording = async () => {
        try {
            //reset all parameter in case of video recapturing
            await acquireWakeLock()

            resetForRecording()

            //handle older browsers that might implement getUserMedia in some way
            ensureGetUserMedia()

            if (navigator.mediaDevices != null) {
                const stream = await navigator.mediaDevices.getUserMedia(constraintObj)



                if (mimetype !== "") {
                    const mediaRec = new MediaRecorder(stream, { mimeType: mimetype })
                    setStreamReference(stream)
                    setMediaRecorder(mediaRec)
                }



            } else {
                setShowAlert(true)
                setAlertMessage("No media devices detected.")
            }

        } catch (ex) {
            console.error(ex)
            setShowAlert(true)
            setAlertMessage("Failed to get user audio-visual source.")
        }
    };

    const closeResources = () => {
        setIsRecordingStopped(false)
        setAudioSocketProccesorConnected(false)
        setVideoSocketProcesorConnected(false)
        if (audiows.current != null) {
            audiows.current.close();
            audiows.current = null;
        }

        if (videows.current != null) {
            videows.current.close();
            videows.current = null;
        }
        if (video_captured.current != null) {
            video_captured.current = null
        }

        if (isRecordingStopped) {
            setIsRecordingStopped(false)
        }
        if (streamReference != null) {
            streamReference.getAudioTracks().forEach((track) => track.stop())
            setStreamReference(null)
        }

        if (mediaRecorder != null) {
            stopRecorder(mediaRecorder)
            // mediaRecorder.stop()
            setMediaRecorder(null)
        }
        navigateToLogin();
    }

    function stopRecorder(mediarec) {
        if (!mediarec) return Promise.resolve();
        if (mediarec.state === 'inactive') return Promise.resolve();

        return new Promise (res => {
            const onStop = () => {
                mediarec.removeEventListener('stop', onStop);
                res();
            };
            mediarec.addEventListener('stop', onStop, { once: true });
            try { mediarec.stop(); } catch { res(); }
        });
    }

    const detectMediaSouceTypeRef = useRef(null);
    const detectMediaSouceType = async () => {
        // A rejected getUserMedia (permission denied, no camera) used to
        // bubble out unhandled, stranding the page on "Processing…" forever.
        try {
            const mediaType = await pickMimeType(constraintObj)
            const mediaExt = (mediaType !== "" && mediaType.indexOf("webm") !== -1) ? "webm" : (mediaType !== "" && mediaType.indexOf("mp4") !== -1) ? "mp4" : ""
            setMimeType(mediaType)
            setMimeExtension(mediaExt)
        } catch (ex) {
            setCurrentForm("")
            setAlertMessage(
                ex && ex.name === "NotAllowedError"
                    ? "Camera and microphone access are needed to enroll. Please allow access and try again."
                    : "No camera/microphone could be opened (" + ((ex && ex.name) || "error") + "). Enrollment needs both.",
            )
            setShowAlert(true)
        }
    }

    detectMediaSouceTypeRef.current = detectMediaSouceType;

    const pickMimeType = async (constraintObj) => {
        const stream = await navigator.mediaDevices.getUserMedia(constraintObj)
        const hasAudio = stream.getAudioTracks().length > 0;
        // Probe only — release the devices (the capture page reopens them).
        setTimeout(() => stream.getTracks().forEach((t) => t.stop()), 0);

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


    const verifyUserProfileInput = (lastname, firstname, username) => {
        if (lastname === '') {
            setAlertMessage("Please Enter Your Last Name");
            setShowAlert(true);
            document.getElementById("lastname").focus();
        } else if (firstname === '') {
            setAlertMessage("Please Enter Your First Name");
            setShowAlert(true);
            document.getElementById("firstname").focus();
        } else if (username === '') {
            setAlertMessage("Please Enter Your User Id");
            setShowAlert(true);
            document.getElementById("username").focus();
        } else if (username.length < 5 || username.length > 10) {
            setAlertMessage("User name should be atleast 5 characters and not more than 10 characters");
            setShowAlert(true);
            document.getElementById("username").focus();
        } else {
            // setUserProfileDetail([lastname, firstname, username])
            new AuthService().createStudentProfile(lastname, firstname, username, setStudentObject, setAlertMessage, setShowAlert);
        }
    }


    // Connects to audio processor websocket server.
    const connect_audio_websocket_processor_service = () => {
        audiows.current.binaryType = "blob"

        audiows.current.onopen = (e) => {
            setAudioSocketProccesorConnected(true)
        }

        audiows.current.onmessage = (e) => {
            if (typeof e.data === 'string') {
                const message = JSON.parse(e.data);
                if (message['type'] === 'saveaudiovideo') {
                    setAudioAuthenticated(true);
                }
                // else if(message['type'] === 'attention_data'){

                // }
                else if (message['type'] === 'error') {
                    if (message['message'] === ENROLLMENT_DENIED) {
                        stopEnrollment(message['message'])
                        return
                    }
                    stopRecording();
                    setCurrentForm("")
                    setEnrollStatus({
                        kind: "error",
                        message: message['message'] || "Saving the recording failed. Please record again.",
                    })
                }
                else if (message['type'] === 'quality_failed') {
                    qualityFails.current += 1
                    setCurrentForm("")
                    const detail = message['detail'] || {}
                    if (detail.partial_kept) {
                        // Not enough speech yet, but the audio so far is KEPT
                        // server-side — the next take is appended, so the
                        // student only records the missing part.
                        setEnrollStatus({
                            kind: "continue",
                            message: message['message'] ||
                                "Almost there — record a little more speech; what you already recorded is kept.",
                            neededSeconds: detail.still_needed_seconds || 10,
                        })
                    } else {
                        setEnrollStatus({
                            kind: "quality_failed",
                            message:
                                (message['message'] || "The recording did not pass the voice-quality check.") +
                                (qualityFails.current >= 3 ? " Your next recording will be accepted as-is." : ""),
                        })
                    }
                }
                else if (message['type'] === 'saved') {
                    setAudioSaved(true)
                }
            }
        };

        audiows.current.onclose = e => {
            setAudioDisconnected(true)
        };
    }

    // Connects to video processor websocket server.
    const connect_video_websocket_processor_service = () => {
        videows.current.binaryType = "blob"

        videows.current.onopen = (e) => {
            setVideoSocketProcesorConnected(true)
        }

        videows.current.onmessage = (e) => {
            if (typeof e.data === 'string') {
                const message = JSON.parse(e.data);
                if (message['type'] === 'saveaudiovideo') {
                    setVideoAuthenticated(true);
                }
                // else if(message['type'] === 'attention_data'){

                // }
                else if (message['type'] === 'error') {
                    if (message['message'] === ENROLLMENT_DENIED) {
                        stopEnrollment(message['message'])
                        return
                    }
                    stopRecording();
                    setCurrentForm("")
                    setEnrollStatus({
                        kind: "error",
                        message: message['message'] || "Processing the video failed. Please record again.",
                    })
                }
                else if (message['type'] === 'saved') {
                    setVideoSaved(true)
                }
            }
        };

        videows.current.onclose = e => {
            setVideoDisconnected(true)
        };
    }

    // Begin capturing and sending student video sample.
    const requestStartAudioVideoProcessingRef = useRef(null);
    const requestStartAudioVideoProcessing = async () => {
        let message = null
        if (audiows.current === null) {
            return
        }

        if (videows.current === null) {
            return
        }

        message = {
            type: "save-audio-video-fingerprinting",
            stage: "start",
            id: studentObject.id,
            alias: studentObject.username,
            token: studentObject.enrollment_token,
            start_time: 0.0,
            sample_rate: 16000,
            encoding: "pcm_f16le",
            video_encoding: "video/webm",
            mimeextension: mimeExtension,
            channels: 2,
            streamdata: "audio-video-fingerprint"
        }

        audiows.current.send(JSON.stringify(message))
        videows.current.send(JSON.stringify(message))

    }




    requestStartAudioVideoProcessingRef.current = requestStartAudioVideoProcessing;

    const closeAlert = () => {
        setShowAlert(false)
    }

    const closeDialog = () => {
        setCurrentForm("")
        if (studentUpdate) {
            closeResources()
        }
    }


    const navigateToLogin = (confirmed = false) => {
        return navigate("/")
    }



    const acquireWakeLock = async () => {
        if (!("wakeLock" in navigator)) {
            console.error(
                "Screen Wake Lock API is not supported by the browser",
            )
            return
        }

        try {
            wakeLock.current = await navigator.wakeLock.request("screen")
            if (wakeLockVisListener.current === null) {
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




    return (
        <ProfileCreationPage
            nextPage={nextPage}
            closeAlert={closeAlert}
            showAlert={showAlert}
            alertMessage={alertMessage}
            verifyUserProfileInput={verifyUserProfileInput}
            GLOW_COLOR={GLOW_COLOR}
            POD_COLOR={POD_COLOR}
            navigateToLogin={navigateToLogin}
            pageTitle={pageTitle}
            startRecording={startRecording}
            stopRecording={stopRecording}
            isRecordingStopped={isRecordingStopped}
            saveRecording={saveRecording}
            closeResources={closeResources}
            closeDialog={closeDialog}
            displayText={displayText}
            currentForm={currentForm}
            enrollStatus={enrollStatus}
            // Release the processing sockets the capture page opened before
            // leaving — Done skipped this and left them to server-side
            // timeout (Skip already does it).
            onDoneEnrolling={() => {
                closeResources()
                navigateToLogin()
            }}
            // Leaving without enrolling: the account already exists, so this
            // just releases the processing sockets the capture page opened and
            // returns home. The coach stops the camera and mic before calling.
            onSkipEnrolling={() => {
                closeResources()
                navigateToLogin()
            }}
        />
    )
}

export { SignupPage }
