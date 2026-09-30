from datetime import datetime, timezone
# callbacks/config put src/common on sys.path; import them first so this
# module also imports standalone (the bare import below only worked because
# server.py had already imported callbacks).
import callbacks
import config as cf
from processing_config_base import validate_start_message
import json
import logging
import time

# Sweep grace for an unverifiable key (audit E.1). The 5 s sweep used to close
# a pod whenever the API could not be reached, so every API restart ended
# every live video pod ("session closed by the owner" in the browser). Only an
# explicit rejection closes at once; an unreachable/erroring API keeps the pod
# until the check has been failing continuously for this long.
KEY_CHECK_GRACE_SECONDS = 600
KEY_CHECK_WARN_INTERVAL_SECONDS = 60


def decide_key_check(outcome, unknown_since, now, grace=KEY_CHECK_GRACE_SECONDS):
    """Pure. outcome is 'valid' | 'revoked' | 'unknown' (callbacks.check_session_key);
    unknown_since is when the current run of 'unknown' results began (None if
    the last result was conclusive). Returns (keep_connection, unknown_since)."""
    if outcome == 'valid':
        return True, None
    if outcome == 'revoked':
        return False, None
    if unknown_since is None:
        unknown_since = now  # a new run of failures starts the clock
    return (now - unknown_since) < grace, unknown_since


class ProcessingConfig:
    def __init__(self, auth_key, session_key, server_start, start_offset, sample_rate, encoding, channels, embeddingsFile,sessionId,deviceId,videocartoonify,video,mimeExtension):
        self.auth_key = auth_key
        self.session_key = session_key
        self.server_start = server_start
        self.start_offset = start_offset
        self.sample_rate = sample_rate
        self.encoding = encoding
        self.depth = 2 if self.encoding in ['pcm_f16le', 'pcm_i16le'] else 4
        self.channels = channels
        self.embeddingsFile = embeddingsFile
        self.sessionId = sessionId
        self.deviceId = deviceId
        self.videocartoonify = videocartoonify
        self.video = video
        self.mimeExtension = mimeExtension
        self.key_unknown_since = None  # see decide_key_check
        self._key_warned_at = None

    @staticmethod
    def from_json(data,source=None):
        # Shared field validation (parse/allowlist) — see processing_config_base.
        ok, res = validate_start_message(data)
        if not ok:
            return False, res
        auth_key = res['auth_key']
        encoding = res['encoding']
        sample_rate = res['sample_rate']
        channels = res['channels']
        offset = res['offset']
        sessionId = res['sessionId']
        deviceId = res['deviceId']

        embeddingsFile = data.get('embeddingsFile', None)

        # check if video cartoonify is activated and  selected by user
        videocartoonify = data.get('Video_cartoonify',False) and cf.video_cartoonize()

        # check if video only is activated and  selected by user
        video = data.get('Video',False) or cf.video_record_original() or cf.video_record_reduced()

        mimeExtension = data.get('mimeextension',None)
        # Check if auth is required and if key is valid.
        try:
            session_key = callbacks.get_redis_session_key(auth_key)
            if session_key:
                session_config = json.loads(callbacks.get_redis_session_config(session_key))
                server_start = datetime.strptime(session_config.get('server_start', None), "%Y-%m-%d %H:%M:%S")
                start_offset = max((datetime.now(timezone.utc).replace(tzinfo=None) - server_start).total_seconds() - offset, 0.0)
            elif not session_key and source == "posthoc processing":
                server_start = datetime.strptime(data.get('server_start', None), "%Y-%m-%dT%H:%M:%S.%fZ")
                convert_off_set = datetime.strptime(data.get('off_set_date', None), "%a %b %d %H:%M:%S %Y")
                start_offset = max((convert_off_set - server_start).total_seconds() - offset, 0.0)
            else:
                logging.warning('Invalid key sent by device.')
                return False, "Invalid key."

            return True, ProcessingConfig(auth_key, session_key, server_start, start_offset, sample_rate, encoding, channels, embeddingsFile,sessionId,deviceId,videocartoonify,video,mimeExtension)
        except Exception:
            return False, "could not verify auth_key"

    def is_valid_key(self):
        # Called by the connection-manager sweep off the reactor; False closes
        # the pod as "access revoked".
        outcome, _key, detail = callbacks.check_session_key(self.auth_key)
        now = time.monotonic()
        since = self.key_unknown_since
        keep, self.key_unknown_since = decide_key_check(outcome, since, now)
        if outcome == 'unknown':
            unknown_for = int(now - since) if since is not None else 0
            if not keep:
                logging.warning('key check for %s unverifiable for %ds (%s); closing the pod',
                                self.auth_key, unknown_for, detail)
            elif self._key_warned_at is None or now - self._key_warned_at >= KEY_CHECK_WARN_INTERVAL_SECONDS:
                self._key_warned_at = now
                logging.warning('key check for %s unverifiable (%s); keeping the pod, %ds of %ds grace used',
                                self.auth_key, detail, unknown_for, KEY_CHECK_GRACE_SECONDS)
        elif self._key_warned_at is not None:
            logging.info('key check for %s reachable again (%s)', self.auth_key, outcome)
            self._key_warned_at = None
        return keep

