"""Session data export: the pods overview's Download dialog.

One ZIP of CSV files per request (build_zip), one file per dataset ticked in
the dialog, plus a README.txt that names the session, the time base and each
file's columns. Every dataset is an entry in DATASETS, so a new data source
(a wearable, a coding scheme) is one more entry here and one more checkbox in
frontend/src/components/session-export/session-export.js.

The three older single-file exports (transcript, video and transcript+video
metrics, optionally windowed) live here too, as writer functions shared by
their original routes in routes/session.py and the ZIP.

Times are seconds since the session was created (session.creation_date, UTC),
the clock every metric table is stamped on.

Recording files are found by routes/session.py; build_zip takes them through
a small ``media`` object (recording_files, segments) so this module needs no
Flask and never starts a video conversion.
"""
import csv
import io
import re
import tempfile
import zipfile
from datetime import datetime, timezone

import database
from tables.event_marker import EventMarker
from metrics_windowing import fmt_start_time
from utility import batch_video_metrics, batch_transcript_metrics, batch_transcript_video_metrics

# Gaze targets that mean "looking at nothing in particular" (the video
# pipeline's "Nothing"); same set as analytics.compute_joint_attention.
_IDLE_TARGETS = {None, '', 'Nothing'}

# Largest window the dialog may ask for, in seconds.
MAX_WINDOW_S = 3600


# ---------------------------------------------------------------------------
# The single-file metric exports (also datasets in the ZIP). Each writes its
# CSV rows into ``si`` and returns the JSON payload for format="json".
# ---------------------------------------------------------------------------

def write_transcript_metrics(si, session_id, windowsize, format):
    field_names = None  
    fwrite = None 
    session_devices = database.get_session_devices(session_id=session_id)
    all_participants_metrics = {}
    if windowsize == 0:
        field_names = ['Device ID', 'Device Name', 'Start Time', 'Transcript Id','Transcript', 'Keywords', 'Keywords Detected', 'Similarity', 'Analytic Thinking', 'Authenticity', 'Certainty',
                    'Clout', 'Emotional Tone', 'Direction',  'participation_score', 'internal_cohesion', 'responsivity', 'social_impact','newness','communication_density','Word Count', 'Speaker Tag', 'Speaker ID', 'Topic ID']
        fwrite = csv.DictWriter(si, fieldnames = field_names)
        fwrite.writeheader()
        for session_device in session_devices:
            transcripts = database.get_all_transcript_metrics_by_session(session_device_id=session_device.id)
            keywords = database.get_keyword_usages(session_device_id=session_device.id)
            # Index once: the per-transcript list scan was O(transcripts x
            # keyword_usages) per export.
            keywords_by_transcript = {}
            for keyword in keywords:
                keywords_by_transcript.setdefault(keyword.transcript_id, []).append(keyword)
            for t, sm in transcripts:
                transcript_keywords = keywords_by_transcript.get(t.id, [])
                fwrite.writerow({'Device ID':session_device.id,
                    'Device Name':session_device.name,
                    'Start Time': fmt_start_time(t.start_time),
                    'Transcript Id': int(t.id),
                    'Transcript':t.transcript,
                    'Keywords': ', '.join([keyword.keyword for keyword in transcript_keywords]),
                    'Keywords Detected': ', '.join([keyword.word for keyword in transcript_keywords]),
                    'Similarity': ', '.join([str(round((1-keyword.similarity)*100,3)) for keyword in transcript_keywords]),
                    'Analytic Thinking': int(t.analytic_thinking_value),
                    'Authenticity': int(t.authenticity_value),
                    'Certainty': int(t.certainty_value),
                    'Clout': int(t.clout_value),
                    'Emotional Tone': int(t.emotional_tone_value),
                    'participation_score': int(float(sm.participation_score)*100),
                    'internal_cohesion':  int(float(sm.internal_cohesion)*100),
                    'responsivity':  int(float(sm.responsivity)*100),
                    'social_impact':  int(float(sm.social_impact)*100),
                    'newness':  int(float(sm.newness)*100), 
                    'communication_density':  int(float(sm.communication_density)*100),
                    'Word Count': int(t.word_count),
                    'Speaker Tag': t.speaker_tag,
                    'Speaker ID': int(t.speaker_id),
                    'Topic ID': int(t.topic_id)
                    })
    else:
        field_names = ['Group ID', 'Group Name', 'Time Range (s)','Transcript', 'Keywords', 'Keywords Detected', 'Similarity', 'Analytic Thinking', 'Authenticity', 'Certainty',
                    'Clout', 'Emotional Tone', 'Direction',  'participation_score', 'internal_cohesion', 'responsivity', 'social_impact','newness','Word Count', 'Speaker Tag', 'Speaker ID', 'Topic ID']
        fwrite = csv.DictWriter(si, fieldnames = field_names)
        fwrite.writeheader()     
        for session_device in session_devices:
            keywords = database.get_keyword_usages(session_device_id=session_device.id)
            speakers = database.get_speakers(session_device_id=session_device.id)
            for speaker in speakers:
                transcriptSpeakerMetric = database.get_all_transcript_metrics_by_session(session_device_id=session_device.id,speaker_id=speaker.id)
                speaker_data = batch_transcript_metrics(transcriptSpeakerMetric,windowsize,fwrite,speaker,session_device,keywords,format)
                if format == 'json':
                    all_participants_metrics[speaker.alias] = [dict(zip(field_names, row)) for row in speaker_data]
                    # all_participants_metrics.extend(speaker_data)
                        

    return all_participants_metrics


