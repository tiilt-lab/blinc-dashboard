// The baseline a group records after "Start recording", before the
// discussion: look at a + for 30 s, close eyes for 30 s (each after a 3-2-1
// countdown), then a group game (name a fruit nobody has said yet, as fast as
// you can), then the discussion. Each step boundary is an event marker
// (services/event-markers.js), so the wearable streams can be cut into
// eyes-open rest, eyes-closed rest, game and discussion.

export const COUNTDOWN_S = 3
export const REST_S = 30

// Steps that end on their own, and how long they last (seconds).
export const TIMED_STEPS = {
    countdown_open: COUNTDOWN_S,
    eyes_open: REST_S,
    countdown_closed: COUNTDOWN_S,
    eyes_closed: REST_S,
}

// step -> [next step, markers sent on leaving it]. null is "not running":
// leaving null starts the baseline, reaching null after discussion_ready
// hands the screen back to the dashboard.
const FLOW = {
    null: ["countdown_open", ["baseline_started"]],
    countdown_open: ["eyes_open", ["eyes_open_start"]],
    eyes_open: ["countdown_closed", ["eyes_open_end"]],
    countdown_closed: ["eyes_closed", ["eyes_closed_start"]],
    eyes_closed: ["game_intro", ["eyes_closed_end"]],
    game_intro: ["game", ["game_start"]],
    game: ["discussion_ready", ["game_end"]],
    discussion_ready: [null, ["discussion_start"]],
}

export function advance(step) {
    const [next, markers] = FLOW[step === null || step === undefined ? "null" : step] || [null, []]
    return { next, markers }
}

// Leaving early from any step.
export function cancel(step) {
    return { next: null, markers: step ? ["baseline_cancelled"] : [] }
}

// The whole-second number a countdown shows: 3, 2, 1.
export function countdownNumber(remainingMs) {
    return Math.max(1, Math.ceil(remainingMs / 1000))
}
