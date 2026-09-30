import { Navigate, useLocation } from "react-router-dom";
import { useEffect, useState } from "react";
import { AuthService } from "../services/auth-service";
import { backoffDelay } from "../globals";

// Session-scoped auth cache. Every navigation used to block on a fresh
// /api/v1/me round trip while rendering NOTHING — the blank flash before the
// spinner before the page. After the first successful check, later
// navigations render immediately from the cache; /me still re-runs in the
// background and bounces the user to /login if the session has died.
let cachedUser = null;
export const clearAuthCache = () => { cachedUser = null; };

function ProtectedRoute({ component: Component }) {
    const location = useLocation();
    const [isauth, setIsAuth] = useState(cachedUser);

    useEffect(() => {
        let cancelled = false;
        let timer = null;
        let attempt = 0;
        const check = async () => {
            const r = await new AuthService().meStatus();
            if (cancelled) return;
            const ok = r.status === "ok" && r.user && Object.keys(r.user).length !== 0;
            if (ok) {
                cachedUser = r.user;
                setIsAuth(r.user);
                return;
            }
            if (r.status === "denied" || r.status === "ok") {
                // Only 401/403 (or an empty user) means logged out.
                cachedUser = null;
                setIsAuth("denied");
                return;
            }
            // The API is unreachable (5xx, network): keep whatever we had
            // and retry with backoff — a deploy must not log the teacher out
            // and tear down the live socket.
            setIsAuth((cur) => (cur === null ? "unavailable" : cur));
            timer = setTimeout(check, backoffDelay(attempt++, 2000, 30000));
        };
        check();
        return () => {
            cancelled = true;
            clearTimeout(timer);
        };
    }, []);

    if (isauth === null) return null; // first load: wait for /me
    if (isauth === "unavailable")
        return (
            <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-tiilt-muted">
                Connecting to the server… retrying.
            </div>
        );
    if (isauth === "denied")
        return (
            <Navigate
                replace={true}
                to="/login"
                state={{ from: `${location.pathname}${location.search}` }}
            />
        );
    return <Component userdata={isauth} />;
}

export { ProtectedRoute };