def write_video_metrics(si, session_id, windowsize, format):
    field_names = None
    fwrite = None  
    session_devices = database.get_session_devices(session_id=session_id)
    all_participants_metrics = {}
    if windowsize == 0:
        field_names = ['Device ID', 'Device Name', 'Start Time', 'Facial Emotion', 'Object Focus On', 'Attention Level', 'Speaker Tag']
        fwrite = csv.DictWriter(si, fieldnames = field_names)
        fwrite.writeheader()  
        for session_device in session_devices:
            videoMetrics = database.get_video_metrics_by_session(session_device_id=session_device.id)
            for v in videoMetrics:
                fwrite.writerow({'Device ID':session_device.id,
                    'Device Name':session_device.name,
                    'Start Time': fmt_start_time(v.time_stamp),
                    'Facial Emotion': str(v.facial_emotion),
                    'Object Focus On': str(v.object_on_focus),
                    'Attention Level': int(v.attention_level),
                    'Speaker Tag': v.student_username
                    })

    else:
        field_names = ['Group ID', 'Group Name', 'Time Range (s)', 'Facial Emotion', 'Object Focus On', 'Attention Level','Attention Rate',"Attention Class", 'Speaker Tag']
        fwrite = csv.DictWriter(si, fieldnames = field_names)
        fwrite.writeheader()
        for session_device in session_devices:
            speakers = database.get_speakers(session_device_id=session_device.id)
            for speaker in speakers:
                videoMetrics = database.get_speaker_video_metrics(session_device_id=session_device.id,student_username=speaker.alias)
                speaker_data = batch_video_metrics(videoMetrics,windowsize,fwrite,speaker,session_device,format)
                if format == 'json':
                    all_participants_metrics[speaker.alias] = [dict(zip(field_names, row)) for row in speaker_data]
                    # all_participants_metrics.extend(speaker_data)  
    return all_participants_metrics


