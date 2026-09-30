import { DeviceService } from '../services/device-service';
import { SessionService } from '../services/session-service';
import { DeviceModel } from '../models/device'
import { SessionModel } from '../models/session'
import { FolderModel } from '../models/folder';
import { useEffect, useState } from 'react';
import {CreateSessionPage} from './html-pages'
import { useNavigate, useSearchParams, useLocation } from 'react-router-dom';


// Date-stamped default name, e.g. "Session Aug 7 14:05" (chars limited to
// the backend's allowed set — letters/digits/space/colon).
function defaultSessionName() {
  const now = new Date();
  return (
    "Session " +
    now.toLocaleString("en-US", { month: "short", day: "numeric" }) +
    " " +
    now.toTimeString().slice(0, 5)
  ).replace(",", "");
}

function CreateSessionComponent(props) {
  const BLINK_DELAY = 15000;
  //Previous state
  const location = useLocation();
  const prevState = location.state;
  const changedState = prevState != null;
  // Page Data
  const [, setUser] = useState(null);
  const [devices, setDevices] = useState([]);

  //menus = Menus;
  const [currentMenu, setCurrentMenu] = useState(changedState ? prevState.currentMenu : 'Settings');
  const [pageTitle, setPageTitle] = useState(changedState ? prevState.pageTitle : 'New session');
  //forms = Forms;
  const [currentForm, setCurrentForm] = useState("");
  const [displayText, setDisplayText] = useState('');

  // Session Data
  // Pre-filled with the date-stamped default so the field is never blank;
  // still editable, and createSession keeps its own fallback for safety.
  const [sessionName, setSessionName] = useState(changedState ? prevState.sessionName : defaultSessionName());
  // Participant devices are always allowed: the passcode this generates is
  // the only way pods can join at all (dedicated hardware pods are gone),
  // so the old "Allow participant devices" toggle was removed from the UI.
  const byod = true;
  const [doa] = useState(changedState ? prevState.doa : true);
  // Always on: live per-utterance feature scoring is nearly free, and the
  // toggle only ever produced sessions whose E&T charts sat empty until a
  // posthoc re-run. The UI option was removed alongside the byod one.
  const features = true;
  // Live transcription engine, locked at creation (crisperwhisper = local GPU).
  // Legacy 'whisper' sessions fold into crisperwhisper (plain Whisper retired).
  const [asr, setAsr] = useState(() => {
    const prev = changedState ? prevState.asr : null;
    const chosen = prev || 'crisperwhisper';
    return chosen === 'whisper' ? 'crisperwhisper' : chosen;
  });
  const [selectedKeywordList] = useState(changedState ? prevState.selectedKeywordList : null);
  const [selectedTopicModel] = useState(changedState ? prevState.selectedTopicModel : null);
  const [selectedDevices, setSelectedDevices] = useState([]);
  const [folder, setFolder] = useState(changedState ? prevState.folder : -1);
  const [folderPath, setFolderPath] = useState(changedState ? prevState.folderPath : 'Home');
  const [folders, setFolders] = useState([]);
  const [folderSelect, setFolderSelect] = useState(null)
  const [breadCrumbSelect, setBreadCrumbSelect] = useState(null)
  const navigate = useNavigate();
  const [searchParam] = useSearchParams()
  
  

  useEffect(() => {
    setUser(props.userdata);
    const fetchData = new DeviceService().getDevices(false, true, false, true)
    fetchData.then(
      response => {
        if (Number(response) === 200) {
          const resp2 = response.json()
          resp2.then(
            respdevices => {
              const deviceresult = DeviceModel.fromJsonList(respdevices)
              setDevices(deviceresult);
              setSelectedDevices([]);
              for (const device of deviceresult) {
                // Add custom view fields to pods.
                device['selected'] = false;
                device['blinking'] = false;
              }
            }
          )
        }
      },
      apierror => {
        console.error("create-session-components func: useEffect 1 ", apierror)
      }
    )

    const fetchData3 = new SessionService().getFolders()
    fetchData3.then(
      response => {
        if (response.status === 200) {
          const resp = response.json()
          resp.then(
            folderss => {
              const folderresult = FolderModel.fileable(FolderModel.fromJsonList(folderss), props.userdata)
              setFolders(folderresult);
              const paramfolder = searchParam.get('folder');
              const passedFolderId = +parseInt(paramfolder, 10);
              if (folder > 0) {
              } else if (passedFolderId > -1) {
                setFolderLocation(passedFolderId, buildBreadcrumbs(passedFolderId));
              } else {
                setFolderLocation(passedFolderId, folderPath);
              }

            })
        }
      },
      apierror => {
        console.error("create-session-components func: useEffect 3 ", apierror)
      }
    )
    
    if (!changedState) {
      goToSettings();
    }

    return () => {
      const blinkingDevices = devices.filter(p => p.blinking === true);
      for (const device of blinkingDevices) {
        new DeviceService().blinkPod(device.id, 'stop');//.subscribe(() => { });
      }
    }
  }, [])



  const setFolderLocation = (folderId, pathString)=>{
    setFolder(folderId);
    setFolderPath(pathString);
    closeDialog();
  }
  
  const buildBreadcrumbs = (folderId)=> {
    const breadcrumbs = [];
    let folderB = folders.find(f => f.id === folderId);
    if (folderB != null) {
      breadcrumbs.unshift(folderB);
      while (folderB.parent) {
        folderB = folders.find(f => f.id === folderB.parent);
        breadcrumbs.unshift(folderB);
      }
    }
    if (breadcrumbs.length > 0) {
      const crumbnames = breadcrumbs.map(b => b.name);
      crumbnames.unshift('Home');
      return crumbnames.join('/');
    }
  }

  const createSession = ()=> {
    {
      // Name is pre-filled with the date-stamped default; if the user
      // clears it, fall back to a fresh one rather than sending blank.
      const trimmed = (sessionName || "").trim()
      const finalName = trimmed !== "" ? trimmed : defaultSessionName()
      const deviceIds = selectedDevices.map(d => d.id);
      const keywordListId = (selectedKeywordList) ? selectedKeywordList.id : null;
      const topicModelId = (selectedTopicModel) ? selectedTopicModel.id : null;
      const fetchData = new SessionService().createNewSession(finalName, deviceIds, keywordListId, topicModelId, byod, features, doa, folder, asr)
      fetchData.then(
        response=>{
          if(response.status === 200){
            const resp = response.json();
            resp.then(
              result =>{
                const s = SessionModel.fromJson(result)
                navigate("/sessions/" + s.id);
                //  this.router.navigate(['sessions', s.id, 'overview']);
              }
            )
          }else{
            // response.json() is a Promise — indexing it directly showed an
            // empty dialog for every server-side rejection.
            response.json().then(
              (j) => openDialog("Error", (j && j.message) || "The session could not be created."),
              () => openDialog("Error", "The session could not be created."),
            );
            goToSettings();
          }
        },
        apierror=>{
          console.error("create-session-components func: createSession 1 ", apierror)
        }
      )  
    }
  }

  // ---------------------
  // Keyword Menu
  // ---------------------

  // ---------------------
  // Device Menu
  // ---------------------

  const deviceSelected = (device)=> {
    if (!selectedDevices.includes(device)) {
      selectedDevices.push(device);
    } else {
      setSelectedDevices(selectedDevices.filter(d => d !== device));
    }
  }

  const onClickSelectAll = ()=> {
    if (selectedDevices.length === devices.length) {
      setSelectedDevices([]);
    } else {
      setSelectedDevices(devices);
    }
  }

  const blinkPod = (e, device)=> {
    e.stopPropagation();
    device.blinking = !device.blinking;
    if (device.blinking) {
      const fetchData = new DeviceService().blinkPod(device.id, 'start')
      fetchData.then(
        response => {
          if(response.status === 200){
            device.timeout = setTimeout(() => {
              device.timeoutId = null;
              device.blinking = false;
            }, BLINK_DELAY);
          }
        },
        apierror =>{
          console.error("create-session-components func: blinkPad 1 ", apierror)
        }
      )
    } else {
      const fetchData2 = new DeviceService().blinkPod(device.id, 'stop')
      fetchData2.then(
        response => {
          if(response.status === 200){
            if (device.timeoutId !== null) {
              clearTimeout(device.timeoutId);
              device.timeoutId = null;
            }
          }
        },
        apierror => {
          console.error("create-session-components func: blinkPad 2 ", apierror)
        }
      )
    }
  }

  // ---------------------
  // Navigation and Dialog
  // ---------------------

  
  const goToSettings = ()=> {
    setCurrentMenu("Settings");
    setPageTitle('New session');
  }

  const goToDevices = () =>{
    setCurrentMenu("Devices");
    setPageTitle('New session');
  }

  const navigateToSessions = ()=> {
    navigate('/sessions');
  }
  
  
  
  const openDialog = (form, text)=> {
    setCurrentForm(form);
    setDisplayText(text);
  }

  const closeDialog = ()=> {
    setCurrentForm("");
  }

  return(
    <CreateSessionPage 
    pageTitle = {pageTitle}
    navigateToSessions = {navigateToSessions}
    currentMenu = {currentMenu}
    sessionName = {sessionName}
    setSessionName = {setSessionName}
    folderPath = {folderPath}
    asr = {asr}
    setAsr = {setAsr}
    goToSettings = {goToSettings}
    goToDevices = {goToDevices}
    devices = {devices}
    deviceSelected = {deviceSelected}
    selectedDevices = {selectedDevices}
    blinkPod = {blinkPod}
    onClickSelectAll = {onClickSelectAll}
    createSession = {createSession}
    closeDialog = {closeDialog}
    setFolderLocation = {setFolderLocation}
    displayText = {displayText}
    folders = {folders}
    currentForm = {currentForm}
    openDialog = {openDialog}
    folderSelect = {folderSelect}
    setFolderSelect = {setFolderSelect}
    breadCrumbSelect = {breadCrumbSelect}
    setBreadCrumbSelect = {setBreadCrumbSelect}
    />
  )
}

export {CreateSessionComponent}
