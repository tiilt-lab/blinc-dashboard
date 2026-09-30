import { useState, useEffect } from "react"
import { useNavigate } from "react-router-dom"
import { okJson } from "../lib/utils"
import {
    dlgHeading,
    dlgBody,
    dlgLabel,
    dlgInput,
    dlgSelect,
    dlgPrimary,
    dlgDanger,
    dlgCancel,
    dlgError,
    btnPrimary,
} from "../components/dialog-styles"
import { Appheader } from "../header/header-component"
import { GenericDialogBox } from "../dialog/dialog-component"
import { DataTable } from "../components/data-table"
import { StatusPill } from "../components/status-pill"
import { AppSpinner } from "../spinner/spinner-component"
import { AppContextMenu } from "../components/context-menu/context-menu-component"
import { AuthService } from "../services/auth-service"
import { UserModel } from "../models/user"
import { useRequireManager } from "../routes/roles"
import { AdminTabs } from "../components/admin-tabs"
import contextStyle from "../components/context-menu/context-menu.module.css"

// Admin user management: one table of accounts with per-row actions, instead
// of the seven separate action-first dialogs this used to be under Settings.
function UsersComponent(props) {
    const me = props.userdata
    const navigate = useNavigate()
    const [users, setUsers] = useState(null)
    const [currentForm, setCurrentForm] = useState("")
    const [status, setStatus] = useState("")
    const [statusTitle, setStatusTitle] = useState("")
    const [target, setTarget] = useState(null) // the user a dialog acts on

    const canManage = useRequireManager(me)

    useEffect(() => {
        if (!canManage) return
        loadUsers()
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    const loadUsers = async () => {
        try {
            const response = await new AuthService().getUsers()
            const all = UserModel.fromJsonList(await okJson(response, []))
            // Never act on yourself here — that's what Settings is for.
            setUsers(all.filter((u) => u.id !== me.id))
        } catch {
            setUsers([])
        }
    }

    const closeDialog = () => {
        setStatus("")
        setCurrentForm("")
        setTarget(null)
    }

    const showStatus = (title, message) => {
        setStatusTitle(title)
        setStatus(message)
        setCurrentForm("Status")
    }

    // Run a service call, refresh the list, and report the outcome. `success`
    // maps the parsed JSON body to a status message (some calls return a
    // generated password we must surface).
    const run = async (title, promise, success, failMessage) => {
        setCurrentForm("Loading")
        try {
            const response = await promise
            if (response.status === 200) {
                let body = {}
                try {
                    body = await response.json()
                } catch {
                    /* empty body is fine */
                }
                await loadUsers()
                showStatus(title, success(body))
            } else {
                await loadUsers()
                showStatus("Something went wrong", failMessage)
            }
        } catch {
            showStatus("Something went wrong", "The server could not be reached.")
        }
    }

    const createUser = (email, role) => {
        email = (email || "").trim()
        if (!email) {
            setStatus("Please enter an email address.")
            return
        }
        run(
            "User created",
            new AuthService().createUser(email, role),
            (body) =>
                `${email} was created with the role "${role}".` +
                (body.invite_emailed
                    ? `\n\nAn invite to set their password was emailed to them.`
                    : "") +
                (body.password
                    ? `\n\nTemporary password: ${body.password}`
                    : ""),
            `${email} could not be created.`,
        )
    }

    const changeRole = (user, role) =>
        run(
            "Role updated",
            new AuthService().changeUserRole(user.id, role),
            () => `${user.email} is now "${role}".`,
            `${user.email}'s role could not be updated.`,
        )

    const toggleLock = (user) =>
        user.locked
            ? run(
                  "User unlocked",
                  new AuthService().unlockUser(user.id),
                  () => `${user.email} can log in again.`,
                  `${user.email} could not be unlocked.`,
              )
            : run(
                  "User locked",
                  new AuthService().lockUser(user.id),
                  () => `${user.email} has been locked out.`,
                  `${user.email} could not be locked.`,
              )

    const resetPassword = (user) =>
        run(
            "Password reset",
            new AuthService().resetUserPassword(user.id),
            (body) =>
                `${user.email}'s password was reset.` +
                (body.password
                    ? `\n\nNew temporary password: ${body.password}`
                    : ""),
            `${user.email}'s password could not be reset.`,
        )

    const toggleApi = (user) =>
        user.api_access
            ? run(
                  "API access revoked",
                  new AuthService().revokeAPIAccess(user.id),
                  () => `${user.email} can no longer use the API.`,
                  `Could not revoke ${user.email}'s API access.`,
              )
            : run(
                  "API access granted",
                  new AuthService().allowAPIAccess(user.id),
                  () => `${user.email} can now use the API.`,
                  `Could not grant ${user.email} API access.`,
              )

    const deleteUser = () =>
        run(
            "User deleted",
            new AuthService().deleteUser(target.id),
            () => `${target.email} has been deleted.`,
            `${target.email} could not be deleted.`,
        )

    const roleTone = (role) =>
        role === "super" ? "brand" : role === "admin" ? "teal" : "neutral"

    const rowActions = (user) => (
        <AppContextMenu label={`Actions for ${user.email}`}>
            <button
                role="menuitem"
                className={contextStyle["menu-item"]}
                onClick={() => {
                    setTarget(user)
                    setCurrentForm("Role")
                }}
            >
                Change role
            </button>
            <button
                role="menuitem"
                className={contextStyle["menu-item"]}
                onClick={() => toggleLock(user)}
            >
                {user.locked ? "Unlock account" : "Lock account"}
            </button>
            <button
                role="menuitem"
                className={contextStyle["menu-item"]}
                onClick={() => {
                    setTarget(user)
                    setCurrentForm("Reset")
                }}
            >
                Reset password
            </button>
            {me.isSuper ? (
                <button
                    role="menuitem"
                    className={contextStyle["menu-item"]}
                    onClick={() => toggleApi(user)}
                >
                    {user.api_access ? "Revoke API access" : "Allow API access"}
                </button>
            ) : null}
            <button
                role="menuitem"
                className={`${contextStyle["menu-item"]} ${contextStyle["red"]}`}
                onClick={() => {
                    setTarget(user)
                    setCurrentForm("Delete")
                }}
            >
                Delete user
            </button>
        </AppContextMenu>
    )

    return (
        <>
            <div role="main" className="main-container">
                <Appheader
                    title={"Admin"}
                    leftText={false}
                    rightText={""}
                    nav={() => navigate("/home")}
                />
                <AdminTabs />
                <div className="relative min-h-0 w-full grow overflow-y-auto">
                    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-8">
                        <div className="flex items-center justify-between gap-3">
                            <span className="text-sm text-tiilt-muted">
                                {users === null
                                    ? "Loading…"
                                    : `${users.length} other ${users.length === 1 ? "account" : "accounts"}`}
                            </span>
                            <button
                                className={btnPrimary}
                                onClick={() => setCurrentForm("Add")}
                            >
                                Add user
                            </button>
                        </div>

                        {users === null ? (
                            <div className="flex justify-center py-10">
                                <AppSpinner />
                            </div>
                        ) : (
                            <DataTable
                                columns={["Email", "Role", "Status", "API", ""]}
                                rows={users.map((u) => [
                                    u.email,
                                    <StatusPill
                                        key="role"
                                        tone={roleTone(u.role)}
                                    >
                                        {u.role}
                                    </StatusPill>,
                                    u.locked ? (
                                        <StatusPill key="st" tone="orange" dot>
                                            Locked
                                        </StatusPill>
                                    ) : (
                                        <StatusPill key="st" tone="teal" dot>
                                            Active
                                        </StatusPill>
                                    ),
                                    u.api_access ? "Yes" : "—",
                                    rowActions(u),
                                ])}
                            />
                        )}
                    </div>
                </div>
            </div>

            <GenericDialogBox onClose={closeDialog} show={currentForm !== ""}>
                {currentForm === "Add" ? (
                    <div className={dlgBody}>
                        <div className={dlgHeading}>Add user</div>
                        <label htmlFor="usrEmail" className={dlgLabel}>
                            Email
                        </label>
                        <input
                            id="usrEmail"
                            className={dlgInput}
                            type="email"
                        />
                        <label htmlFor="usrRole" className={dlgLabel}>
                            Role
                        </label>
                        <select id="usrRole" className={dlgSelect}>
                            <option value="user">User</option>
                            <option value="admin">Admin</option>
                            {me.isSuper ? (
                                <option value="super">Super</option>
                            ) : null}
                        </select>
                        {status ? (
                            <div className={dlgError}>{status}</div>
                        ) : null}
                        <button
                            className={dlgPrimary}
                            onClick={() =>
                                createUser(
                                    document.getElementById("usrEmail").value,
                                    document.getElementById("usrRole").value,
                                )
                            }
                        >
                            Create user
                        </button>
                        <button className={dlgCancel} onClick={closeDialog}>
                            Cancel
                        </button>
                    </div>
                ) : null}

                {currentForm === "Role" && target ? (
                    <div className={dlgBody}>
                        <div className={dlgHeading}>Change role</div>
                        <div className="text-sm text-tiilt-muted">
                            {target.email} — currently "{target.role}".
                        </div>
                        <label htmlFor="newRole" className={dlgLabel}>
                            New role
                        </label>
                        <select
                            id="newRole"
                            className={dlgSelect}
                            defaultValue={target.role}
                        >
                            <option value="user">User</option>
                            <option value="admin">Admin</option>
                            {me.isSuper ? (
                                <option value="super">Super</option>
                            ) : null}
                        </select>
                        <button
                            className={dlgPrimary}
                            onClick={() =>
                                changeRole(
                                    target,
                                    document.getElementById("newRole").value,
                                )
                            }
                        >
                            Update role
                        </button>
                        <button className={dlgCancel} onClick={closeDialog}>
                            Cancel
                        </button>
                    </div>
                ) : null}

                {currentForm === "Reset" && target ? (
                    <div className={dlgBody}>
                        <div className={dlgHeading}>Reset password?</div>
                        <div className="text-sm text-tiilt-muted">
                            A new temporary password will be generated for{" "}
                            {target.email}. They'll need to change it on next
                            login.
                        </div>
                        <button
                            className={dlgPrimary}
                            onClick={() => resetPassword(target)}
                        >
                            Reset password
                        </button>
                        <button className={dlgCancel} onClick={closeDialog}>
                            Cancel
                        </button>
                    </div>
                ) : null}

                {currentForm === "Delete" && target ? (
                    <div className={dlgBody}>
                        <div className={dlgHeading}>Delete {target.email}?</div>
                        <div className="text-sm text-tiilt-muted">
                            This permanently removes the account. This can't be
                            undone.
                        </div>
                        <button className={dlgDanger} onClick={deleteUser}>
                            Delete user
                        </button>
                        <button className={dlgCancel} onClick={closeDialog}>
                            Cancel
                        </button>
                    </div>
                ) : null}

                {currentForm === "Loading" ? (
                    <div className={dlgBody}>
                        <div className={dlgHeading}>Working…</div>
                        <div className="flex justify-center py-4">
                            <AppSpinner />
                        </div>
                    </div>
                ) : null}

                {currentForm === "Status" ? (
                    <div className={dlgBody}>
                        <div className={dlgHeading}>{statusTitle}</div>
                        <div className="text-sm whitespace-pre-line text-tiilt-ink">
                            {status}
                        </div>
                        <button className={dlgCancel} onClick={closeDialog}>
                            Close
                        </button>
                    </div>
                ) : null}
            </GenericDialogBox>
        </>
    )
}

export { UsersComponent }