def write_transcript_video_metrics(si, session_id, windowsize, format):
    field_names = None
    fwrite = None
    session_devices = database.get_session_devices(session_id=session_id)
    all_participants_metrics = {}
    if windowsize == 0:
        field_names = ['Device ID', 'Device Name', 'Start Time', 'Transcript', 'Keywords', 'Keywords Detected', 'Similarity', 'Analytic Thinking', 'Authenticity', 'Certainty',
                    'Clout', 'Emotional Tone', 'Direction',  'participation_score', 'internal_cohesion', 'responsivity', 'social_impact','newness','communication_density',
                    'Word Count', 'Facial Emotion', 'Object Focus On', 'Attention Level', 'Speaker Tag', 'Speaker ID', 'Topic ID']
        fwrite = csv.DictWriter(si, fieldnames = field_names)
        fwrite.writeheader()
        for session_device in session_devices:
            all_metrics = database.get_all_metrics_by_session(session_device_id=session_device.id)
            keywords = database.get_keyword_usages(session_device_id=session_device.id)
            # Index once: the per-transcript list scan was O(transcripts x
            # keyword_usages) per export.
            keywords_by_transcript = {}
            for keyword in keywords:
                keywords_by_transcript.setdefault(keyword.transcript_id, []).append(keyword)
            for t,sm,vm in all_metrics:
                transcript_keywords = keywords_by_transcript.get(t.id, [])
                fwrite.writerow({'Device ID':session_device.id,
                    'Device Name':session_device.name,
                    'Start Time': fmt_start_time(t.start_time),
                    'Transcript':t.transcript,
                    'Keywords': ', '.join([keyword.keyword for keyword in transcript_keywords]),
                    'Keywords Detected': ', '.join([keyword.word for keyword in transcript_keywords]),
                    'Similarity': ', '.join([str(round((1-keyword.similarity)*100,3)) for keyword in transcript_keywords]),
                    'Analytic Thinking': int(t.analytic_thinking_value),
                    'Authenticity': int(t.authenticity_value),
                    'Certainty': int(t.certainty_value),
                    'Clout': int(t.clout_value),
                    'Emotional Tone': int(t.emotional_tone_value),
                    'participation_score': int(float(sm.participation_score)*100),
                    'internal_cohesion':  int(float(sm.internal_cohesion)*100),
                    'responsivity':  int(float(sm.responsivity)*100),
                    'social_impact':  int(float(sm.social_impact)*100),
                    'newness':  int(float(sm.newness)*100), 
                    'communication_density':  int(float(sm.communication_density)*100),
                    'Word Count': int(t.word_count),
                    'Facial Emotion': str(vm.facial_emotion),
                    'Object Focus On': str(vm.object_on_focus),
                    'Attention Level': int(vm.attention_level),
                    'Speaker Tag': t.speaker_tag,
                    'Speaker ID': int(t.speaker_id),
                    'Topic ID': int(t.topic_id)
                    })
    else:
        field_names = ['Group ID', 'Group Name', 'Time Range (s)', 'Transcript', 'Keywords', 'Keywords Detected', 'Similarity', 'Analytic Thinking', 'Authenticity', 'Certainty',
                    'Clout', 'Emotional Tone',  'participation_score', 'internal_cohesion', 'responsivity', 'social_impact','newness',
                    'Word Count', 'Facial Emotion', 'Object Focus On', 'Attention Level','Attention Rate',"Attention Class", 'Speaker Tag', 'Speaker ID', 'Topic ID']
        fwrite = csv.DictWriter(si, fieldnames = field_names)
        fwrite.writeheader()       
        for session_device in session_devices:
            keywords = database.get_keyword_usages(session_device_id=session_device.id)
            speakers = database.get_speakers(session_device_id=session_device.id)
            for speaker in speakers:
                videoMetrics = database.get_speaker_video_metrics(session_device_id=session_device.id,student_username=speaker.alias)
                transcriptSpeakerMetric = database.get_all_transcript_metrics_by_session(session_device_id=session_device.id,speaker_id=speaker.id)
                speaker_data = batch_transcript_video_metrics(transcriptSpeakerMetric,videoMetrics,windowsize,fwrite,speaker,session_device,keywords,format)
                if format == 'json':
                    all_participants_metrics[speaker.alias] = [dict(zip(field_names, row)) for row in speaker_data]
                    # all_participants_metrics.extend(speaker_data) 
                   

    return all_participants_metrics


# ---------------------------------------------------------------------------
# ZIP datasets
# ---------------------------------------------------------------------------

