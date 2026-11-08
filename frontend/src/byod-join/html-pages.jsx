import React, { useState, useEffect, useRef } from "react"
import { btnPrimary, btnPrimaryTall, btnSecondaryTall, dlgInput, dlgSelect, dlgPrimary, dlgError } from "../components/dialog-styles"
import { pageShell, formCard } from "../components/layout-styles"
import { PodMicRing } from "../components/pod-mic-ring"
import { Appheader } from "../header/header-component"
import { VoiceRecorder } from "react-voice-recorder-player"
import {
    DialogBox,
    WaitingDialog,
    DialogBoxTwoOption,
    GenericDialogBox,
} from "../dialog/dialog-component"
import { AppSectionBoxComponent } from "../components/section-box/section-box-component"
import { AppSpinner } from "../spinner/spinner-component"
import { AppSessionToolbar } from "../session-toolbar/session-toolbar-component"
import { TranscriptsComponentClient } from "../transcripts/transcripts-component_client"

import style from "./byod-join.module.css"
import style2 from "../pod-details/pod.module.css"
import style3 from "../components/library-dialog.module.css"
import MicIcon from "@icons/Mic"
import Check from "@icons/Check"
import Chevron from "@icons/Chevron"

import { InlineDeviceCheck } from "./device-check"
import { AppInfographicsComparison } from "../components/infographics-view/infographics-comparison"

const fieldLabel = "mb-1.5 block text-left text-sm font-semibold text-tiilt-ink"

// Which slot of the session's pod setup this laptop occupies: the group
// (from the setup's list, or typed) and, when each team records from its
// own laptop, the team. The join sends both; the pod is named after them.
function SlotPicker({ setup, paired, savedRole, error }) {
    const groups = (setup && setup.groups) || []
    const teams = (setup && setup.teams) || []
    const twoTeams = paired && teams.length >= 2
    // A paired session always carries its team names in the setup; without
    // them (never the normal case) the team is typed instead of picked.
    const teamOptions = twoTeams ? teams : []
    const typeTeam = paired && !twoTeams
    const groupSize = setup && setup.group_size
    // Controlled, seeded from the saved/linked slot: the option lists arrive
    // with the session lookup, after the first paint, and an uncontrolled
    // select would keep whatever it showed when it mounted.
    // "Other…" lets a laptop join as a group that is not on the list (an
    // extra table, a late-formed group): the custom label is typed below.
    const OTHER = "__other__"
    const [group, setGroup] = useState((savedRole && savedRole.group) || "")
    const [side, setSide] = useState((savedRole && savedRole.side) || "")
    useEffect(() => {
        if (savedRole && savedRole.group) setGroup(savedRole.group)
        if (savedRole && savedRole.side) setSide(savedRole.side)
    }, [savedRole])
    const listed = groups.length === 0 || groups.includes(group) || group === "" || group === OTHER
    const selectValue = groups.includes(group) ? group : group === "" ? "" : OTHER
    const intro = twoTeams
        ? `This session runs groups of ${groupSize} as ${teams.map((t) => `${t.name} (${t.size})`).join(" vs ")}, each team on its own device. Pick the slot this device occupies; everything it hears and sees counts for that team.`
        : paired
          ? "This negotiation session records each team from its own device. Enter your table and which side this device sits with; everything it hears and sees counts for that team."
          : `This session runs groups of ${groupSize}${teams.length >= 2 ? ` (${teams.map((t) => `${t.name} ${t.size}`).join(" vs ")})` : ""}. Pick your table or group.`
    return (
        <div className="flex flex-col gap-3 rounded-xl border border-tiilt/40 bg-tiilt-soft/40 px-3 py-3">
            <div className="text-left text-xs leading-5 text-tiilt-ink">{intro}</div>
            <div>
                <label htmlFor="negGroup" className={fieldLabel}>
                    Table or breakout group
                </label>
                {groups.length > 0 ? (
                    <>
                        <select
                            id="negGroup"
                            className={dlgSelect}
                            value={selectValue}
                            onChange={(e) => setGroup(e.target.value)}
                        >
                            <option value="">Choose…</option>
                            {groups.map((g) => (
                                <option key={g} value={g}>
                                    {g}
                                </option>
                            ))}
                            <option value={OTHER}>Other / a new group…</option>
                        </select>
                        {selectValue === OTHER ? (
                            <input
                                className={dlgInput + " mt-2"}
                                id="negGroupCustom"
                                maxLength={32}
                                autoFocus
                                value={group === OTHER ? "" : group}
                                onChange={(e) => setGroup(e.target.value || OTHER)}
                                placeholder="Name your group, e.g. Table 11"
                            />
                        ) : null}
                        {!listed ? null : null}
                    </>
                ) : (
                    <input
                        className={dlgInput}
                        id="negGroup"
                        maxLength={32}
                        value={group}
                        onChange={(e) => setGroup(e.target.value)}
                        placeholder="e.g. Table 3"
                    />
                )}
            </div>
            {typeTeam ? (
                <div>
                    <label htmlFor="negSide" className={fieldLabel}>
                        This device records (team name)
                    </label>
                    <input
                        className={dlgInput}
                        id="negSide"
                        maxLength={16}
                        value={side}
                        onChange={(e) => setSide(e.target.value)}
                        placeholder="Team name"
                    />
                </div>
            ) : null}
            {teamOptions.length > 0 ? (
                <div>
                    <label htmlFor="negSide" className={fieldLabel}>
                        This device records
                    </label>
                    <select
                        id="negSide"
                        className={dlgSelect}
                        value={teamOptions.some((t) => t.name === side) ? side : ""}
                        onChange={(e) => setSide(e.target.value)}
                    >
                        <option value="">Choose a team…</option>
                        {teamOptions.map((t) => (
                            <option key={t.name} value={t.name}>
                                {t.name}&apos;s team{t.size ? ` (${t.size} people)` : ""}
                            </option>
                        ))}
                    </select>
                </div>
            ) : null}
            {error ? <div className={dlgError}>{error}</div> : null}
        </div>
    )
}

