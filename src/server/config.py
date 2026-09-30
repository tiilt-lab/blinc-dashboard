import configparser
import os

def initialize():
    global config
    config_path = os.path.dirname(os.path.abspath(__file__)) + '/config.ini'
    config = configparser.RawConfigParser(allow_no_value=True)
    config.read(config_path)

def https():
    return str(config['server']['https']) in ['true', 'True', 't']

def cloud():
    # TODO: probably want this as a separate config, but for now http/https is probably good enough
    # to determine if running in AWS
    return https()

def proxy_count():
    # How many reverse proxies sit in front of this instance, i.e. how many
    # trailing X-Forwarded-For entries to trust. This box has only nginx, so 1.
    # It used to be derived from https() on the assumption that TLS meant an AWS
    # load balancer in front of nginx; on a plain nginx host that made ProxyFix
    # read one entry too far to the left, into the part of the header the client
    # supplies. remote_addr then became whatever a caller claimed, which
    # verify_local trusts, and with no header at all it stayed nginx's
    # 127.0.0.1 so every client shared one rate-limit bucket.
    return int(config['server'].get('proxy_count', 1))

def debug():
    return str(config['server']['debug']) in ['true', 'True', 't']

def domain():
    return domains()[0]

def domains():
    # 'domain' accepts a comma-separated list so one instance can serve
    # several hostnames (all must be allowed as SocketIO CORS origins).
    scheme = 'https' if https() else 'http'
    value = str(config['server']['domain'])
    return ['{0}://{1}'.format(scheme, d.strip()) for d in value.split(',') if d.strip()]

# Temp field until we implement RDS.
def database_user():
    user = str(config['server']['database_user'])
    return "{0}:{1}".format(user, user)

def database_name():
    # DC_DATABASE_NAME points one process (e.g. a migration check) at another
    # database on the same server without editing config.ini.
    return os.environ.get('DC_DATABASE_NAME') or str(config['server'].get('database_name', 'discussion_capture'))

def root_dir():
    return str(config['rootpath']['root_dir'])

def redis_host():
    return str(config['server'].get('redis_host', 'localhost'))

def redis_port():
    return int(config['server'].get('redis_port', 6379))

def redis_db():
    # Defaults to 0 so existing deployments are unchanged. Set to a distinct
    # value per instance to isolate Redis keys + the SocketIO message queue.
    return int(config['server'].get('redis_db', 0))

def redis_url():
    return 'redis://{0}:{1}/{2}'.format(redis_host(), redis_port(), redis_db())

def sync_peer_url():
    # Base URL of a peer BLINC deployment to mirror student profiles with.
    # Empty (the default) disables cross-instance student syncing entirely.
    return str(config['sync'].get('peer_url', '')).rstrip('/') if config.has_section('sync') else ''

def sync_token():
    # Shared secret required on both the outbound and inbound sync requests.
    return str(config['sync'].get('token', '')) if config.has_section('sync') else ''

def sync_enabled():
    # Syncing is only available when both a peer and a shared token are set.
    return bool(sync_peer_url()) and bool(sync_token())