class _Context:
    """What the dataset row functions share for one export: the session, its
    pods and the recording lookups, with each pod's transcripts read once."""

    def __init__(self, session, media):
        self.session = session
        self.media = media
        self.pods = sorted(database.get_session_devices(session_id=session.id), key=lambda p: p.id)
        self._transcripts = {}

    def transcripts(self, pod):
        if pod.id not in self._transcripts:
            self._transcripts[pod.id] = database.get_transcripts(session_device_id=pod.id)
        return self._transcripts[pod.id]


def _pod_rows(ctx):
    durations = database.get_pod_durations(ctx.session.id)
    speakers = database.get_pod_speaker_counts(ctx.session.id)
    for pod in ctx.pods:
        yield [pod.id, pod.name, int(bool(pod.removed)), durations.get(pod.id, ''),
               speakers.get(pod.id, 0), len(ctx.media.recording_files(pod.id))]


def _participant_rows(ctx):
    for pod in ctx.pods:
        for speaker in sorted(database.get_speakers(session_device_id=pod.id), key=lambda s: s.id):
            yield [pod.id, pod.name, speaker.id, speaker.get_alias()]


def _marker_rows(ctx):
    pods = {p.id: p for p in ctx.pods}
    for m in database.get_event_markers(session_id=ctx.session.id):
        yield m.csv_row(pods.get(m.session_device_id))


def _transcript_rows(ctx):
    for pod in ctx.pods:
        for t in ctx.transcripts(pod):
            yield [pod.id, pod.name, t.id, t.start_time, t.start_time + t.length, t.length,
                   t.speaker_tag, t.speaker_id, int(bool(t.question)), t.word_count, t.transcript]


def _keyword_rows(ctx):
    for pod in ctx.pods:
        by_id = {t.id: t for t in ctx.transcripts(pod)}
        usages = database.get_keyword_usages(session_device_id=pod.id)
        for k in sorted(usages, key=lambda k: (by_id[k.transcript_id].start_time if k.transcript_id in by_id else 0, k.id)):
            t = by_id.get(k.transcript_id)
            yield [pod.id, pod.name, k.transcript_id, t.start_time if t else '', t.speaker_tag if t else '',
                   k.keyword, k.word, round((1 - k.similarity) * 100, 3)]


def _joint_attention_rows(ctx):
    # One row per moment (a timestamp with >=2 people tracked) and shared
    # target; a moment with no shared target is one row with joint=0.
    for pod in ctx.pods:
        by_ts = {}
        for v in database.get_video_metrics_by_session(session_device_id=pod.id):
            by_ts.setdefault(v.time_stamp, {})[v.student_username] = v.object_on_focus
        for ts in sorted(by_ts):
            focuses = by_ts[ts]
            if len(focuses) < 2:
                continue
            groups = {}
            for name, focus in focuses.items():
                if focus not in _IDLE_TARGETS:
                    groups.setdefault(focus, []).append(name)
            shared = sorted((t, sorted(p)) for t, p in groups.items() if len(p) >= 2)
            if not shared:
                yield [pod.id, pod.name, ts, len(focuses), 0, '', '']
            for target, people in shared:
                yield [pod.id, pod.name, ts, len(focuses), 1, target, ';'.join(people)]


def _hr_rows(pod):
    # Insertion order breaks time_stamp ties: RR intervals are only
    # meaningful in the order the strap sent them.
    return sorted(database.get_speaker_hr_metrics(session_device_id=pod.id),
                  key=lambda r: (r.time_stamp, r.id))


def _heart_rate_rows(ctx):
    for pod in ctx.pods:
        for r in _hr_rows(pod):
            yield [pod.id, pod.name, r.id, r.speaker_id, r.speaker_alias, r.sensor_name,
                   r.time_stamp, r.heart_rate, r.rr_ms or '']


def _rr_rows(ctx):
    for pod in ctx.pods:
        for r in _hr_rows(pod):
            for i, rr in enumerate(v for v in (r.rr_ms or '').split(',') if v.strip()):
                yield [pod.id, pod.name, r.id, r.speaker_alias, r.sensor_name, r.time_stamp, i, int(rr)]