// The slot fields of the join form, when they are shown ({group, side});
// null for a session without a pod setup.
function readNegotiationRole() {
    const group = document.getElementById("negGroup")
    const custom = document.getElementById("negGroupCustom")
    const side = document.getElementById("negSide")
    if (!group && !side) return null
    let label = group ? group.value.trim() : ""
    if (label === "__other__") label = custom ? custom.value.trim() : ""
    return { group: label, side: side ? side.value : "" }
}

// Speaker name as an inline editor: click the name to type. Committing
// (Enter/blur) a name that matches an enrolled profile attaches that
// profile's saved fingerprint automatically; other names just rename. The
// card's single left-side status icon (check/X) is the only fingerprint
// indicator.
function SpeakerNameEditor({ speaker, onCommit }) {
    const [editing, setEditing] = useState(false)
    const [value, setValue] = useState("")
    const committed = React.useRef(false)

    const commit = () => {
        if (committed.current) return
        committed.current = true
        setEditing(false)
        const name = value.trim()
        if (name && name !== speaker.alias) onCommit(speaker, name)
    }

    if (!editing) {
        return (
            <button
                type="button"
                title="Tap to edit the name"
                className="grow cursor-pointer rounded font-sans text-lg/loose font-bold text-tiilt-ink hover:bg-tiilt-ground/60"
                onClick={() => {
                    committed.current = false
                    setValue(speaker.alias || "")
                    setEditing(true)
                }}
            >
                {speaker.alias}
            </button>
        )
    }
    return (
        <span className="flex grow items-center gap-1.5">
            <input
                autoFocus
                aria-label={`Name for ${speaker.alias}`}
                className={dlgInput + " grow text-center text-lg font-bold"}
                value={value}
                spellCheck="false"
                onChange={(e) => setValue(e.target.value)}
                onKeyDown={(e) => {
                    if (e.key === "Enter") commit()
                    if (e.key === "Escape") {
                        committed.current = true
                        setEditing(false)
                    }
                }}
                onBlur={commit}
            />
        </span>
    )
}

// iOS-style switch, same look as the create-session toggles once had.
function Toggle({ label, checked, onChange }) {
    return (
        <button
            type="button"
            role="switch"
            aria-checked={checked}
            onClick={onChange}
            className="flex w-full items-center justify-between gap-3 rounded-lg border border-tiilt-line bg-white px-4 py-3 text-left transition hover:bg-tiilt-soft/40"
        >
            <span className="text-sm font-medium text-tiilt-ink">{label}</span>
            <span
                className={`relative inline-flex h-6 w-11 flex-none items-center rounded-full transition ${checked ? "bg-tiilt" : "bg-tiilt-line"}`}
            >
                <span
                    className={`inline-block h-5 w-5 transform rounded-full bg-[#fff] shadow transition ${checked ? "translate-x-[22px]" : "translate-x-0.5"}`}
                />
            </span>
        </button>
    )
}

// Optional Polar heart-rate strap per speaker, shown only when the join
// form's "Add Polar H10" toggle is on. Pairing opens the browser's
// Bluetooth chooser, which lists every heart-rate strap advertising nearby
// by its printed ID (e.g. "Polar H10 8C0B2A2B") — picking the strap on
// this person's chest IS the assignment. Once connected the card shows the
// sensor ID and live BPM. Needs Web Bluetooth (Chrome/Edge on desktop or
// Android; no iOS browser has it) — say so rather than silently hiding.
function PolarStrapControl({ speaker, supported, enabled, info, onAssign, onUnassign }) {
    const [notice, setNotice] = useState("")
    if (!enabled) return null
    if (!supported) {
        return (
            <span className="mx-auto mt-1 max-w-56 font-ahamono text-[11px] leading-snug text-tiilt-muted">
                Heart-rate pairing isn&apos;t available in this browser — use
                Chrome or Edge on a laptop or Android device (iPhones
                don&apos;t support Web Bluetooth).
            </span>
        )
    }
    if (!info) {
        const pair = async () => {
            setNotice("")
            const outcome = await onAssign(speaker)
            if (outcome === "dismissed") {
                setNotice(
                    "No strap picked. Straps only show up in the list while worn — the electrodes need skin contact (moisten them if needed), then try again.",
                )
            } else if (outcome === "failed") {
                setNotice(
                    "Pairing failed — check that Bluetooth is on and the strap isn't connected to another app or phone, then try again.",
                )
            }
        }
        return (
            <span className="mx-auto mt-1 flex flex-col items-center gap-1">
                <button
                    type="button"
                    aria-label={`Pair a heart-rate strap for ${speaker.alias}`}
                    className="cursor-pointer rounded px-1 font-ahamono text-[11px] text-tiilt-muted hover:text-tiilt-ink"
                    onClick={pair}
                >
                    ♥ pair strap
                </button>
                {notice && (
                    <span className="max-w-56 text-[11px] leading-snug text-amber-600">
                        {notice}
                    </span>
                )}
            </span>
        )
    }
    const live = info.status === "connected"
    return (
        <span className="mx-auto flex items-center gap-1.5 font-ahamono text-[11px] text-tiilt-muted">
            <span className={live ? "text-red-500" : "text-tiilt-line"}>♥</span>
            <span className="font-semibold text-tiilt-ink">
                {info.bpm != null ? info.bpm + " bpm" : "…"}
            </span>
            <span>{info.name}</span>
            {info.battery != null && <span>{info.battery}%</span>}
            {!live && <span className="text-amber-600">reconnecting…</span>}
            <button
                type="button"
                aria-label={`Remove heart-rate strap for ${speaker.alias}`}
                className="cursor-pointer px-1 hover:text-tiilt-ink"
                onClick={() => onUnassign(speaker)}
            >
                ✕
            </button>
        </span>
    )
}

