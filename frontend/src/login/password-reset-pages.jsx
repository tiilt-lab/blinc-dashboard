import { useEffect, useState } from "react"
import { Link, useNavigate, useSearchParams } from "react-router-dom"
import { InlineSpinner } from "../components/inline-spinner"
import { btnPrimaryTall } from "../components/dialog-styles"
import { AuthService } from "../services/auth-service"
import { BrandCard } from "../components/brand-panel"

const inputClass =
    "h-12 w-full rounded-lg border border-tiilt-line bg-white px-3.5 text-base text-tiilt-ink outline-none transition " +
    "focus-visible:border-tiilt focus-visible:ring-[3px] focus-visible:ring-tiilt/30"
const labelClass = "text-sm font-semibold text-tiilt-ink"
const alertClass = "rounded-md bg-tiilt-danger-soft px-3 py-2 text-sm text-tiilt-danger"
const noteClass = "rounded-md bg-tiilt-soft px-3 py-2 text-sm text-tiilt-ink"

// Asks for an email and sends a reset link to it. The answer is the same
// whether or not the address has an account.
function ForgotPasswordPage() {
    const [params] = useSearchParams()
    const [email, setEmail] = useState(params.get("email") || "")
    const [loading, setLoading] = useState(false)
    const [result, setResult] = useState(null)

    const submit = async (e) => {
        e.preventDefault()
        if (!email.trim()) return setResult({ ok: false, message: "Enter your email address." })
        setLoading(true)
        setResult(await new AuthService().forgotPassword(email.trim()))
        setLoading(false)
    }

    return (
        <BrandCard>
            <Link to="/login" className="mb-4 text-sm font-semibold text-tiilt-muted hover:text-tiilt">
                &larr; Back to sign in
            </Link>
            <h1 className="text-xl font-semibold text-tiilt-ink">Reset your password</h1>
            <p className="mt-1 mb-6 text-sm text-tiilt-muted">
                Enter your account's email and we'll send you a link to choose a new password.
            </p>
            {result?.ok ? (
                <div className={noteClass} role="status">
                    If <b>{email.trim()}</b> has a BLINC account, a reset link is on its way. It works for
                    1 hour. Check your spam folder if it doesn't arrive in a few minutes.
                </div>
            ) : (
                <form className="flex max-w-md flex-col gap-4" onSubmit={submit} noValidate>
                    <div className="flex flex-col gap-1.5">
                        <label htmlFor="email" className={labelClass}>Email</label>
                        <input
                            id="email"
                            type="email"
                            autoComplete="username"
                            autoCapitalize="off"
                            spellCheck="false"
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                            className={inputClass}
                        />
                    </div>
                    {result && !result.ok ? <div role="alert" className={alertClass}>{result.message}</div> : null}
                    <button type="submit" disabled={loading} className={btnPrimaryTall + " gap-2.5 disabled:opacity-70"}>
                        {loading ? <><InlineSpinner />Sending…</> : "Send reset link"}
                    </button>
                </form>
            )}
        </BrandCard>
    )
}

// Where emailed links land: a password reset, or an invite to finish setting
// up an account (?invite=1 only changes the wording until the token is
// checked). Setting the password signs the person in.
function ResetPasswordPage() {
    const navigate = useNavigate()
    const [params] = useSearchParams()
    const token = params.get("token") || ""
    const [info, setInfo] = useState(null)
    const [password, setPassword] = useState("")
    const [confirm, setConfirm] = useState("")
    const [show, setShow] = useState(false)
    const [error, setError] = useState("")
    const [loading, setLoading] = useState(false)

    useEffect(() => {
        new AuthService().checkAccountToken(token).then(setInfo)
    }, [token])

    const invite = info?.ok ? info.purpose === "invite" : params.get("invite") === "1"

    const submit = async (e) => {
        e.preventDefault()
        if (!password) return setError("Choose a password.")
        if (password !== confirm) return setError("The two passwords do not match.")
        setLoading(true)
        const result = await new AuthService().resetPassword(token, password, confirm)
        setLoading(false)
        if (result.ok) return navigate("/home")
        setError(result.message || "Could not set the password.")
    }

    return (
        <BrandCard>
            <h1 className="text-xl font-semibold text-tiilt-ink">
                {invite ? "Set up your account" : "Choose a new password"}
            </h1>
            {info == null ? (
                <p className="mt-4 text-sm text-tiilt-muted">Checking your link…</p>
            ) : !info.ok ? (
                <div className="mt-4 flex max-w-md flex-col gap-4">
                    <div role="alert" className={alertClass}>
                        {info.message || "This link has expired or was already used."}
                    </div>
                    <Link to="/forgot-password" className="text-sm font-semibold text-tiilt hover:underline">
                        Send me a new link
                    </Link>
                </div>
            ) : (
                <>
                    <p className="mt-1 mb-6 text-sm text-tiilt-muted">
                        {invite ? "Choose a password for " : "For "}
                        <b className="text-tiilt-ink">{info.email}</b>. At least 8 characters.
                    </p>
                    <form className="flex max-w-md flex-col gap-4" onSubmit={submit} noValidate>
                        <input type="email" autoComplete="username" value={info.email} readOnly hidden />
                        <div className="flex flex-col gap-1.5">
                            <label htmlFor="password" className={labelClass}>New password</label>
                            <div className="relative flex">
                                <input
                                    id="password"
                                    type={show ? "text" : "password"}
                                    autoComplete="new-password"
                                    value={password}
                                    onChange={(e) => setPassword(e.target.value)}
                                    className={inputClass}
                                />
                                <button
                                    type="button"
                                    aria-pressed={show}
                                    onClick={() => setShow(!show)}
                                    className="absolute top-1/2 right-1.5 -translate-y-1/2 rounded-md px-2.5 py-2 text-xs font-semibold text-tiilt-muted hover:text-tiilt"
                                >
                                    {show ? "Hide" : "Show"}
                                </button>
                            </div>
                        </div>
                        <div className="flex flex-col gap-1.5">
                            <label htmlFor="confirm" className={labelClass}>Confirm password</label>
                            <input
                                id="confirm"
                                type={show ? "text" : "password"}
                                autoComplete="new-password"
                                value={confirm}
                                onChange={(e) => setConfirm(e.target.value)}
                                className={inputClass}
                            />
                        </div>
                        {error ? <div role="alert" className={alertClass}>{error}</div> : null}
                        <button type="submit" disabled={loading} className={btnPrimaryTall + " gap-2.5 disabled:opacity-70"}>
                            {loading ? <><InlineSpinner />Saving…</> : invite ? "Create my account" : "Save password and sign in"}
                        </button>
                    </form>
                </>
            )}
        </BrandCard>
    )
}

export { ForgotPasswordPage, ResetPasswordPage }
