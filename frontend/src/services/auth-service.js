
import { ApiService } from "./api-service";
import { UserModel } from "../models/user";
import { StudentModel } from "../models/student";
class AuthService {

  // Instructor sign-up. Creates a system user (role 'user') and signs it in,
  // unlike createStudentProfile below, which enrols a study participant and
  // creates no login. Resolves to { ok, message } for the caller to render.
  async register(email, password, confirm) {
    const body = { email: email, password: password, confirm: confirm };
    try {
      const response = await new ApiService().httpRequestCall("api/v1/register", "POST", body);
      if (response.status === 200) {
        return { ok: true, user: UserModel.fromJson(await response.json()) };
      }
      // The rate limiter answers with its own non-JSON body, so without this
      // the generic fallback below replaced a specific, actionable reason.
      if (response.status === 429) {
        return {
          ok: false,
          message: "Too many attempts from this network. Wait a minute and try again.",
        };
      }
      let message = "Could not create the account. Please try again.";
      try {
        message = (await response.json()).message || message;
      } catch {
        /* proxy and server errors are not always JSON */
      }
      return { ok: false, message };
    } catch {
      return { ok: false, message: "The server could not be reached." };
    }
  }

  // Emailed links. Each resolves to { ok, message, ... } for the page to
  // render; the server gives the same answer to forgotPassword whether or not
  // the address has an account.
  async _call(path, body) {
    try {
      const response = await new ApiService().httpRequestCall(path, "POST", body);
      if (response.status === 429) {
        return { ok: false, message: "Too many attempts. Wait a few minutes and try again." };
      }
      let data = {};
      try {
        data = await response.json();
      } catch {
        /* proxy and server errors are not always JSON */
      }
      return { ok: response.status === 200, ...data };
    } catch {
      return { ok: false, message: "The server could not be reached." };
    }
  }

  forgotPassword(email) {
    return this._call("api/v1/password/forgot", { email });
  }

  checkAccountToken(token) {
    return this._call("api/v1/password/token", { token });
  }

  resetPassword(token, password, confirm) {
    return this._call("api/v1/password/reset", { token, password, confirm });
  }

  login(email, password, setLoginStatus, setAuthObject) {
    const body = {
      email: email,
      password: password,
    };
    const fetchRes = new ApiService().httpRequestCall("api/v1/login", 'POST', body);
    fetchRes.then(
      (response) => {
        setLoginStatus(response);
        if (response.status === 200) {
          response.json().then(
            userobj => {
              const user = UserModel.fromJson(userobj);
              setAuthObject(user)
            }
          )
        }
      },
      (apiError) => {
        apiError.status = 600
        setLoginStatus(apiError);
      })
  }

  logout() {
    return new ApiService().httpRequestCall("api/v1/logout", 'POST', {});
  }

  createStudentProfile(lastname, firstname, username,setStudentObject,setAlertMessage,setShowAlert) {
    const body = {
      lastname: lastname,
      firstname: firstname,
      username: username
    };
    const fetchRes =  new ApiService().httpRequestCall("api/v1/student/addstudent", 'POST', body);
    fetchRes.then(
      (response) => {
        if (response.status === 200) {
          response.json().then(
            userobj => {
              const student = StudentModel.fromJson(userobj);
              setStudentObject(student)
            }
          )
        }else if (response.status === 400) {
           response.json().then(
            err => {
              if (err["message"] === "Username already exists."){
                const student = StudentModel.fromJson(err["data"] );
                // Re-enrollment is self-service: proceed to the recording page
                // whenever the entered name matches the existing record (the
                // server checks). A new recording replaces the old biometrics.
                if (err["reenroll_allowed"] || student.biometric_captured !== "yes"){
                  setStudentObject(student)
                }else{
                  setAlertMessage("That username already belongs to a different student. If it's yours, enter your name exactly as you registered it — otherwise pick a different username.")
                  setShowAlert(true)
                }
              }
            }
          )

          }
      },
      (apiError) => {
        apiError.status = 600
        setAlertMessage("A fatal error occurred!!!");
        setShowAlert(true);
      })
  }

    updateStudentProfile(id,lastname, firstname,biometric_captured,setStudentUpdated,setAlertMessage,setShowAlert) {
    const body = {
      id:id,
      lastname: lastname,
      firstname: firstname,
      biometric_captured: biometric_captured
    };
    const fetchRes =  new ApiService().httpRequestCall("api/v1/student/updatestudent", 'POST', body);
    fetchRes.then(
      (response) => {
        if (response.status === 200) {
          response.json().then(
            userobj => {
              StudentModel.fromJson(userobj);
              setStudentUpdated(true)
            }
          )
        }else if (response.status === 400) {
           response.json().then(
            err => {
              if (err["message"] === "Update unsuccessful"){
                setAlertMessage("The profile update is unsuccessful, please contact administrator")
                setShowAlert(true)
              }else if(err["message"]==="Student  Id must be provided"){
                setAlertMessage("Student  Id must be provided, please contact administrator")
                setShowAlert(true)
              }
            }
          )
          
          }
      },
      (apiError) => {
        apiError.status = 600
        setAlertMessage("A fatal error occurred!!!");
        setShowAlert(true);
      })
  }

  syncStudentProfile() {
    const body = {};
    return new ApiService().httpRequestCall("api/v1/syncstudenttable", 'POST', body);
  }

  syncEnabled() {
    return new ApiService().httpRequestCall("api/v1/sync/enabled", 'GET', {});
  }
 