def _video_segment_rows(ctx):
    for pod in ctx.pods:
        files = ctx.media.recording_files(pod.id)
        if not files:
            continue
        segments = ctx.media.segments(ctx.session.id, pod.id)
        for i, path in enumerate(files):
            seg = segments[i] if i < len(segments) else {}
            yield [pod.id, pod.name, i + 1, re.split(r'[\\/]', path)[-1],
                   _round(seg.get('start')), _round(seg.get('video_start')), _round(seg.get('duration'))]


def _round(value):
    return '' if value is None else round(value, 3)


class Dataset:
    """One file in the ZIP: either rows(ctx) under fixed columns, or one of
    the older metric writers (which choose their own columns and honour the
    window size)."""

    def __init__(self, key, filename, about, columns=None, rows=None, writer=None):
        self.key = key
        self.filename = filename
        self.about = about
        self.columns = columns
        self.rows = rows
        self.writer = writer

    @property
    def windowed(self):
        return self.writer is not None


DATASETS = {d.key: d for d in [
    Dataset('pods', 'pods.csv',
            'One row per pod (group) in the session. activity_s is the span of its transcribed speech.',
            ['pod_id', 'pod_name', 'removed', 'activity_s', 'participants', 'video_segments'], _pod_rows),
    Dataset('participants', 'participants.csv',
            'Everyone enrolled in each pod. speaker_id matches the transcript and metric files.',
            ['pod_id', 'pod_name', 'speaker_id', 'alias'], _participant_rows),
    Dataset('markers', 'markers.csv',
            'Protocol moments pressed on each join-page laptop, in time order: session_joined, '
            'recording_started, baseline_started, eyes_open_start/end, eyes_closed_start/end, game_start/end, '
            'discussion_start (or baseline_cancelled). sensors lists the wearables that laptop had connected.',
            list(EventMarker.CSV_COLUMNS), _marker_rows),
    Dataset('transcripts', 'transcripts.csv',
            'Every transcript line, including lines no participant was matched to (speaker_id -1).',
            ['pod_id', 'pod_name', 'transcript_id', 'start_s', 'end_s', 'length_s', 'speaker_tag',
             'speaker_id', 'question', 'word_count', 'text'], _transcript_rows),
    Dataset('transcript_metrics', 'transcript_metrics.csv',
            'Per-line language and collaboration metrics (the "Transcript Metrics" export). '
            'With a window size, one row per participant per window.',
            writer=lambda si, sid, w: write_transcript_metrics(si, sid, w, 'csv')),
    Dataset('video_metrics', 'video_metrics.csv',
            'Per-person facial emotion, attention and gaze target from the video (the "Video Metrics" export). '
            'With a window size, one row per participant per window.',
            writer=lambda si, sid, w: write_video_metrics(si, sid, w, 'csv')),
    Dataset('transcript_video_metrics', 'transcript_video_metrics.csv',
            'Transcript lines joined with the speaker\'s video metrics (the "Transcript-Video Metrics" export).',
            writer=lambda si, sid, w: write_transcript_video_metrics(si, sid, w, 'csv')),
    Dataset('keyword_usage', 'keyword_usage.csv',
            'Each session keyword detected in speech. similarity_pct: 100 is an exact match.',
            ['pod_id', 'pod_name', 'transcript_id', 'time_s', 'speaker_tag', 'keyword', 'word_detected',
             'similarity_pct'], _keyword_rows),
    Dataset('joint_attention', 'joint_attention.csv',
            'Each moment at least two people were tracked on video. joint=1 rows name a target '
            'two or more of them looked at together (people separated by ";").',
            ['pod_id', 'pod_name', 'time_s', 'people_tracked', 'joint', 'target', 'people'],
            _joint_attention_rows),
    Dataset('heart_rate', 'heart_rate.csv',
            'Heart-rate strap notifications (about one per second). rr_ms lists the beat-to-beat '
            'intervals in that notification, comma separated.',
            ['pod_id', 'pod_name', 'row_id', 'speaker_id', 'speaker_alias', 'sensor_name', 'time_s',
             'heart_rate', 'rr_ms'], _heart_rate_rows),
    Dataset('rr_intervals', 'rr_intervals.csv',
            'One row per heartbeat interval, in the order the strap sent them (use for HRV). '
            'row_id links to heart_rate.csv; beat_index orders beats within a notification.',
            ['pod_id', 'pod_name', 'row_id', 'speaker_alias', 'sensor_name', 'time_s', 'beat_index',
             'rr_ms'], _rr_rows),
    Dataset('video_segments', 'video_segments.csv',
            'Each pod\'s recording files. A pod that disconnected has several; the downloadable video plays '
            'them back to back. session_start_s is where a segment starts on the session clock, '
            'video_start_s where it starts in the downloaded video.',
            ['pod_id', 'pod_name', 'segment', 'file', 'session_start_s', 'video_start_s', 'duration_s'],
            _video_segment_rows),
]}


