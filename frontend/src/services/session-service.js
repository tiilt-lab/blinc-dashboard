import { ApiService } from "./api-service";
//import 'rxjs/add/operator/map';

class SessionService {
  deviceIds = [];
  keywordListId = "";
  api = new ApiService();

  // GET for the chained polls: takes an AbortSignal so an unmounted view
  // can cancel its in-flight request. The shared fetch wrapper has no
  // signal option yet, so with a signal this mirrors its options directly.
  _get(path, headers = {}, opts = {}) {
    if (!opts.signal) {
      return this.api.httpRequestCallWithHeader(path, "GET", {}, headers);
    }
    return fetch(this.api.getEndpoint() + path, {
      method: "GET",
      mode: "cors",
      credentials: "include",
      cache: "no-store",
      headers: this.api._generateHeaders(headers, undefined),
      redirect: "follow",
      signal: opts.signal,
    });
  }

  // `after_id` narrows a poll to rows newer than the last one seen; 0 (or
  // absent) keeps the full-history response.
  _afterId(path, afterId) {
    return afterId > 0 ? `${path}?after_id=${afterId}` : path;
  }

  endSession(sessionId) {
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/stop`,
      "POST",
      {}
    );
  }

  getSessions(opts = {}) {
    return this._get("api/v1/sessions", {}, opts);
  }

  getSession(sessionId) {
    return this.api.httpRequestCall(`api/v1/sessions/${sessionId}`, "GET", {});
  }

  markPosthocCompleted(sessionId, sessionDeviceId, models = null) {
    // `models` is the per-run model choice set (asr, embedder, diarizer,
    // scorer, emotion, attention, ...) for provenance; optional.
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/device/${sessionDeviceId}/posthoc_completed`,
      "POST",
      models ? { models } : {},
    );
  }

  getSessionByPasscode(passcode) {
    return this.api.httpRequestCall(`api/v1/sessions/student/passcode/${passcode}`, "GET", {});
  }

  getSessionById(sessionId) {
    return this.api.httpRequestCall(`api/v1/sessions/student/sessionid/${sessionId}`, "GET", {});
  }

 
  getSessionsByAlias(alias) {
    return this.api.httpRequestCall(`api/v1/sessions/student/alias/${alias}`, "GET", {});
  }

 getSessionsDeviceByAlias(sessionid, alias) {
    return this.api.httpRequestCall(`api/v1/sessions/sessionid/${sessionid}/student/alias/${alias}`, "GET", {});
  }

  deleteSession(sessionId) {
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}`,
      "DELETE",
      {}
    );
  }

  updateSession(sessionId, name) {
    const body = {
      name: name,
    };
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}`,
      "PUT",
      body
    );
  }

  renamePod(sessionId, sessionDeviceId, name) {
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/devices/${sessionDeviceId}/name`,
      "PUT",
      { name }
    );
  }

  updateSessionAnalysisConfig(sessionId, keywordListId, topicModelId) {
    return this.api.httpRequestCall(`api/v1/sessions/${sessionId}`, "PUT", {
      keywordListId: keywordListId,
      topicModelId: topicModelId,
    });
  }

  updateSessionFolder(sessionId, folder) {
    const body = {
      folder: folder,
    };
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}`,
      "PUT",
      body
    );
  }

  getSessionDevice(sessionId, sessionDeviceId) {
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/devices/${sessionDeviceId}`,
      "GET",
      {}
    );
  }

  getSessionDeviceForClient(session_device_id){
    return this.api.httpRequestCall(
      `/api/v1/devices/${session_device_id}/session_device`,
      "GET",
      {}
    );
  }
  stopPosthocQueue(sessionId) {
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/posthoc_queue/stop`,
      "POST",
      {},
    );
  }
  getGlobalPosthocQueue() {
    return this.api.httpRequestCall(`api/v1/posthoc_queue`, "GET", {});
  }
  getPosthocQueue(sessionId, opts = {}) {
    return this._get(`api/v1/sessions/${sessionId}/posthoc_queue`, {}, opts);
  }
  // Live per-pod alert flags (silent / dominated / hanging question).
  getSessionTriage(sessionId, opts = {}) {
    return this._get(`api/v1/sessions/${sessionId}/triage`, {}, opts);
  }
  enqueuePosthoc(sessionId, deviceIds) {
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/posthoc_queue`,
      "POST",
      { device_ids: deviceIds },
    );
  }
  getSessionDevices(sessionId, opts = {}) {
    return this._get(`api/v1/sessions/${sessionId}/devices`, {}, opts);
  }

  getSessionDeviceTranscripts(sessionId, sessionDeviceId, startTime = 0) {
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/devices/${sessionDeviceId}/transcripts`,
      "GET",
      {}
    );
  }

  getSessionDeviceSpeakers(sessionId, sessionDeviceId) {
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/devices/${sessionDeviceId}/speakers`,
      "GET",
      {}
    );
  }

  getSessionSpeakers(sessionId) {
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/speakers`,
      "GET",
      {}
    );
  }

  // The pod's processing key, handed to the BYOD client in its join response,
  // is what proves a caller belongs to this pod. Logged-in users are covered by
  // their session cookie instead, so the header is only sent when we have one.
  _clientKeyHeader(processingKey) {
    return processingKey ? { "X-Processing-Key": processingKey } : {};
  }

  // The three live polls below take { afterId, signal }: afterId asks only
  // for rows newer than that id (the caller merges by id), signal aborts.
  getSessionDeviceTranscriptSpeakerMetricsForClient(sessionDeviceId, startTime = 0, processingKey = null, opts = {}) {
    return this._get(
      this._afterId(`api/v1/devices/${sessionDeviceId}/transcriptspeakermetrics/client`, opts.afterId),
      this._clientKeyHeader(processingKey),
      opts
    );
  }

  getSessionDeviceTranscriptsForClient(sessionDeviceId, startTime = 0, opts = {}) {
    return this._get(
      this._afterId(`api/v1/devices/${sessionDeviceId}/transcripts/client`, opts.afterId),
      {},
      opts
    );
  }

  // Heart-rate/RR batch from the join page's Polar straps (Web Bluetooth).
  postHeartRateForClient(sessionDeviceId, samples, processingKey) {
    return this.api.httpRequestCallWithHeader(
      `api/v1/devices/${sessionDeviceId}/heartrate/client`,
      "POST",
      { client_now: Date.now(), samples },
      this._clientKeyHeader(processingKey)
    );
  }

  getPodHeartRate(sessionId, sessionDeviceId) {
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/device/${sessionDeviceId}/heartrate`,
      "GET",
      {}
    );
  }

  getSessionDeviceVideoMetricsForClient(sessionDeviceId, startTime = 0, processingKey = null, opts = {}) {
    return this._get(
      this._afterId(`api/v1/devices/${sessionDeviceId}/videometrics/client`, opts.afterId),
      this._clientKeyHeader(processingKey),
      opts
    );
  }

  // Student-dashboard polls: abortable, but these routes have no after_id
  // (the caller replaces the list).
  getSessionTranscriptsForClient(sessionId, alias, startTime = 0, opts = {}) {
    return this._get(`api/v1/session/${sessionId}/transcripts/student/${alias}`, {}, opts);
  }

  getSessionVideoMetricsForClient(sessionId, alias, startTime = 0, opts = {}) {
    return this._get(`api/v1/session/${sessionId}/videometrics/student/${alias}`, {}, opts);
  }

  getSessionDeviceTranscriptsByAlias(sessionId, deviceId, alias,startTime = 0) {
    return this.api.httpRequestCall(
      `api/v1/session/${sessionId}/sessiondevice/${deviceId}/transcripts/student/${alias}`,
      "GET",
      {}
    );
  }

  getSessionDeviceVideoMetricsByAlias(sessionId,deviceId, alias,startTime = 0) {
    return this.api.httpRequestCall(
      `api/v1/session/${sessionId}/sessiondevice/${deviceId}/videometrics/student/${alias}`,
      "GET",
      {}
    );
  }

  getRaterDetailByExpertId(expertId) {
    return this.api.httpRequestCall("api/v1/student/raters/" + expertId, 'GET', {});
  }

  postRating(payload){
    const body =  payload;
    return this.api.httpRequestCall(
      `api/v1/student/postrating`,
      "POST",
      body
    );
  }

  postSurveyResponse(payload){
    const body =  payload;
    return this.api.httpRequestCall(
      `api/v1/student/postsurveyresponse`,
      "POST",
      body
    );
  }

  setDeviceButton(sessionDeviceId, pressed, key) {
    const body = {
      id: sessionDeviceId,
      activated: pressed,
    };
    const headers = {
      "X-Processing-Key": key,
    };
    return this.api.httpRequestCallWithHeader(
      `api/v1/help_button`,
      "POST",
      body,
      headers
    );
  }

  createNewSession(
    name,
    devices,
    keywordListId,
    topicModelId,
    byod,
    features,
    doa,
    folder,
    asr
  ) {
    const body = {
      name: name,
      devices: devices,
      keywordListId: keywordListId,
      topicModelId: topicModelId,
      byod: byod,
      features: features,
      doa: doa,
      folder: folder,
      // live transcription engine, locked at creation
      asr: asr || null,
    };

    return this.api.httpRequestCall("api/v1/sessions", "POST", body);
  }

  joinByodSession(name, passcode, collaborators) {
    const body = {
      name: name,
      passcode: passcode,
      collaborators: collaborators,
    };
    return this.api.httpRequestCall("api/v1/sessions/byod", "POST", body);
  }

  addSpeaker(sessionDeviceId) {
    return this.api.httpRequestCall(
      `api/v1/devices/${sessionDeviceId}/speakers`,
      "POST",
      {}
    );
  }

  removeSpeaker(sessionDeviceId, speakerId) {
    return this.api.httpRequestCall(
      `api/v1/devices/${sessionDeviceId}/speakers/${speakerId}`,
      "DELETE"
    );
  }

  updateCollaborator(speakerId, alias) {
    const body = {
      alias: alias,
    };
    return this.api.httpRequestCall(`api/speakers/${speakerId}`, "POST", body);
  }

  addPodToSession(sessionId, podId) {
    const body = {
      sessionId: sessionId,
      podId: podId,
    };
    return this.api.httpRequestCall("api/v1/sessions/pod", "POST", body);
  }

  setPasscodeStatus(sessionId, state) {
    const body = {
      state: state,
    };
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/passcode`,
      "POST",
      body
    );
  }

  removeDeviceFromSession(sessionId, sessionDeviceId, shouldDelete = false) {
    // delete must ride the query string: the fetch wrapper drops bodies on
    // DELETE, so passing it as data silently never deleted anything.
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/devices/${sessionDeviceId}?delete=${shouldDelete ? "true" : "false"}`,
      "DELETE",
      {}
    );
  }

  downloadSessionTranscriptMetrics(sessionId, fileName,windowsize,format) {
    if(windowsize === ""){
      windowsize = 0;
    }else{
      windowsize = parseInt(windowsize);
    }

    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/exporttranscriptmetrics/${windowsize}/${format}`,
      "GET",
      {}
    );
  }

  downloadSessionVideoMetrics(sessionId, fileName,windowsize,format) {
    if(windowsize === ""){
      windowsize = 0;
    }else{
      windowsize = parseInt(windowsize);
    }
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/exportvideometrics/${windowsize}/${format}`,
      "GET",
      {}
    );
  }

  downloadSessionTranscriptVideoMetrics(sessionId, fileName,windowsize,format) {
    if(windowsize === ""){
      windowsize = 0;
    }else{
      windowsize = parseInt(windowsize);
    }
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/exporttranscriptvideometrics/${windowsize}/${format}`,
      "GET",
      {}
    );
  }

  getSynthesizedFeedbackMetrics(sessionId, sessionDeviceId) {
    return this.api.httpRequestCall(
      `api/v1/sessions/${sessionId}/device/${sessionDeviceId}/synthesized_feedback_metrics`,
      "GET",
      {}
    );
  }

  getLLMFeedbackBasedOnMetrics(metricData) {
    return this.api.httpRequestCall(
      `api/v1/llmqueries/generate_llm_feedback_based_on_metrics`,
      "POST",
      metricData
    );
  }

  getLLMPromptResponse(metricData) {
    return this.api.httpRequestCall(
      `api/v1/llmqueries/fetch_response_for_question`,
      "POST",
      metricData
    );
  }

  get_llm_question_answer_interactions(sessionId, sessionDeviceId,username) {
    return this.api.httpRequestCall(
      `api/v1/llminteractiveprompting/sessionid/${sessionId}/device/${sessionDeviceId}/username/${username}`,
      "GET",
      {}
    );
  } 

  getFolders() {
    return this.api.httpRequestCall(`api/folders`, "GET", {});
  }

  addFolder(name, parent) {
    const body = {
      name: name,
      parent: parent,
    };
    return this.api.httpRequestCall(`api/folder`, "POST", body);
  }

  updateFolder(folderId, parent, name) {
    const body = {
      parent: parent,
      name: name,
    };
    return this.api.httpRequestCall(`api/folders/${folderId}`, "POST", body);
  }

  deleteFolder(folderId) {
    return this.api.httpRequestCall(`api/folders/${folderId}`, "DELETE", {});
  }

  // Folder sharing: who has access (direct and inherited), add/change a person
  // by email at viewer / editor / manager, and remove one (or leave).
  getFolderMembers(folderId) {
    return this.api.httpRequestCall(`api/folders/${folderId}/members`, "GET", {});
  }

  setFolderMember(folderId, email, level) {
    return this.api.httpRequestCall(`api/folders/${folderId}/members`, "PUT", { email, level });
  }

  removeFolderMember(folderId, userId) {
    return this.api.httpRequestCall(`api/folders/${folderId}/members/${userId}`, "DELETE", {});
  }
}

export { SessionService };
