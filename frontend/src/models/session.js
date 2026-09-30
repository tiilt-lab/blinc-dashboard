import { stringToDate, formatSeconds } from '../globals';

export class SessionModel {
  // Server Fields
  id;
  name;
  creation_date;
  end_date;
  passcode;
  keywords;
  folder;
  has_video;
  has_posthoc;
  pod_count;
  participant_count;
  // The owner's email (sent for any session that is not the caller's own —
  // an admin's view of everyone's, or one shared through a folder), whether
  // it is theirs, and whether they may rename, move, delete or stop it.
  owner;
  owned;
  can_modify;

  // Client Fields
  local_start_date;

  // Format a duration in seconds as H:MM:SS / M:SS (used for pod durations).
  // Delegates to the one canonical globals.formatSeconds (unpadded leading
  // unit, "—" for missing) instead of a private copy of the same math.
  static formatDuration(seconds) {
    return formatSeconds(seconds, { padLeading: false, invalid: '—' });
  }

  get recording(){
    return (this.end_date == null);
  }

  get length(){
    if (this.end_date != null) {
      return Math.floor((this.end_date.getTime() - this.creation_date.getTime()) / 1000);
    } else {
      return Math.floor((Date.now() - this.local_start_date.getTime()) / 1000);
    }
  }

  get title() {
    return (this.name == null) ? 'Session' : this.name;
  }

  get lengthFormatted() {
    // Always HH:MM:SS (hour zero-padded, shown even when 0), via the shared
    // formatter. formatSeconds floors seconds/3600 so this stays correct past
    // 24h (unlike the Date-based formatHMS, which would wrap).
    return formatSeconds(this.length, { alwaysHours: true });
  }

  static fromJson(json){
    const model = new SessionModel();
    model.id = json['id'];
    model.name = json['name'];
    model.creation_date = stringToDate(json['creation_date']);
    if (json['end_date'] != null) {
      model.end_date = stringToDate(json['end_date']);
    }
    model.local_start_date = new Date();
    model.local_start_date.setSeconds(model.local_start_date.getSeconds() - json['length']);
    model.passcode = json['passcode'];
    model.keywords = json['keywords'];
    model.folder = json['folder']
    model.has_video = json['has_video'] === true
    model.has_posthoc = json['has_posthoc'] === true
    model.pod_count = json['pod_count'] != null ? json['pod_count'] : null
    model.participant_count = json['participant_count'] != null ? json['participant_count'] : null
    model.analysis_running = json['analysis_running'] === true
    model.owner = json['owner'] != null ? json['owner'] : null
    model.owned = json['owned'] !== false
    // Rename / move / delete allowed: owner, super, or an editor of its folder.
    model.can_modify = json['can_modify'] != null ? json['can_modify'] : model.owned
    return model;
  }

  // Converts JSON to SessionModel[]
  static fromJsonList(jsonArray){
    const sessions = [];
    for (const el of jsonArray) {
      sessions.push(SessionModel.fromJson(el));
    }
    return sessions;
  }
}