def parse_items(text):
    """The dataset keys in ``text`` ("a,b,c"), in DATASETS order, or raise
    ValueError naming what is unknown or that nothing was chosen."""
    keys = [k.strip() for k in (text or '').split(',') if k.strip()]
    unknown = [k for k in keys if k not in DATASETS]
    if unknown:
        raise ValueError('Unknown data: %s.' % ', '.join(unknown))
    if not keys:
        raise ValueError('Choose at least one kind of data.')
    return [k for k in DATASETS if k in keys]


def parse_window(text):
    try:
        window = int(text or 0)
    except (TypeError, ValueError):
        raise ValueError('Window size must be a whole number of seconds.')
    if not 0 <= window <= MAX_WINDOW_S:
        raise ValueError('Window size must be between 0 and %d seconds.' % MAX_WINDOW_S)
    return window


def zip_filename(session):
    slug = re.sub(r'[^A-Za-z0-9._-]+', '_', session.name or '').strip('_')[:40]
    return 'session%d%s_data.zip' % (session.id, '_' + slug if slug else '')


def _csv_text(dataset, ctx, window):
    si = io.StringIO()
    if dataset.windowed:
        dataset.writer(si, ctx.session.id, window)
    else:
        writer = csv.writer(si)
        writer.writerow(dataset.columns)
        writer.writerows(dataset.rows(ctx))
    return si.getvalue()


def _readme(session, chosen, window, exported_at):
    lines = [
        'BLINC session export',
        '',
        'Session: %s (id %d)' % (session.name, session.id),
        'Created: %s UTC' % session.creation_date.strftime('%Y-%m-%d %H:%M:%S'),
        'Exported: %s UTC' % exported_at.strftime('%Y-%m-%d %H:%M:%S'),
        '',
        'Time columns (time_s, start_s, ...) are seconds since the session was created.',
        'Metric files with a "Start Time" column show it as H:MM:SS on the same clock.',
        'Files are UTF-8 with a byte-order mark, so Excel shows non-English names correctly.',
    ]
    if any(DATASETS[k].windowed for k in chosen):
        lines.append('Window size for the metric files: %s.' % (
            '%d s' % window if window else 'none (one row per line or video sample)'))
    for key in chosen:
        d = DATASETS[key]
        lines += ['', d.filename, '  ' + d.about]
        if d.columns:
            lines.append('  Columns: ' + ', '.join(d.columns))
    return '\n'.join(lines) + '\n'


def build_zip(session, keys, window, media, now=None):
    """The chosen datasets as an open, rewound temporary file holding the
    ZIP (deleted when closed). ``media`` provides recording_files(device_id)
    and segments(session_id, device_id) for the pod and video files."""
    ctx = _Context(session, media)
    out = tempfile.TemporaryFile()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('README.txt', _readme(session, keys, window, now or datetime.now(timezone.utc)))
        for key in keys:
            z.writestr(DATASETS[key].filename, '﻿' + _csv_text(DATASETS[key], ctx, window))
    out.seek(0)
    return out
