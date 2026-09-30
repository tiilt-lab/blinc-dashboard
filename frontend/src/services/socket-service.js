import { io } from 'socket.io-client';
export class SocketService {

  //api = new ApiService()

  // Creates socket connection to server.
  //
  // `joinFields()` (optional) is called on EVERY connect and its result is
  // merged into the join_room payload — the caller passes the last-seen
  // transcript / video-metric ids so a reconnect replays only what it
  // missed instead of the whole session digest.
  createSocket(endpoint, room = null, joinFields = null) {
    // WebSocket first, long-polling as the fallback. The client was
    // polling-first for a while (a server migration had broken the
    // WebSocket transport); WS works again, and polling-first cost ~4
    // HTTP round trips per connection before engine.io upgraded.
    const socket = io(window.location.protocol + '//' + window.location.host + '/' + endpoint, {transports: ['websocket', 'polling']});
    // A dead feed used to be indistinguishable from a quiet class: every
    // handler was a no-op. Log the lifecycle (warn: the lint config allows
    // only warn/error) so the console tells the story.
    const log = (event, detail) => {
      console.warn('[socket ' + endpoint + '] ' + event + (detail === undefined ? '' : ': ' + detail));
    };
    socket.on('connect', () => {
      log('connected', socket.id + ' via ' + (socket.io.engine ? socket.io.engine.transport.name : '?'));
      if (room != null) {
        const extra = joinFields ? joinFields() : null;
        socket.emit('join_room', {room: room, ...(extra || {})});
      }
    });

    socket.on('disconnect', (reason) => log('disconnected', reason));
    socket.on('connect_error', (e) => log('connect_error', e && e.message));
    // Reconnect events live on the Manager (socket.io), not the socket.
    socket.io.on('reconnect_attempt', (n) => log('reconnect attempt', n));
    socket.io.on('reconnect', (n) => log('reconnected after attempt', n));
    socket.io.on('reconnect_error', (e) => log('reconnect_error', e && e.message));
    socket.io.on('reconnect_failed', () => log('reconnect failed (gave up)'));
    return socket;
  }
}