  getStudentProfileByID(username) {
    return new ApiService().httpRequestCall("api/v1/student/getstudentbyid/"+ username, 'GET', {});
  }

  getStudentProfiles() {
    return new ApiService().httpRequestCall("api/v1/admin/students", 'GET', {});
  }

  // Roster + participation stats; the server scopes it to the caller's own
  // sessions unless they're an admin.
  getStudentsOverview() {
    return new ApiService().httpRequestCall("api/v1/students/overview", 'GET', {});
  }

  getStudentActivity(studentId) {
    return new ApiService().httpRequestCall("api/v1/students/" + studentId + "/activity", 'GET', {});
  }

  mergeStudents(duplicateId, targetId) {
    return new ApiService().httpRequestCall(
      "api/v1/admin/students/" + duplicateId + "/merge",
      'POST',
      { targetId: targetId },
    );
  }

  getRaters() {
    return new ApiService().httpRequestCall("api/v1/admin/raters", 'GET', {});
  }

  me(stateSetter) {
    this.meStatus().then((r) => stateSetter(r.status === "ok" ? r.user : "cors error"));
  }

  // /me with the failure kind kept apart, for the route guard:
  //   { status: "ok", user }
  //   { status: "denied", httpStatus }      401/403 — the login is gone
  //   { status: "unavailable", httpStatus } 5xx, network error, bad body —
  //                                          the API is unreachable, NOT the
  //                                          user logged out (a 502 during a
  //                                          deploy used to bounce teachers
  //                                          to /login mid-class).
  async meStatus() {
    try {
      const response = await new ApiService().httpRequestCall("api/v1/me", 'GET', {});
      if (response.status === 200) {
        try {
          return { status: "ok", user: UserModel.fromJson(await response.json()) };
        } catch {
          return { status: "unavailable", httpStatus: 200 };
        }
      }
      if (response.status === 401 || response.status === 403) {
        return { status: "denied", httpStatus: response.status };
      }
      return { status: "unavailable", httpStatus: response.status };
    } catch {
      return { status: "unavailable", httpStatus: null };
    }
  }

  changeEmail(currentPassword, newEmail) {
    const body = {
      password: currentPassword,
      email: newEmail,
    };
    return new ApiService().httpRequestCall("api/v1/email", 'POST', body);
  }

  changePassword(currentPassword, newPassword, confirmPassword) {
    const body = {
      password: currentPassword,
      new: newPassword,
      confirm: confirmPassword,
    };
    return new ApiService().httpRequestCall("api/v1/password", 'POST', body);
  }

  async createUser(email, role) {
    const body = {
      email: email,
      role: role,
    };
    return new ApiService().httpRequestCall("api/v1/admin/users", 'POST', body);
  }

  async createRater(sessionid,sessiondeviceid,speakerid,speakertag,raterid,type,evaluationcategory) {
    const body = {
      sessionid: sessionid,
      sessiondeviceid: sessiondeviceid,
      speakerid: speakerid,
      speakertag: speakertag,
      raterid: raterid,
      type: type,
      evaluationcategory: evaluationcategory
    };
    return new ApiService().httpRequestCall("api/v1/admin/raters", 'POST', body);
  }
  

  deleteUser(userId) {
    return new ApiService().httpRequestCall("api/v1/admin/users/" + userId, 'DELETE', {});
  }

  deleteStudent(studentId) {
    return new ApiService().httpRequestCall("api/v1/admin/students/" + studentId, 'DELETE', {});
  }

  deleteRater(id) {
    return new ApiService().httpRequestCall("api/v1/admin/raters/" + id, 'DELETE', {});
  }

  getUsers() {
    return new ApiService().httpRequestCall("api/v1/admin/users", 'GET', {});
  }

  lockUser(userId) {
    return new ApiService().httpRequestCall("api/v1/admin/users/" + userId + "/lock", 'POST', {});
  }

  unlockUser(userId) {
    return new ApiService().httpRequestCall("api/v1/admin/users/" + userId + "/unlock", 'POST', {});
  }

  changeUserRole(userId, role) {
    const body = {
      role: role,
    };
    return new ApiService().httpRequestCall("api/v1/admin/users/" + userId + "/role", 'POST', body);
  }

  resetUserPassword(userId) {
    return new ApiService().httpRequestCall("api/v1/admin/users/" + userId + "/reset", 'POST', {});
  }

  // Type can be either 'dcs' or 'aps'.
  getServerLogs(type) {
    const query = {
      log_type: type,
    };
    return new ApiService().httpRequestCall("api/v1/admin/server/logs", 'GET', query);
    
  }

  getDeviceLogs(deviceId) {
    return new ApiService().httpRequestCall("api/v1/admin/devices/" + deviceId + "/logs", 'GET', {});
  }

  deleteServerLogs(type) {
    const query = {
      log_type: type,
    };
    return new ApiService().httpRequestCall("api/v1/admin/server/logs", 'DELETE', query);
  }

  deleteDeviceLogs(deviceId) {
    return new ApiService().httpRequestCall("api/v1/admin/devices/" + deviceId + "/logs", 'DELETE', {});
  }

  allowAPIAccess(userId) {
    return new ApiService().httpRequestCall("api/v1/admin/users/" + userId + "/api", 'POST', {});
  }

  revokeAPIAccess(userId) {
    return new ApiService().httpRequestCall("api/v1/admin/users/" + userId + "/api", 'DELETE', {});
  }

}

export { AuthService }