// One-tap group sizing on the speaker page (the join form no longer asks):
// creates N speaker slots at once; each then records or attaches a
// fingerprint. Hidden as soon as any slot exists.
function GroupSizeQuickAdd({ visible, onAdd }) {
    const [count, setCount] = useState(4)
    if (!visible) return null
    return (
        <div className="m-0.5 flex w-60 items-end gap-2 @sm:m-3 @sm:w-80">
            <div className="grow text-left">
                <label className={fieldLabel}>People in your group</label>
                <select
                    aria-label="Number of people in your group"
                    className={dlgSelect}
                    value={count}
                    onChange={(e) => setCount(parseInt(e.target.value, 10))}
                >
                    {[1, 2, 3, 4, 5, 6, 7, 8].map((n) => (
                        <option key={n} value={n}>
                            {n}
                        </option>
                    ))}
                </select>
            </div>
            <button
                className={dlgPrimary + " mt-0 flex-none px-4"}
                onClick={() => onAdd(count)}
            >
                Add
            </button>
        </div>
    )
}

function ByodJoinPage(props) {
    // Join-form niceties: prefer the prefilled code (link/QR) as a compact
    // chip; the Advanced disclosure starts open so the source/camera options
    // and mic check are visible without an extra tap.
    const [editCode, setEditCode] = useState(false)
    // Controlled so the inline device check can follow the selection (a
    // camera preview only when joining with video). It opens on what the
    // session starter chose (props.joinWithDefault, from the passcode
    // lookup; Audio until then and for sessions that chose nothing), and
    // follows that answer until the team touches the list themselves.
    const [joinwithSel, setJoinwithSel] = useState(props.joinWithDefault || "Audio")
    const joinwithTouched = useRef(false)
    useEffect(() => {
        if (!joinwithTouched.current && props.joinWithDefault) setJoinwithSel(props.joinWithDefault)
    }, [props.joinWithDefault])
    // Collapsed extras (the Polar H10 toggle); everything essential is
    // visible without it.
    const [showAdvanced, setShowAdvanced] = useState(false)
    // Reflect the derived join phase (join-machine.ts) onto the body so E2E
    // and debugging have one signal for "where in the flow are we". Purely
    // observational — no behavior depends on it yet.
    useEffect(() => {
        if (props.joinPhase) document.body.dataset.joinPhase = props.joinPhase
        return () => {
            delete document.body.dataset.joinPhase
        }
    }, [props.joinPhase])
    // The group name is optional: left blank, the server hands out the next
    // free letter in the session (Group A, Group B, ...). After a join it is
    // prefilled with the assigned name so backing out and rejoining resumes
    // the same pod instead of minting a new letter.
    return (
        <>
            {(props.currentForm === "gottoselectedtranscript" &&
                Object.keys(props.currentTranscript) && (
                    <TranscriptsComponentClient
                        sessionDevice={props.sessionDevice}
                        transcriptIndex={props.currentTranscript.id}
                        setParentCurrentForm={props.setCurrentForm}
                    />
                )) ||
                (props.currentForm === "gototranscript" && (
                    <TranscriptsComponentClient
                        sessionDevice={props.sessionDevice}
                        transcriptIndex={undefined}
                        setParentCurrentForm={props.setCurrentForm}
                    />
                )) || (
                    <div role="main" className="main-container">
                        <Appheader
                            title={props.pageTitle}
                            leftText={false}
                            rightText={"Options"}
                            // Every join mode: "Add a participant" lives in
                            // this dialog, and audio joins (the default) used
                            // to get an empty dialog instead (2026-10-19).
                            rightTextClick={() => props.openDialog("Options")}
                            nav={() => props.navigateToLogin()}
                        />
                        {props.joinPhase === "form" && (
                            <div className={pageShell}>
                            <div className={formCard}>
                                <div className="mx-auto flex w-full max-w-md grow flex-col gap-4 px-4 py-6">
                                    <div className="text-sm leading-6 text-tiilt-muted">
                                        Enter the passcode to join a session
                                        — your group gets the next free name
                                        automatically. If rejoining, use the
                                        same group name as before.
                                    </div>
                                    {props.pairedSession || props.joinSetup ? (
                                        <SlotPicker
                                            setup={props.joinSetup}
                                            paired={props.pairedSession}
                                            savedRole={props.savedRole}
                                            error={props.roleError}
                                        />
                                    ) : null}
                                    <div>
                                        <label htmlFor="name" className={fieldLabel}>
                                            Group name{" "}
                                            <span className="font-normal text-tiilt-muted">
                                                (optional)
                                            </span>
                                        </label>
                                        <input
                                            className={dlgInput}
                                            id="name"
                                            defaultValue={props.savedName || ""}
                                            placeholder={
                                                props.pairedSession || props.joinSetup
                                                    ? "Leave blank to be named after your table and team"
                                                    : "Leave blank to be named Group A, B, C…"
                                            }
                                        />
                                    </div>

                                    <div>
                                        <label htmlFor="passcode" className={fieldLabel}>
                                            Passcode
                                        </label>
                                        {props.pcode && !editCode ? (
                                            <div className="flex items-center justify-between rounded-xl border border-tiilt-line bg-tiilt-ground px-4 py-3">
                                                <span className="font-mono text-lg font-bold tracking-[0.2em] text-tiilt-ink">
                                                    {props.pcode}
                                                </span>
                                                <button
                                                    type="button"
                                                    onClick={() => setEditCode(true)}
                                                    className="cursor-pointer text-sm font-semibold text-tiilt transition hover:text-tiilt-deep"
                                                >
                                                    Change
                                                </button>
                                                <input
                                                    type="hidden"
                                                    id="passcode"
                                                    value={props.pcode}
                                                />
                                            </div>
                                        ) : (
                                            <input
                                                className={dlgInput}
                                                id="passcode"
                                                value={props.pcode}
                                                placeholder="Session word (e.g. MAPLE)"
                                                onInput={(event) =>
                                                    props.changeTouppercase(event)
                                                }
                                            />
                                        )}
                                        {props.wrongInput ? (
                                            <div className={dlgError + " mt-2"}>
                                                That passcode looks too
                                                long — it should be one
                                                short word.
                                            </div>
                                        ) : null}
                                    </div>
                                    <div>
                                        <div className="flex flex-col gap-4">
                                            <div>
                                                <label htmlFor="joinwith" className={fieldLabel}>
                                                    Join with
                                                </label>
                                                <select
                                                    id="joinwith"
                                                    className={dlgSelect}
                                                    value={joinwithSel}
                                                    onChange={(e) => {
                                                        joinwithTouched.current = true
                                                        setJoinwithSel(e.target.value)
                                                    }}
                                                >
                                                    {/* "Video (cartoon)" was dropped: the
                                                        deployment has cartoonize=false, so it
                                                        behaved identically to plain Video. The
                                                        backend still accepts the value. */}
                                                    <option value="Audio">Audio</option>
                                                    <option value="Video">Video</option>
                                                </select>
                                            </div>
                                            <InlineDeviceCheck
                                                wantsVideo={
                                                    joinwithSel !== "Audio"
                                                }
                                                selectionRef={
                                                    props.deviceSelectionRef
                                                }
                                            />
                                            <div>
                                                <button
                                                    type="button"
                                                    onClick={() => setShowAdvanced(!showAdvanced)}
                                                    aria-expanded={showAdvanced}
                                                    className="flex cursor-pointer items-center gap-1.5 text-sm font-semibold text-tiilt transition hover:text-tiilt-deep"
                                                >
                                                    <Chevron
                                                        direction={showAdvanced ? "down" : "right"}
                                                        size={12}
                                                    />
                                                    Advanced options
                                                </button>
                                                <div className={showAdvanced ? "mt-3 flex flex-col gap-4" : "hidden"}>
                                                    {joinwithSel !== "Audio" && (
                                                        <div>
                                                            <Toggle
                                                                label="Live video analytics"
                                                                checked={!!props.liveAnalytics}
                                                                onChange={() =>
                                                                    props.setLiveAnalytics(
                                                                        !props.liveAnalytics,
                                                                    )
                                                                }
                                                            />
                                                            <p className="mt-1 text-xs text-tiilt-muted">
                                                                Gaze, attention and
                                                                facial-emotion models run
                                                                during the session. Turn
                                                                off to lighten server
                                                                load when many groups
                                                                record at once — the
                                                                video is still recorded
                                                                and can be analyzed
                                                                afterwards.
                                                            </p>
                                                        </div>
                                                    )}
                                                    <div>
                                                        <Toggle
                                                            label="Add Polar H10 heart-rate straps"
                                                            checked={!!props.polarEnabled}
                                                            onChange={() =>
                                                                props.setPolarEnabled(
                                                                    !props.polarEnabled,
                                                                )
                                                            }
                                                        />
                                                        <p className="mt-1 text-xs text-tiilt-muted">
                                                            Pair each strap from
                                                            its wearer&apos;s card
                                                            on the next page:
                                                            nearby straps are
                                                            listed by the ID
                                                            printed on the
                                                            sensor.
                                                        </p>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                                <div className="w-full flex-none border-t border-tiilt-line bg-white">
                                    <div className="mx-auto w-full max-w-md px-4 py-4">
                                        <button
                                            className={dlgPrimary + " w-full"}
                                            onClick={() =>
                                                props.verifyInputAndAudio(
                                                    document
                                                        .getElementById("name")
                                                        .value.trim(),
                                                    document
                                                        .getElementById("passcode")
                                                        .value.trim(),
                                                    document
                                                        .getElementById("joinwith")
                                                        .value.trim(),
                                                    // Group size is chosen on
                                                    // the speaker page now.
                                                    0,
                                                    readNegotiationRole(),
                                                )
                                            }
                                        >
                                            Connect to server
                                        </button>
                                    </div>
                                </div>
                            </div>
                            </div>
                        )}
                        {props.joinPhase === "enrolling" && (
                                <React.Fragment>
                                    <div className="@container relative box-border flex min-h-0 grow flex-col items-center justify-between overflow-y-auto text-center">
                                        <div className="w-[300px] px-2 sm:w-[400px] lg:w-3xl">
                                            <div className="my-1.5 font-sans text-base/loose font-medium text-tiilt-muted">
                                                Please add a Speaker Fingerprint
                                                for each speaker
                                            </div>
                                            <div className="my-1.5 font-sans text-xs/normal font-normal text-tiilt-muted">
                                                Every speaker needs a voice
                                                fingerprint so the session can
                                                tell who is talking. Record
                                                one from each card, or type an
                                                enrolled username as the name
                                                to use a saved fingerprint.
                                                One more person than expected?
                                                Add a card for them; someone
                                                arriving later can be added
                                                from Options during the session.
                                            </div>
                                        </div>
                                        <div className="mt-2 h-fit w-[300px] sm:w-[400px] lg:w-3xl">
                                            {!props.speakers && (
                                                <div
                                                    className={
                                                        style3[
                                                        "load-text onload"
                                                        ]
                                                    }
                                                >
                                                    Loading...
                                                </div>
                                            )}
                                            {props.speakers &&
                                                props.speakers.length === 0 && (
                                                    <div
                                                        className={
                                                            style3[
                                                            "empty-keyword-list"
                                                            ]
                                                        }
                                                    >
                                                        <div
                                                            className={
                                                                style3[
                                                                "load-text"
                                                                ]
                                                            }
                                                        >
                                                            {" "}
                                                            No Speakers{" "}
                                                        </div>
                                                        <div
                                                            className={
                                                                style3[
                                                                "load-text-description"
                                                                ]
                                                            }
                                                        >
                                                            {" "}
                                                            Pick your group
                                                            size below to add a
                                                            card per member, or
                                                            Join Session to
                                                            detect speakers
                                                            automatically (less
                                                            accurate).{" "}
                                                        </div>
                                                    </div>
                                                )}
                                            {(props.speakers || []).map(
                                                (speaker, count) => (
                                                    <div
                                                        key={speaker.id ?? "speaker" + count}
                                                        className="my-3 flex flex-row items-center justify-between rounded-md border px-2 py-2"
                                                    >
                                                        {speaker.fingerprinted ? (
                                                            <span
                                                                role="img"
                                                                aria-label="Fingerprint ready"
                                                                title="Fingerprint ready"
                                                                className="flex-none text-tiilt-teal"
                                                            >
                                                                <Check size={32} />
                                                            </span>
                                                        ) : (
                                                            <span
                                                                role="img"
                                                                aria-label="No fingerprint yet"
                                                                title="No fingerprint yet — record one or use an enrolled name"
                                                                className="flex-none px-1 font-sans text-2xl font-bold text-tiilt-danger"
                                                            >
                                                                ✕
                                                            </span>
                                                        )}
                                                        <div className="flex grow flex-col text-center">
                                                            <SpeakerNameEditor
                                                                speaker={speaker}
                                                                onCommit={props.inlineRenameSpeaker}
                                                            />
                                                            {speaker.printSaved && (
                                                                <span className="text-xs text-tiilt-muted">
                                                                    Voice print saved to this profile
                                                                </span>
                                                            )}
                                                            <PolarStrapControl
                                                                speaker={speaker}
                                                                supported={props.bluetoothSupported}
                                                                info={(props.polarInfo || {})[speaker.id]}
                                                                enabled={props.polarEnabled}
                                                                onAssign={props.assignPolarSensor}
                                                                onUnassign={props.unassignPolarSensor}
                                                            />
                                                        </div>
                                                        <div className="flex flex-none flex-col items-center gap-0.5">
                                                            <button
                                                                type="button"
                                                                title={
                                                                    speaker.fingerprinted
                                                                        ? "Re-record this speaker's fingerprint"
                                                                        : "Record this speaker's fingerprint"
                                                                }
                                                                className="flex cursor-pointer items-center gap-1.5 rounded-lg border border-tiilt-line px-2.5 py-1.5 text-sm font-semibold text-tiilt-ink transition hover:bg-tiilt-ground"
                                                                onClick={() =>
                                                                    props.openForms(
                                                                        "fingerprintAudio",
                                                                        speaker,
                                                                    )
                                                                }
                                                            >
                                                                <MicIcon
                                                                    width={11}
                                                                    height={17}
                                                                    fill="currentColor"
                                                                />
                                                                Record
                                                            </button>
                                                            <button
                                                                type="button"
                                                                title="Remove this speaker"
                                                                aria-label={`Remove ${speaker.alias}`}
                                                                className="cursor-pointer rounded px-1.5 text-xs font-medium text-tiilt-muted transition hover:text-tiilt-danger"
                                                                onClick={() =>
                                                                    props.removeSpeakerSlot(
                                                                        speaker,
                                                                    )
                                                                }
                                                            >
                                                                Remove
                                                            </button>
                                                        </div>
                                                    </div>
                                                ),
                                            )}
                                        </div>
                                        <div className="flex flex-col items-center">
                                            <GroupSizeQuickAdd
                                                visible={
                                                    !!props.speakers &&
                                                    props.speakers.length === 0
                                                }
                                                onAdd={props.addSpeakerSlots}
                                            />
                                            {/* Hidden while the group-size
                                                quick-add above covers the
                                                empty page; once slots exist
                                                (quick-add gone) this is how
                                                a late arrival gets added. */}
                                            {!!props.speakers &&
                                                props.speakers.length > 0 && (
                                                    <button
                                                        className={btnSecondaryTall + " m-0.5 w-60 @sm:m-3 @sm:w-80"}
                                                        onClick={() => props.addSpeakerSlot()}
                                                    >
                                                        + Add speaker
                                                    </button>
                                                )}
                                            <button
                                                className={btnPrimaryTall + " m-0.5 w-60 @sm:m-3 @sm:w-80"}
                                                onClick={props.confirmSpeakers}
                                            >
                                                Join Session
                                            </button>
                                        </div>
                                    </div>
                                </React.Fragment>
                            )
                        }

                        {(props.joinPhase === "ready" || props.joinPhase === "recording") && (
                                <>
                                    <div className="toolbar-view-container">
                                        {props.session ? (
                                            <AppSessionToolbar
                                                session={props.session}
                                                closingSession={
                                                    props.disconnect
                                                }
                                                fromClient={true}
                                                menus={[
                                                    {
                                                        title: "Group",
                                                        action: () =>
                                                            props.viewGroup(),
                                                    },
                                                    {
                                                        title: "Comparison",
                                                        action: () =>
                                                            props.viewComparison(),
                                                    },
                                                ]}
                                                participants={props.speakers.map((speaker, index) => (
                                                    {
                                                        alias: speaker.alias,
                                                        action: () => props.loadSpeakerMetrics(speaker.id, speaker.alias),
                                                    }
                                                ))}
                                            />
                                        ) : (
                                            <></>
                                        )}
                                        <div className="center-column-container">
                                            <br />
                                            {/* Explicit start/stop for this pod's recording. */}
                                            {!props.armed ? (
                                                <div className="mx-auto w-full max-w-3xl shrink-0 rounded-xl border border-tiilt-line bg-white p-4 text-center">
                                                    <div className="text-sm font-semibold text-tiilt-ink">
                                                        Connected — not recording yet
                                                    </div>
                                                    <div className="mt-1 text-xs text-tiilt-muted">
                                                        Check your camera and
                                                        levels below, then start
                                                        when your group is ready.
                                                    </div>
                                                    <button
                                                        className={dlgPrimary + " mt-3 w-full"}
                                                        onClick={props.startRecording}
                                                    >
                                                        Start recording
                                                    </button>
                                                </div>
                                            ) : (
                                                <div className="mx-auto flex w-full max-w-3xl shrink-0 flex-col gap-2 rounded-xl border border-tiilt-line bg-white px-4 py-3">
                                                <div className="flex items-center justify-between">
                                                    <div className="flex items-center gap-2 text-sm font-semibold text-tiilt-ink">
                                                        <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-red-500"></span>
                                                        Recording{" "}
                                                        <span className="font-ahamono text-xs font-normal text-tiilt-muted">
                                                            {Math.floor(props.recSeconds / 60)}:
                                                            {String(props.recSeconds % 60).padStart(2, "0")}
                                                        </span>
                                                    </div>
                                                    <button
                                                        className="cursor-pointer rounded-lg bg-red-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-red-500"
                                                        onClick={props.requestEndRecording}
                                                    >
                                                        End recording
                                                    </button>
                                                </div>
                                                {props.micSilent && (
                                                    <div className="rounded-md bg-tiilt-orange/15 px-3 py-2 text-left text-xs leading-relaxed text-tiilt-orange-text">
                                                        No microphone signal —
                                                        the mic went quiet
                                                        (earbuds connecting,
                                                        Siri, or a call can
                                                        cause this). Speech is
                                                        not being captured
                                                        right now; unplug/replug
                                                        or speak to check it
                                                        comes back.
                                                    </div>
                                                )}
                                                {/* The resting baseline and group game, run
                                                    full screen before the discussion
                                                    (baseline-overlay.jsx). */}
                                                {props.showBaselineButton && (
                                                    <button
                                                        className={dlgPrimary + " w-full"}
                                                        onClick={props.startBaseline}
                                                    >
                                                        Record baseline
                                                    </button>
                                                )}
                                                </div>
                                            )}
                                            {props.joinwith === "Audio" && (
                                                <AppSectionBoxComponent
                                                    heading={"Audio control:"}
                                                    type="mx-auto mt-4 w-full max-w-3xl shrink-0"
                                                >
                                                    <button
                                                        type="button"
                                                        aria-label="Request help"
                                                        className={
                                                            style[
                                                            "pod-overview-button"
                                                            ]
                                                        }
                                                        style={{
                                                            margin: "0 auto",
                                                        }}
                                                        onClick={
                                                            props.requestHelp
                                                        }
                                                    >
                                                        <PodMicRing
                                                            style={{ marginTop: "22px" }}
                                                            color={props.POD_COLOR}
                                                            pulsing={!!props.button_pressed}
                                                            pulseClassName={style.svgpulse}
                                                        />
                                                        <div
                                                            style={{
                                                                marginTop:
                                                                    "13px",
                                                            }}
                                                        >
                                                            Help
                                                        </div>
                                                    </button>
                                                </AppSectionBoxComponent>
                                            )}

                                            {/* shrink-0: these boxes live in a flex column; without it
                                                they get crushed to border-height and clipped by the
                                                boxes' overflow-hidden (video preview showed as nothing). */}
                                            {props.joinwith === "Video" && (
                                                <AppSectionBoxComponent
                                                    heading={"Camera preview:"}
                                                    type="mx-auto mt-4 w-full max-w-3xl shrink-0"
                                                >
                                                    {/* The <video> must stay mounted even when hidden —
                                                        recording attaches to it and starts in its
                                                        onloadedmetadata. */}
                                                    <div
                                                        className={
                                                            props.preview
                                                                ? "px-3 pb-3"
                                                                : "hidden"
                                                        }
                                                    >
                                                        <video
                                                            muted={true}
                                                            autoPlay={true}
                                                            playsInline={true}
                                                            className="mx-auto w-full max-w-md rounded-xl bg-black"
                                                        />
                                                        <div className="mt-2 text-center text-xs text-tiilt-muted">
                                                            Live view of what is
                                                            being recorded. Hide
                                                            it under Options in
                                                            the header.
                                                        </div>
                                                    </div>
                                                </AppSectionBoxComponent>
                                            )}

                                            {props.joinwith ===
                                                "Videocartoonify" && (
                                                    <AppSectionBoxComponent
                                                        heading={"Camera preview:"}
                                                        type="mx-auto mt-4 w-full max-w-3xl shrink-0"
                                                    >
                                                        <div
                                                            className={
                                                                props.preview
                                                                    ? "px-3 pb-3"
                                                                    : "hidden"
                                                            }
                                                        >
                                                            <video
                                                                muted={true}
                                                                className="mx-auto w-full max-w-md rounded-xl bg-black"
                                                            />
                                                            <img
                                                                style={{
                                                                    marginLeft:
                                                                        "12px",
                                                                }}
                                                                width="150"
                                                                height="80"
                                                                alt="Your cartoon avatar preview"
                                                                src={
                                                                    props.cartoonImgUrl
                                                                }
                                                            />
                                                        </div>
                                                    </AppSectionBoxComponent>
                                                )}
                                            <AppInfographicsComparison
                                                displayTranscripts={props.displayTranscripts}
                                                displayVideoMetrics={props.displayVideoMetrics}
                                                fromclient={true}
                                                onClickedTimeline={props.onClickedTimeline}
                                                radarTrigger={props.radarTrigger}
                                                session={props.session}
                                                sessionDevice={props.sessionDevice}
                                                setRange={props.setRange}
                                                showBoxes={props.showBoxes}
                                                showFeatures={props.showFeatures}
                                                startTime={props.startTime}
                                                endTime={props.endTime}
                                                speakers={props.speakers}
                                                selectedSpkrId1={props.selectedSpkrId1}
                                                setSelectedSpkrId1={props.setSelectedSpkrId1}
                                                selectedSpkrId2={props.selectedSpkrId2}
                                                setSelectedSpkrId2={props.setSelectedSpkrId2}
                                                spkr1Transcripts={props.spkr1Transcripts}
                                                spkr2Transcripts={props.spkr2Transcripts}
                                                spkr1VideoMetrics={props.spkr1VideoMetrics}
                                                spkr2VideoMetrics={props.spkr2VideoMetrics}
                                                details={props.details}
                                                getSpeakerAliasFromID={props.getSpeakerAliasFromID}
                                                loadSpeakerMetrics={props.loadSpeakerMetrics}
                                            />
                                        </div>

                                        {props.loading() ? (
                                            <AppSpinner></AppSpinner>
                                        ) : (
                                            <></>
                                        )}
                                    </div>
                                </>
                            )}
                    </div>
                )}

            {/* Only the forms rendered inside — any other currentForm value
                (ClosedSession, Connecting, …) has its own dialog below, and
                showing this shell for them produced an empty floating box. */}
            <GenericDialogBox onClose={props.closeDialog}
                show={[
                    "Transcript",
                    "Options",
                    "fingerprintAudio",
                    "renameAlias",
                    "savedAudioVideoFingerprint",
                ].includes(props.currentForm)}
            >
                {(props.currentForm === "Transcript" && (
                    <div className={style2["dialog-content"]}>
                        <div className={style2["dialog-heading"]}>
                            Transcript
                        </div>
                        <div className={style2["dialog-body"]}>
                            {props.currentTranscript.transcript}
                        </div>
                        <div className={style2["dialog-button-container"]}>
                            <button
                                className={`${style2["dialog-button"]} ${style2["right-button"]}`}
                                onClick={props.closeDialog}
                            >
                                Close
                            </button>
                            <button
                                className={`${style2["dialog-button"]} ${style2["left-button"]}`}
                                onClick={props.seeAllTranscripts}
                            >
                                View All
                            </button>
                        </div>
                    </div>
                )) ||
                    (props.currentForm === "Options" && (
                        <div className={style2["dialog-content"]}>
                            <div className={style2["dialog-heading"]}>
                                Session Options
                            </div>
                            {(props.joinwith === "Video" ||
                                props.joinwith === "Videocartoonify") && (
                                <button
                                    className={style2["basic-button"]}
                                    onClick={props.togglePreview}
                                >
                                    {props.previewLabel}
                                </button>
                            )}
                            <button
                                className={style2["basic-button"]}
                                title="Someone joined your group late? Add a card and record their voice; they are picked up from now on."
                                onClick={props.addLateParticipant}
                            >
                                Add a participant
                            </button>
                            <button
                                className={style2["cancel-button"]}
                                onClick={props.closeDialog}
                            >
                                Cancel
                            </button>
                        </div>
                    )) ||
                    (props.currentForm === "fingerprintAudio" && (
                        <div className="flex w-[min(26rem,80vw)] flex-col items-center gap-3 text-center">
                            <div className="sans text-base/normal font-bold sm:text-xl/loose">
                                Record voice fingerprint
                                {props.selectedSpeaker
                                    ? ` — ${props.selectedSpeaker.alias}`
                                    : ""}
                            </div>
                            <div className="px-2 text-left text-xs leading-relaxed text-tiilt-muted">
                                Talk naturally for at least 10 seconds — say
                                your name, what you&apos;re working on, or how
                                your day is going, in your normal speaking
                                voice. Don&apos;t read from a script: natural
                                talking matches discussion speech best, and
                                pauses and &quot;um&quot;s are fine. If this
                                card carries your registered username and you
                                have no voice print yet, the take is saved as
                                your print too, so next time typing your name
                                is enough.
                            </div>
                            <VoiceRecorder
                                onAudioDownload={props.saveAudioFingerprint}
                                downloadable={false}
                                uploadAudioFile={false}
                                width="100%"
                                mainContainerStyle={{
                                    "margin-right": "auto",
                                    "margin-left": "auto",
                                }}
                                controllerContainerStyle={{ height: "3rem" }}
                            />
                            {props.fingerprintRecordError && (
                                <div className={dlgError}>
                                    {props.fingerprintRecordError}
                                </div>
                            )}
                            <button
                                className={btnPrimary + " z-40"}
                                onClick={props.addSpeakerFingerprint}
                            >
                                Confirm
                            </button>
                            <button
                                className={style2["cancel-button"]}
                                onClick={props.closeDialog}
                            >
                                Cancel
                            </button>
                        </div>
                    ))
                }
            </GenericDialogBox>

            <DialogBoxTwoOption
                show={props.currentForm === "SlotTaken"}
                itsclass={style["dialog-window"]}
                heading={"That slot is already connected"}
                body={
                    (props.displayText || "") +
                    " Yes joins this device as an extra one for the same table and team (it gets its own speaker cards); Cancel goes back so you can pick another slot."
                }
                deletebuttonaction={props.joinAsExtraLaptop}
                cancelbuttonaction={props.closeDialog}
            />

            <DialogBoxTwoOption
                show={props.currentForm === "ConfirmEndRec"}
                itsclass={style["dialog-window"]}
                heading={"End recording"}
                body={
                    "This stops this pod's recording. The session itself stays open for other pods."
                }
                deletebuttonaction={props.confirmEndRecording}
                cancelbuttonaction={props.closeDialog}
            />

            <DialogBoxTwoOption
                show={props.currentForm === "NavGuard"}
                itsclass={style["dialog-window"]}
                heading={"Stop Recording"}
                body={
                    "Leaving this page will stop recording. Are you sure you want to continue?"
                }
                deletebuttonaction={() => props.navigateToLogin(true)}
                cancelbuttonaction={props.closeDialog}
            />

            <DialogBox
                itsclass={"add-dialog"}
                heading={"Failed to Join"}
                message={props.displayText}
                show={props.currentForm === "JoinError"}
                closedialog={props.closeDialog}
            />

            <DialogBox
                itsclass={"add-dialog"}
                heading={"Missing Speaker Fingerprints"}
                message={props.displayText}
                show={props.currentForm === "FingerprintingError"}
                closedialog={props.closeDialog}
            />

            <DialogBox
                itsclass={"add-dialog"}
                heading={props.closedHeading || "Session ended"}
                message={props.displayText || "You have left the session."}
                show={props.currentForm === "ClosedSession"}
                closedialog={props.closeDialog}
            />

            <WaitingDialog
                itsclass={"add-dialog"}
                heading={"Connecting..."}
                message={"Please wait..."}
                show={props.currentForm === "Connecting"}
            />
            <WaitingDialog
                itsclass={"add-dialog"}
                heading={"Saving recording..."}
                message={
                    "Uploading the last of this pod's video. Keep this page open."
                }
                show={props.currentForm === "FinishingUpload"}
            />
            <WaitingDialog
                itsclass={"add-dialog"}
                heading={"Processing..."}
                message={"Please wait..."}
                show={props.currentForm === "processing"}
            />

        </>
    )
}

export { ByodJoinPage }
