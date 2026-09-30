import { useState, useEffect } from "react"
import { useNavigate, useSearchParams } from "react-router-dom"
import { SessionService } from "../services/session-service"
import { SessionModel } from "../models/session"
import { FolderModel } from "../models/folder"
import { DiscussionSessionPage } from "./html-pages"
import { startPolling } from "../globals"

const LIST_POLL_MS = 15000

function SessionsComponent(props) {
    //forms = Forms;
    const [currentForm, setCurrentForm] = useState("")
    const [sessions, setSessions] = useState([])
    const [folders, setFolders] = useState([])
    const [displayedFolders, setDisplayedFolders] = useState([])
    const [displayedSessions, setDisplayedSessions] = useState([])
    const [selectableFolders, setSelectableFolders] = useState([])
    const [selectedSession, setSelectedSession] = useState(null)
    const [selectedFolder, setSelectedFolder] = useState(null)
    const [isLoading, setIsLoading] = useState(true)
    const [showAlert, setShowAlert] = useState(false)
    const [alertMessage, setAlertMessage] = useState("")
    const [breadcrumbs, setBreadCrumbs] = useState([])
    const [currentFolder, setCurrentFolder] = useState("")
    const [folderSelect, setFolderSelect] = useState(null)
    const [invalidName, setInvalidName] = useState(false)
    const [sortBy, setSortBy] = useState("date-desc")
    const [videoFilter, setVideoFilter] = useState("all")
    const [searchQuery, setSearchQuery] = useState("")
    // Bulk selection over session rows (Move to… / Delete N at once).
    const [selectedIds, setSelectedIds] = useState({})
    const navigate = useNavigate()
    const [searchParam, setSearchParam] = useSearchParams()

    // Poll the sessions list so live badges (Analyzing…) update without a
    // manual refresh. Chained (never overlapping), paused while the tab is
    // hidden, backed off on errors, aborted on unmount; the initial load
    // below does the first fetch, so this one waits a full interval.
    useEffect(() => {
        return startPolling(
            async (signal) => {
                const response = await new SessionService().getSessions({ signal })
                if (response.status !== 200) return false
                setSessions(SessionModel.fromJsonList(await response.json()))
                return true
            },
            LIST_POLL_MS,
            { immediate: false },
        )
    }, [])

    // Initial load. Any non-200, non-JSON body or network error clears the
    // spinner and says so — it used to spin forever on a 5xx.
    useEffect(() => {
        let cancelled = false
        const fail = (why) => {
            if (cancelled) return
            console.error("sessions-components: load failed", why)
            setIsLoading(false)
            setShowAlert(true)
            setAlertMessage(
                "Couldn't load your sessions" +
                    (typeof why === "number" ? " (server returned " + why + ")" : "") +
                    ". Check your connection and refresh to try again.",
            )
        }
        const load = async () => {
            try {
                const svc = new SessionService()
                const response = await svc.getSessions()
                if (response.status !== 200) return fail(response.status)
                const sessresult = SessionModel.fromJsonList(await response.json())
                const response2 = await svc.getFolders()
                if (response2.status !== 200) return fail(response2.status)
                const folderresult = FolderModel.fromJsonList(await response2.json())
                if (cancelled) return
                setSessions(sessresult)
                setFolders(folderresult)
                setIsLoading(false)
            } catch (error) {
                fail(error)
            }
        }
        load()
        return () => {
            cancelled = true
        }
    }, [currentForm])

    useEffect(() => {
        const folder = searchParam.get("folder")
        displayFolder(parseInt(folder, 10))
    }, [folders])

    const navigateToHomescreen = () => {
        navigate("/home", { replace: true })
    }

    const newRecording = () => {
        if (currentFolder !== "") {
            navigate("/sessions/new?folder=" + currentFolder, { replace: true })
        } else {
            navigate("/sessions/new", { replace: true })
        }
        //navigate('/sessions/new'+"?folder=", { queryParams: { folder: this.currentFolder } });
    }

    const goToSession = (session) => {
        navigate("/sessions/" + session.id)
    }

    // ----------
    // Folders
    // ----------

    const displayFolder = (folderId) => {
        if (!folderId) {
            setDisplayedFolders(folders.filter((f) => f.parent == null))
            setDisplayedSessions(sessions.filter((s) => s.folder == null))
            setBreadCrumbs([])
            setCurrentFolder("")
            navigate("/sessions")
            // const url = '/sessions';
            // this.location.replaceState(url);
        } else {
            setDisplayedFolders(folders.filter((x) => x.parent === folderId))
            setDisplayedSessions(sessions.filter((s) => s.folder === folderId))
            setCurrentFolder(folderId)
            buildBreadcrumbs(folderId)
            setSearchParam({ folder: folderId }, { replace: true })
            // const url = '/sessions/?folder=' + folderId;
            // this.location.replaceState(url);
        }
    }

    const buildBreadcrumbs = (folderId) => {
        let folder = folders.find((f) => f.id === folderId)
        const bread = []
        if (folder != null) {
            bread.unshift(folder)
            while (folder.parent) {
                folder = folders.find((f) => f.id === folder.parent)
                bread.unshift(folder)
            }
        }
        setBreadCrumbs(bread)
    }

    const goBackToPrevious = () => {
        if (breadcrumbs.length > 1) {
            displayFolder(breadcrumbs[breadcrumbs.length - 2].id)
        } else {
            displayFolder()
        }
    }

    const addFolder = (newName, locationId) => {
        if (!newName) {
            setShowAlert(true)
            setAlertMessage("Please enter folder name")
            closeDialog()
        } else {
            const fetchData = new SessionService().addFolder(
                newName,
                locationId,
            )
            fetchData
                .then(
                    async (response) => {
                        if (response.status === 200) {
                            const folder = FolderModel.fromJson(await response.json())
                            folders.push(folder)
                            if (locationId) {
                                displayFolder(folder.parent)
                            } else {
                                displayFolder()
                            }
                        } else {
                            setShowAlert(true)
                            setAlertMessage((await response.json())["message"])
                        }
                    },
                    (apierror) => {
                        console.error(
                            "sessions-components func: addFolder 1 ",
                            apierror,
                        )
                    },
                )
                .finally(() => closeDialog())
        }
    }

    const changeFolderName = (newName) => {
        if (newName === "") {
            setInvalidName(true)
            return
        }
        setInvalidName(false)
        setCurrentForm("Loading")
        const folderId = selectedFolder.id
        const fetchData = new SessionService().updateFolder(
            folderId,
            null,
            newName,
        )
        fetchData
            .then(
                async (response) => {
                    if (response.status === 200) {
                        const folder = FolderModel.fromJson(await response.json())
                        const folderIndex = folders.findIndex(
                            (s) => s.id === folder.id,
                        )
                        folders[folderIndex] = folder
                        displayFolder(folder.parent)
                    } else {
                        setShowAlert(true)
                        setAlertMessage((await response.json())["message"])
                    }
                },
                (apierror) => {
                    console.error(
                        "sessions-components func: changefoldername 1 ",
                        apierror,
                    )
                },
            )
            .finally(() => {
                closeDialog()
            })
    }

    const moveFolder = (newFolderId) => {
        const fetchData = new SessionService().updateFolder(
            selectedFolder.id,
            newFolderId,
            null,
        )
        fetchData
            .then(
                async (response) => {
                    if (response.status === 200) {
                        const updatedFolder = FolderModel.fromJson(
                            await response.json(),
                        )
                        const index = folders.findIndex(
                            (f) => f.id === updatedFolder.id,
                        )
                        folders[index] = updatedFolder
                        /* console.log("MOVE FOLDER");
          console.log(folders)
          console.log(updatedFolder);
          console.log(newFolderId); // ideally I'd want to change it to this thing
          const diffFolder = folders.findIndex(f => f.id === newFolderId); */
                        // this might need to be changed
                        displayFolder(updatedFolder.parent)
                    } else {
                        setShowAlert(true)
                        setAlertMessage((await response.json())["message"])
                    }
                },
                (apierror) => {
                    console.error(
                        "sessions-components func: movefolder 1 ",
                        apierror,
                    )
                },
            )
            .finally(() => {
                closeDialog()
            })
    }

    const deleteFolder = () => {
        setCurrentForm("Loading")
        const folderId = selectedFolder.id
        const parentId = selectedFolder.parent
        const fetchData = new SessionService().deleteFolder(folderId)
        fetchData
            .then(
                async (response) => {
                    if (response.status === 200) {
                        //const updatedFolder = FolderModel.fromJson(await response.json());
                        const foldersToRemove = [folderId]
                        const children = folders.filter(
                            (f) => f.parent === folderId,
                        )
                        while (children.length > 0) {
                            const child = children.pop()
                            foldersToRemove.push(child.id)
                            folders
                                .filter((f) => f.parent === child.id)
                                .forEach((f) => children.push(f))
                        }
                        setFolders(
                            folders.filter(
                                (f) => foldersToRemove.indexOf(f.id) === -1,
                            ),
                        )
                        setSessions(
                            sessions.filter(
                                (s) => foldersToRemove.indexOf(s.folder) === -1,
                            ),
                        )
                        if (parentId) {
                            displayFolder(parentId)
                        } else {
                            displayFolder()
                        }
                    } else {
                        setShowAlert(true)
                        setAlertMessage((await response.json())["message"])
                    }
                },
                (apierror) => {
                    console.error(
                        "sessions-components func: movefolder 1 ",
                        apierror,
                    )
                },
            )
            .finally(() => {
                closeDialog()
            })
    }

    // ----------
    // Sessions
    // ----------

    const endSession = (session) => {
        setCurrentForm("Loading")
        const sessionId = session.id
        setSelectedSession(null)
        const fetchData = new SessionService().endSession(sessionId)
        fetchData
            .then(
                async (response) => {
                    if (response.status === 200) {
                        response.json().then((json) => {
                            const endedSession = SessionModel.fromJson(json)
                            const sessionIndex = sessions.findIndex(
                                (s) => s.id === endedSession.id,
                            )
                            sessions[sessionIndex] = endedSession
                            displayFolder(endedSession.folder)
                        })
                    } else {
                        setShowAlert(true)
                        setAlertMessage((await response.json())["message"])
                    }
                },
                (apierror) => {
                    console.error(
                        "sessions-components func: endsession 1 ",
                        apierror,
                    )
                },
            )
            .finally(() => {
                closeDialog()
            })
    }

    const changeSessionName = (newName) => {
        if (newName === "") {
            setInvalidName(true)
            return
        }
        setInvalidName(false)
        setCurrentForm("Loading")
        const sessionId = selectedSession.id
        const fetchData = new SessionService().updateSession(sessionId, newName)
        fetchData.then(
            async (response) => {
                if (response.status === 200) {
                    response.json().then((json) => {
                        const session = SessionModel.fromJson(json)
                        const sessionIndex = sessions.findIndex(
                            (s) => s.id === session.id,
                        )
                        if (sessionIndex > -1) {
                            sessions[sessionIndex] = session
                            displayFolder(session.folder)
                        }
                    })
                }
            },
            (apierror) => {
                console.error(
                    "sessions-components func:changesesion 1 ",
                    apierror,
                )
            },
        )
        closeDialog()
    }

    // Inline rename from the row pencil — no dialog, no loading form.
    const renameSessionInline = (session, newName) => {
        const trimmed = (newName || "").trim()
        if (!trimmed || trimmed === session.title) return
        new SessionService().updateSession(session.id, trimmed).then(
            async (response) => {
                if (response.status === 200) {
                    response.json().then((json) => {
                        const updated = SessionModel.fromJson(json)
                        const i = sessions.findIndex(
                            (s) => s.id === updated.id,
                        )
                        if (i > -1) {
                            sessions[i] = updated
                            displayFolder(updated.folder)
                        }
                    })
                }
            },
            (apierror) => {
                console.error(
                    "sessions-components func: renameSessionInline ",
                    apierror,
                )
            },
        )
    }

    const moveSession = (newParentId) => {
        const fetchData = new SessionService().updateSessionFolder(
            selectedSession.id,
            newParentId,
        )
        fetchData
            .then(
                async (response) => {
                    if (response.status === 200) {
                        response.json().then((json) => {
                            const updatedSession = SessionModel.fromJson(json)
                            const index = sessions.findIndex(
                                (s) => s.id === updatedSession.id,
                            )
                            sessions[index] = updatedSession
                            displayFolder(updatedSession.folder)
                        })
                    } else {
                        setShowAlert(true)
                        setAlertMessage((await response.json())["message"])
                    }
                },
                (apierror) => {
                    console.error(
                        "sessions-components func:movesession 1 ",
                        apierror,
                    )
                },
            )
            .finally(() => {
                closeDialog()
            })
    }

    const deleteSession = () => {
        setCurrentForm("Loading")
        const sessionId = selectedSession.id
        const fetchData = new SessionService().deleteSession(sessionId)
        fetchData
            .then(
                async (response) => {
                    if (response.status === 200) {
                        setSessions(sessions.filter((s) => s.id !== sessionId))
                    } else {
                        setShowAlert(true)
                        setAlertMessage((await response.json())["message"])
                    }
                },
                (apierror) => {
                    console.error(
                        "sessions-components func deletesession 1 ",
                        apierror,
                    )
                },
            )
            .finally(() => {
                closeDialog()
            })
    }

    // ----------
    // Dialogs
    // ----------

    const openSessionDialog = (newForm, selectedSession) => {
        setCurrentForm(newForm)
        setSelectedSession(selectedSession)
        if (newForm === "MoveSession") {
            setSelectableFolders(FolderModel.fileable(folders, props.userdata))
        }
    }

    const openFolderDialog = (newForm, selectedFolder) => {
        setCurrentForm(newForm)
        setSelectedFolder(selectedFolder)
        if (newForm === "MoveFolder") {
            setSelectableFolders(
                FolderModel.fileable(folders, props.userdata).filter(
                    (f) =>
                        f.parent !== selectedFolder.id &&
                        f.id !== selectedFolder.id,
                ),
            )
        }
    }

    const closeDialog = () => {
        setCurrentForm("")
        setSelectedSession(null)
        setSelectedFolder(null)
        setFolderSelect(null)
        setInvalidName(false)
    }

    const closeAlert = () => {
        setShowAlert(false)
    }

    const toggleSelected = (id) =>
        setSelectedIds((m) => ({ ...m, [id]: !m[id] }))
    const clearSelected = () => setSelectedIds({})
    // Header select-all over the rows currently displayed (respects search,
    // filters, and the open folder).
    const toggleSelectAll = (ids) =>
        setSelectedIds((m) => {
            const all = ids.length > 0 && ids.every((id) => m[id])
            const next = { ...m }
            ids.forEach((id) => (next[id] = !all))
            return next
        })
    const selectedCount = Object.values(selectedIds).filter(Boolean).length
    const selectedList = () =>
        Object.keys(selectedIds).filter((id) => selectedIds[id])

    // Sequential on purpose: keeps server load sane and failures isolated.
    const bulkMoveSessions = async (newParentId) => {
        setCurrentForm("Loading")
        const svc = new SessionService()
        for (const id of selectedList()) {
            try {
                await svc.updateSessionFolder(id, newParentId)
            } catch (e) {
                console.error("bulk move failed for session", id, e)
            }
        }
        window.location.reload()
    }

    const bulkDeleteSessions = async () => {
        setCurrentForm("Loading")
        const svc = new SessionService()
        for (const id of selectedList()) {
            try {
                await svc.deleteSession(id)
            } catch (e) {
                console.error("bulk delete failed for session", id, e)
            }
        }
        window.location.reload()
    }

    const SESSION_SORTERS = {
        "date-desc": (a, b) => b.creation_date - a.creation_date,
        "date-asc": (a, b) => a.creation_date - b.creation_date,
        "length-desc": (a, b) => b.length - a.length,
        "length-asc": (a, b) => a.length - b.length,
        "name-asc": (a, b) => a.title.localeCompare(b.title),
        "name-desc": (a, b) => b.title.localeCompare(a.title),
        "participants-desc": (a, b) =>
            (b.participant_count || 0) - (a.participant_count || 0),
        "participants-asc": (a, b) =>
            (a.participant_count || 0) - (b.participant_count || 0),
        "pods-desc": (a, b) => (b.pod_count || 0) - (a.pod_count || 0),
        "pods-asc": (a, b) => (a.pod_count || 0) - (b.pod_count || 0),
        "type-desc": (a, b) => (b.has_video ? 1 : 0) - (a.has_video ? 1 : 0),
        "type-asc": (a, b) => (a.has_video ? 1 : 0) - (b.has_video ? 1 : 0),
    }
    const query = searchQuery.trim().toLowerCase()
    // A non-empty search spans EVERY folder (results carry a folder hint);
    // browsing stays scoped to the open folder.
    const searchingAll = query !== ""
    const baseSessions = searchingAll ? sessions : displayedSessions
    const filteredSessions = baseSessions.filter(
        (s) =>
            (videoFilter === "video"
                ? s.has_video
                : videoFilter === "audio"
                  ? !s.has_video
                  : true) &&
            (query === "" || (s.title || "").toLowerCase().includes(query)),
    )
    const sortedSessions = [...filteredSessions].sort(
        SESSION_SORTERS[sortBy] || SESSION_SORTERS["date-desc"],
    )
    // Counts across the unfiltered set so the filter labels can show totals.
    const videoCount = displayedSessions.filter((s) => s.has_video).length
    const audioCount = displayedSessions.length - videoCount

    return (
        <DiscussionSessionPage
            sortBy={sortBy}
            setSortBy={setSortBy}
            searchQuery={searchQuery}
            setSearchQuery={setSearchQuery}
            selectedIds={selectedIds}
            toggleSelected={toggleSelected}
            toggleSelectAll={toggleSelectAll}
            clearSelected={clearSelected}
            selectedCount={selectedCount}
            openBulkDialog={(form) => {
                // Same prep as the single-session Move dialog: without this
                // the folder picker rendered an empty list (Home only).
                if (form === "MoveSessions") {
                    setSelectableFolders(FolderModel.fileable(folders, props.userdata))
                    setFolderSelect(null)
                }
                setCurrentForm(form)
            }}
            bulkMoveSessions={bulkMoveSessions}
            bulkDeleteSessions={bulkDeleteSessions}
            videoFilter={videoFilter}
            setVideoFilter={setVideoFilter}
            videoCount={videoCount}
            audioCount={audioCount}
            openFolderDialog={openFolderDialog}
            navigateToHomescreen={navigateToHomescreen}
            me={props.userdata}
            isLoading={isLoading}
            sessions={sessions}
            folders={folders}
            breadcrumbs={breadcrumbs}
            goBackToPrevious={goBackToPrevious}
            displayFolder={displayFolder}
            displayedFolders={displayedFolders}
            displayedSessions={sortedSessions}
            searchingAll={searchingAll}
            renameSessionInline={renameSessionInline}
            goToSession={goToSession}
            openSessionDialog={openSessionDialog}
            endSession={endSession}
            alertMessage={alertMessage}
            showAlert={showAlert}
            closeAlert={closeAlert}
            currentForm={currentForm}
            closeDialog={closeDialog}
            selectedSession={selectedSession}
            changeSessionName={changeSessionName}
            changeFolderName={changeFolderName}
            deleteSession={deleteSession}
            deleteFolder={deleteFolder}
            addFolder={addFolder}
            newRecording={newRecording}
            moveFolder={moveFolder}
            moveSession={moveSession}
            selectedFolder={selectedFolder}
            selectableFolders={selectableFolders}
            folderSelect={folderSelect}
            setFolderSelect={setFolderSelect}
            invalidName={invalidName}
        />
    )
}

export { SessionsComponent }
