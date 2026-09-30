import { useEffect, useState } from 'react';
import { formatHMS as formatSeconds, startPolling, mergeById, maxId } from "../globals";
import { SessionService } from '../services/session-service';
import {TranscriptComponentPage} from './html-pages'
import { decorateTranscripts } from './transcript-utils'

// 2 s keeps the pod's transcript list feeling live; with after_id most
// responses are empty, so the cost is one tiny request per tick.
const POLL_MS = 2000
const FULL_REFRESH_EVERY = 15 // 30 s at POLL_MS

function TranscriptsComponentClient(props){
  const [transcripts, setTransripts] = useState([]);
  const [dialogKeywords, setDialogKeywords] = useState();
  const [currentForm, setCurrentForm] = useState("");
  const [displayTranscripts, setDisplayTranscripts] = useState([]);
  const [showKeywords] = useState(true);
  const [showDoA] = useState(false);
  const sessionService = new SessionService()
  
  
  // Live poll (audit G.2): chained so requests never overlap, incremental
  // (after_id, merged by id) with a full re-fetch every FULL_REFRESH_EVERY
  // polls so rows edited after insertion are picked up too, paused while
  // the tab is hidden, backed off on errors, aborted on unmount.
  useEffect(() => {
    if (props.sessionDevice === null) return undefined
    const deviceId = props.sessionDevice.id
    let rows = []
    let lastId = 0
    let polls = 0
    const stop = startPolling(async (signal) => {
        const full = polls++ % FULL_REFRESH_EVERY === 0
        const response = await sessionService.getSessionDeviceTranscriptsForClient(
            deviceId,
            0,
            { signal, afterId: full ? 0 : lastId },
        )
        if (response.status !== 200) {
            console.error("transcripts-component-client: poll failed", response.status)
            return false
        }
        const data = await response.json()
        rows = full ? data : mergeById(rows, data)
        lastId = maxId(data, full ? 0 : lastId)
        setTransripts(rows)
        return true
    }, POLL_MS)
    return stop
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.sessionDevice])



  /*
  useEffect(()=>{
    if(transcripts.length > 0){
      createDisplayTranscripts();
    }
  },[transcripts.length]) 
  */
  
  // Rebuild the decorated rows whenever a poll delivers transcripts. (The
  // old reload flag was set false-then-true inside one awaited fetch, which
  // React 18 batches into "no change" — the display list froze after the
  // first successful fetch.)
  useEffect(()=>{
    createDisplayTranscripts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  },[transcripts])


const createDisplayTranscripts = ()=> {
    setDisplayTranscripts(decorateTranscripts(transcripts, showKeywords, showDoA, angleToColor))
  }

  const angleToColor = (angle)=> {
    if (angle === -1) {
      return 'hsl(0, 100%, 100%)';
    } else {
      return 'hsl(' + angle + ', 100%, 95%)';
    }
  }

  const openKeywordDialog = (dialogKeywords) =>{
    setDialogKeywords(dialogKeywords);
    setCurrentForm("Keyword");
  }

  const openOptionsDialog = ()=> {
    setCurrentForm("");
  }

  const closeDialog = ()=> {
    setCurrentForm("");
  }


  const navigateToSession = ()=> {
    props.setParentCurrentForm("")
  }

  return(
    <TranscriptComponentPage
      sessionDevice = {props.sessionDevice}
      currentForm = {currentForm}
      navigateToSession = {navigateToSession}
      displayTranscripts = { displayTranscripts}
      formatSeconds = {formatSeconds}
      openKeywordDialog = {openKeywordDialog}
      closeDialog = {closeDialog}
      dialogKeywords = {dialogKeywords}
      showDoA = {showDoA}
      transcriptIndex = {props.transcriptIndex}
      createDisplayTranscripts = {createDisplayTranscripts}
      openOptionsDialog = {openOptionsDialog}
    />
  )
}

export {TranscriptsComponentClient}
