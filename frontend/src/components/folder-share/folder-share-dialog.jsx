import { useEffect, useState } from "react"
import { SessionService } from "../../services/session-service"
import {
    dlgWindow, dlgHeading, dlgInput, dlgSelect, dlgLabel, dlgError, dlgCancel,
    btnPrimary, btnDangerOutlineSm,
} from "../dialog-styles"

const LEVEL_HELP = {
    viewer: "Can see sessions, metrics, transcripts, audio and video",
    editor: "Can also add, rename, move and delete sessions and subfolders",
    manager: "Can also rename, move or delete the folder and share it",
}
const LEVELS = ["viewer", "editor", "manager"]
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1)

// Who can reach a folder, and (for its managers and admins) who to add. Access
// flows down the tree, so people granted on a folder above this one are
// listed too, marked with where their access comes from; they can only be
// changed there.
function FolderShareDialog({ folder, me, onClose, onLeft }) {
    const [data, setData] = useState(null)
    const [email, setEmail] = useState("")
    const [level, setLevel] = useState("viewer")
    const [error, setError] = useState("")
    const [busy, setBusy] = useState(false)

    const apply = async (request) => {
        setBusy(true)
        setError("")
        try {
            const response = await request
            const body = await response.json()
            if (response.status === 200) {
                setData(body)
                return body
            }
            setError(body.message || "Something went wrong.")
        } catch {
            setError("Could not reach the server.")
        } finally {
            setBusy(false)
        }
        return null
    }

    useEffect(() => {
        apply(new SessionService().getFolderMembers(folder.id))
    }, [folder.id])

    const add = async (e) => {
        e.preventDefault()
        if (!email.trim()) return
        if (await apply(new SessionService().setFolderMember(folder.id, email.trim(), level))) {
            setEmail("")
        }
    }
    const change = (member, newLevel) =>
        apply(new SessionService().setFolderMember(folder.id, member.email, newLevel))
    const remove = async (member) => {
        const body = await apply(new SessionService().removeFolderMember(folder.id, member.user_id))
        if (body && member.user_id === me?.id) onLeft()
    }

    const canManage = data?.can_manage_members
    return (
        <div className={dlgWindow} style={{ width: "min(34rem, 86vw)" }}>
            <div className={dlgHeading}>Share “{folder.name}”</div>
            {canManage ? (
                <form onSubmit={add} className="flex flex-col gap-2">
                    <label className={dlgLabel} htmlFor="share-email">Add a person</label>
                    <div className="flex flex-wrap gap-2">
                        <input
                            id="share-email"
                            type="email"
                            autoFocus
                            placeholder="name@example.edu"
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                            className={dlgInput + " min-w-0 flex-1 basis-48"}
                        />
                        <select
                            aria-label="Access level"
                            value={level}
                            onChange={(e) => setLevel(e.target.value)}
                            className={dlgSelect + " w-auto flex-none"}
                        >
                            {LEVELS.map((l) => <option key={l} value={l}>{cap(l)}</option>)}
                        </select>
                        <button type="submit" disabled={busy || !email.trim()} className={btnPrimary + " h-11 disabled:opacity-50"}>
                            Add
                        </button>
                    </div>
                    <p className="text-xs text-tiilt-muted">
                        {cap(level)}: {LEVEL_HELP[level]}. Applies to every subfolder too. They'll get an
                        email; someone without an account is invited to create one.
                    </p>
                </form>
            ) : null}
            {error ? <div className={dlgError} role="alert">{error}</div> : null}

            <div className={dlgLabel}>People with access</div>
            {data == null ? (
                <p className="text-sm text-tiilt-muted">Loading…</p>
            ) : (
                <ul className="flex max-h-72 flex-col divide-y divide-tiilt-line overflow-y-auto rounded-lg border border-tiilt-line">
                    <li className="flex items-center justify-between gap-3 px-3 py-2">
                        <span className="min-w-0 truncate text-sm text-tiilt-ink">
                            {data.owner.email}{data.owner.user_id === me?.id ? " (you)" : ""}
                        </span>
                        <span className="flex-none text-xs font-semibold text-tiilt-muted">Owner</span>
                    </li>
                    {data.members.map((m) => (
                        <li key={m.user_id} className="flex items-center justify-between gap-3 px-3 py-2">
                            <span className="flex min-w-0 flex-col">
                                <span className="truncate text-sm text-tiilt-ink">
                                    {m.email}{m.user_id === me?.id ? " (you)" : ""}
                                    {m.invited ? (
                                        <span
                                            title="Invited by email; hasn't set up their account yet"
                                            className="ml-1.5 rounded-full bg-tiilt-line/40 px-1.5 py-0.5 text-[11px] font-semibold text-tiilt-muted"
                                        >
                                            Invited
                                        </span>
                                    ) : null}
                                </span>
                                {m.inherited_from ? (
                                    <span className="truncate text-xs text-tiilt-muted">
                                        From “{m.inherited_from.name}”
                                    </span>
                                ) : null}
                            </span>
                            <span className="flex flex-none items-center gap-2">
                                {canManage && !m.inherited_from ? (
                                    <select
                                        aria-label={`Access level for ${m.email}`}
                                        value={m.level}
                                        disabled={busy}
                                        onChange={(e) => change(m, e.target.value)}
                                        className={dlgSelect + " h-8 w-auto py-0 text-sm"}
                                    >
                                        {LEVELS.map((l) => <option key={l} value={l}>{cap(l)}</option>)}
                                    </select>
                                ) : (
                                    <span className="text-xs font-semibold text-tiilt-muted">{cap(m.level)}</span>
                                )}
                                {!m.inherited_from && (canManage || m.user_id === me?.id) ? (
                                    <button
                                        disabled={busy}
                                        onClick={() => remove(m)}
                                        className={btnDangerOutlineSm}
                                    >
                                        {m.user_id === me?.id ? "Leave" : "Remove"}
                                    </button>
                                ) : null}
                            </span>
                        </li>
                    ))}
                </ul>
            )}
            <button className={dlgCancel} onClick={onClose}>Done</button>
        </div>
    )
}

export { FolderShareDialog }
